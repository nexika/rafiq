"""The phone companion (#207): pairing, the phone's own listener behind `tailscale serve`, and what a phone may do."""

import json
import queue
import stat
import threading
import time
import urllib.error
import urllib.request

import numpy as np
import pytest

from rafiq import mobile
from rafiq.assistant import Assistant, Events

# ---------------------------------------------------------------------- pairing and device tokens

@pytest.fixture
def devices(tmp_path):
    return mobile.Devices(tmp_path / "mobile")


def test_a_pairing_code_is_short_typeable_and_never_stored(devices):
    code = devices.start_pairing("Sam's iPhone")
    assert len(code) == 8 and code.isalnum() and code.isupper()
    assert not set(code) & set("O0I1L")                 # nothing to misread on a phone keyboard
    saved = (devices.folder / "pairing.json").read_text()
    assert code not in saved
    assert stat.S_IMODE((devices.folder / "pairing.json").stat().st_mode) == 0o600


def test_a_code_becomes_the_phones_own_token_once(devices):
    code = devices.start_pairing("Sam's iPhone")
    name, token = devices.redeem(code.lower())          # typed in lower case on the phone
    assert name == "Sam's iPhone" and len(token) >= 40
    assert devices.check(token) == "Sam's iPhone"
    with pytest.raises(mobile.PairingError):
        devices.redeem(code)
    assert token not in (devices.folder / "devices.json").read_text()
    assert stat.S_IMODE((devices.folder / "devices.json").stat().st_mode) == 0o600


def test_a_code_runs_out_after_five_minutes(devices, monkeypatch):
    code = devices.start_pairing("phone")
    later = time.time() + mobile.PAIR_SECONDS + 1
    monkeypatch.setattr(mobile.time, "time", lambda: later)
    with pytest.raises(mobile.PairingError, match="ran out"):
        devices.redeem(code)


def test_a_code_dies_after_five_wrong_tries(devices):
    code = devices.start_pairing("phone")
    for _ in range(mobile.PAIR_TRIES):
        with pytest.raises(mobile.PairingError):
            devices.redeem("WRONGCOD")
    with pytest.raises(mobile.PairingError):
        devices.redeem(code)                            # even the right one, now


def test_without_a_pairing_started_no_code_works(devices):
    with pytest.raises(mobile.PairingError, match="rafiq mobile pair"):
        devices.redeem("ABCDEFGH")


def test_a_phone_can_be_unpaired_and_its_token_stops_working(devices):
    _, token = devices.redeem(devices.start_pairing("old phone"))
    _, other = devices.redeem(devices.start_pairing("new phone"))
    assert devices.names() == ["new phone", "old phone"]
    assert devices.remove("old phone") is True
    assert devices.check(token) is None
    assert devices.check(other) == "new phone"
    assert devices.remove("old phone") is False


def test_pairing_again_under_a_name_replaces_that_phones_token(devices):
    _, first = devices.redeem(devices.start_pairing("phone"))
    _, second = devices.redeem(devices.start_pairing("phone"))
    assert devices.check(first) is None and devices.check(second) == "phone"


@pytest.mark.parametrize("name", ["", " ", "x" * 41, "tab\there", "line\nbreak"])
def test_a_phone_name_must_be_short_and_on_one_line(devices, name):
    with pytest.raises(ValueError):
        devices.start_pairing(name)


def test_wrong_tokens_are_refused(devices):
    devices.redeem(devices.start_pairing("phone"))
    assert devices.check("") is None
    assert devices.check("not-a-token") is None


# ---------------------------------------------------------------------- the listener

class FakeBrain:
    def __init__(self):
        self.stopped = False

    def stop(self):
        self.stopped = True


class FakeAssistant:
    def __init__(self):
        self.events, self.pending_answer, self.confirming = Events(), queue.Queue(), False
        self.brain = FakeBrain()
        self.asked = []
        self.phone_runs, self.state, self.busy = {}, "idle", False

    def ask_phone(self, device, text="", audio=None, timeout=None):
        self.asked.append({"device": device, "text": text, "audio": audio})
        if self.busy:
            return {"error": "Rafiq is still answering your other requests; try again in a moment"}
        return {"heard": text or "from audio", "tag": "en", "answer": "It's sunny.", "audio": "QUJD",
                "mime": "audio/mpeg"}


@pytest.fixture
def phone(devices):
    assistant = FakeAssistant()
    server = mobile.serve(assistant, "127.0.0.1", 0, devices)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _, token = devices.redeem(devices.start_pairing("Sam's iPhone"))
    yield assistant, port, token
    server.shutdown()


def call(port, path, body=None, token=None, host=None, raw=None, kind="application/json"):
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method="POST" if data is not None else "GET",
                                 data=data)
    req.add_header("Content-Type", kind)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if host:
        req.add_header("Host", host)
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers)


def test_the_listener_is_only_on_this_computer(devices):
    for host in ("0.0.0.0", "192.168.1.20", "100.101.102.103", "::"):
        with pytest.raises(ValueError, match="tailscale serve"):
            mobile.serve(FakeAssistant(), host, 0, devices)


def test_the_page_manifest_and_icons_need_no_token_and_hold_no_secret(phone):
    _, port, token = phone
    status, page, headers = call(port, "/")
    assert status == 200 and b"<html" in page and token.encode() not in page
    assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]
    status, manifest, _ = call(port, "/manifest.webmanifest")
    found = json.loads(manifest)
    assert status == 200 and found["display"] == "standalone" and found["start_url"] == "/"
    for icon in found["icons"]:
        status, png, headers = call(port, icon["src"])
        assert status == 200 and png.startswith(b"\x89PNG") and headers["Content-Type"] == "image/png"
    assert call(port, "/apple-touch-icon.png")[0] == 200


def test_a_tailnet_name_is_a_host_the_listener_answers(phone):
    _, port, token = phone
    assert call(port, "/", host="desk.tail1234.ts.net")[0] == 200
    assert call(port, "/api/ask", {"text": "hi"}, token, host="desk.tail1234.ts.net")[0] == 200


@pytest.mark.parametrize("host", ["evil.example", "desk.tail1234.ts.net.evil.example", "127.0.0.1:1", ""])
def test_another_host_name_is_refused(phone, host):
    _, port, token = phone
    assert call(port, "/api/ask", {"text": "hi"}, token, host=host or " ")[0] == 403


@pytest.mark.parametrize("path, body", [("/api/ask", {"text": "hi"}), ("/api/confirm", {"id": "q", "yes": True}),
                                        ("/api/stop", {}), ("/events", None)])
def test_everything_but_the_page_and_pairing_needs_a_paired_phone(phone, path, body):
    _, port, _token = phone
    assert call(port, path, body)[0] == 403
    assert call(port, path, body, token="x" * 43)[0] == 403


def test_an_unpaired_phone_is_refused_at_once(phone, devices):
    _, port, token = phone
    devices.remove("Sam's iPhone")
    assert call(port, "/api/ask", {"text": "hi"}, token)[0] == 403


def test_pairing_over_the_listener_gives_the_phone_its_token(phone, devices):
    _, port, _ = phone
    code = devices.start_pairing("Tablet")
    status, body, _ = call(port, "/api/pair", {"code": code})
    found = json.loads(body)
    assert status == 200 and found["name"] == "Tablet"
    assert devices.check(found["token"]) == "Tablet"
    assert call(port, "/api/pair", {"code": code})[0] == 403


def test_asking_by_text_answers_with_the_words_and_the_voice(phone):
    assistant, port, token = phone
    status, body, _ = call(port, "/api/ask", {"text": "weather?"}, token)
    assert status == 200
    assert json.loads(body) == {"heard": "weather?", "tag": "en", "answer": "It's sunny.", "audio": "QUJD",
                                "mime": "audio/mpeg"}
    assert assistant.asked == [{"device": "Sam's iPhone", "text": "weather?", "audio": None}]


def test_asking_by_voice_decodes_the_recording_first(phone, monkeypatch):
    assistant, port, token = phone
    seen = []
    monkeypatch.setattr(mobile, "decode", lambda data: seen.append(data) or np.zeros(16000, "float32"))
    status, body, _ = call(port, "/api/voice", token=token, raw=b"webm bytes", kind="audio/webm")
    assert status == 200 and json.loads(body)["heard"] == "from audio"
    assert seen == [b"webm bytes"]
    assert assistant.asked[0]["audio"].shape == (16000,)


def test_a_recording_that_cant_be_decoded_is_refused(phone, monkeypatch):
    _, port, token = phone

    def broken(data):
        raise ValueError("not audio")

    monkeypatch.setattr(mobile, "decode", broken)
    assert call(port, "/api/voice", token=token, raw=b"junk", kind="audio/webm")[0] == 400


def test_a_recording_too_long_is_refused_before_it_is_read(phone):
    import http.client

    _, port, token = phone
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    conn.putrequest("POST", "/api/voice")
    conn.putheader("Authorization", f"Bearer {token}")
    conn.putheader("Content-Length", str(mobile.MAX_AUDIO + 1))      # announced; none of it is sent
    conn.endheaders()
    assert conn.getresponse().status == 413
    conn.close()


def test_the_phone_answers_yes_or_no_but_never_always(phone):
    assistant, port, token = phone
    assert call(port, "/api/confirm", {"id": "q1", "yes": True, "always": True}, token)[0] == 200
    assert call(port, "/api/confirm", {"id": "q2", "yes": False}, token)[0] == 200
    assert assistant.pending_answer.get_nowait() == ("q1", True, "Sam's iPhone")    # never "always"
    assert assistant.pending_answer.get_nowait() == ("q2", False, "Sam's iPhone")


def test_a_confirm_without_a_yes_or_no_is_refused(phone):
    _, port, token = phone
    assert call(port, "/api/confirm", {"id": "q1", "yes": "yes"}, token)[0] == 400


def test_stop_ends_the_request_and_says_no_to_a_waiting_question(phone):
    assistant, port, token = phone
    assistant.confirming = True
    assert call(port, "/api/stop", {}, token)[0] == 200
    assert assistant.brain.stopped
    assert assistant.pending_answer.get_nowait() is False


@pytest.mark.parametrize("path", ["/api/settings", "/api/permissions/revoke", "/api/history/clear", "/api/mode",
                                  "/api/tool", "/api/panel-key"])
def test_settings_permissions_history_and_tools_are_not_offered(phone, path):
    _, port, token = phone
    assert call(port, path, {}, token)[0] == 404


def open_stream(port, token):
    req = urllib.request.Request(f"http://127.0.0.1:{port}/events")
    req.add_header("Authorization", f"Bearer {token}")
    resp = urllib.request.urlopen(req, timeout=10)
    assert resp.headers["Content-Type"].startswith("text/event-stream")
    return resp


def next_event(resp, skip=("run",)) -> dict:
    """The next event, past the run's own start and end (which the phone gets for its requests too)."""
    while True:
        line = resp.readline().decode()
        if line.startswith("data: "):
            event = json.loads(line.removeprefix("data: "))
            if event["kind"] not in skip:
                return event
        elif not line:
            raise EOFError("the stream ended")


def test_the_phone_follows_the_steps_of_its_own_requests_only(phone):
    assistant, port, token = phone
    with assistant.events.run():
        assistant.events.publish("said", {"text": "someone at home was told this before"})
    resp = open_stream(port, token)
    assert next_event(resp)["kind"] == "state"            # no history: only how things stand now
    with assistant.events.run():
        assistant.events.publish("heard", {"text": "read my email"})          # a request at home
        assistant.events.publish("said", {"text": "Your bank wrote..."})
    with assistant.events.run() as run_id:
        assistant.phone_runs[run_id] = "Sam's iPhone"
        assistant.events.publish("step", {"name": "weather", "status": "running"})
    event = next_event(resp)
    assert event["kind"] == "step" and event["name"] == "weather"
    resp.close()


def test_another_phones_requests_are_not_shown(phone):
    assistant, port, token = phone
    resp = open_stream(port, token)
    next_event(resp)
    with assistant.events.run() as run_id:
        assistant.phone_runs[run_id] = "Tablet"
        assistant.events.publish("said", {"text": "for the tablet"})
    with assistant.events.run() as run_id:
        assistant.phone_runs[run_id] = "Sam's iPhone"
        assistant.events.publish("said", {"text": "for Sam"})
    assert next_event(resp)["text"] == "for Sam"
    resp.close()


def test_forget_that_at_home_takes_only_words_the_phone_was_shown_off_it():
    runs, shown = {"r1": "p"}, set()
    assert mobile.for_phone({"kind": "said", "text": "mine", "run_id": "r1"}, "p", runs, shown)
    found = mobile.for_phone({"kind": "forgotten", "texts": ["mine", "someone else's"], "kinds": ["said"]}, "p",
                             runs, shown)
    assert found["texts"] == ["mine"] and found["kinds"] == []
    assert mobile.for_phone({"kind": "forgotten", "texts": ["someone else's"]}, "p", runs, shown) is None


@pytest.mark.parametrize("kind", ["dictated", "notify", "reading", "interpreted", "tool", "privacy", "mode"])
def test_events_outside_the_phones_requests_stay_at_home(kind):
    assert mobile.for_phone({"kind": kind, "text": "x", "run_id": "home"}, "p", {"r1": "p"}, set()) is None
    assert mobile.for_phone({"kind": kind, "text": "x"}, "p", {}, set()) is None


def test_unpairing_a_phone_ends_its_open_stream(phone, devices, monkeypatch):
    monkeypatch.setattr(mobile, "STREAM_CHECK_SECONDS", 0.2)
    _, port, token = phone
    resp = open_stream(port, token)
    next_event(resp)
    devices.remove("Sam's iPhone")
    begun = time.monotonic()
    with pytest.raises(EOFError):
        while True:
            next_event(resp)
    assert time.monotonic() - begun < 5


# ---------------------------------------------------------------------- decoding the phone's recording

def test_a_recording_is_decoded_with_ffmpeg_to_16_khz_mono(monkeypatch):
    calls = []

    class Done:
        returncode = 0
        stdout = (np.array([0, 16384, -16384], dtype="<i2")).tobytes()
        stderr = b""

    def run(args, input=None, capture_output=None, timeout=None, check=None):
        calls.append((args, timeout))
        return Done()

    monkeypatch.setattr(mobile.subprocess, "run", run)
    samples = mobile.decode(b"\x1a\x45\xdf\xa3 webm")
    assert np.allclose(samples, [0.0, 0.5, -0.5])
    args, timeout = calls[0]
    assert args[0] == "ffmpeg" and "-ar" in args and args[args.index("-ar") + 1] == "16000"
    assert args[args.index("-ac") + 1] == "1" and args[args.index("-t") + 1] == str(mobile.MAX_SECONDS)
    assert args[args.index("-protocol_whitelist") + 1] == "file"
    assert args[args.index("-f") + 1] == "matroska" and args.index("-f") < args.index("-i")
    assert timeout and timeout <= 30


@pytest.mark.parametrize("data, kind", [(b"\x1a\x45\xdf\xa3....", "matroska"), (b"\0\0\0\x18ftypmp42", "mov"),
                                        (b"OggS\0\2", "ogg"), (b"RIFF\0\0\0\0WAVE", "wav")])
def test_the_recordings_a_phone_makes_are_known_by_their_first_bytes(data, kind):
    assert mobile.audio_format(data) == kind


@pytest.mark.parametrize("data", [b"#EXTM3U\n#EXTINF:1,\nfile:///home/me/secret.m4a\n",
                                  b"ffconcat version 1.0\nfile '/home/me/a.wav'\n", b"", b"<html>"])
def test_a_playlist_or_anything_else_is_never_given_to_ffmpeg(data, monkeypatch):
    monkeypatch.setattr(mobile.subprocess, "run", lambda *a, **k: pytest.fail("ffmpeg ran"))
    with pytest.raises(ValueError):
        mobile.decode(data)


def test_a_recording_ffmpeg_cant_read_is_a_value_error(monkeypatch):
    class Failed:
        returncode = 1
        stdout = b""
        stderr = b"Invalid data"

    monkeypatch.setattr(mobile.subprocess, "run", lambda *a, **k: Failed())
    with pytest.raises(ValueError):
        mobile.decode(b"OggS but broken")


# ---------------------------------------------------------------------- the assistant's side

def bare_assistant(said_by_brain="It's sunny."):
    a = Assistant.__new__(Assistant)
    a.events, a.pending_answer, a.tag, a.silent = Events(), queue.Queue(), "en", False
    a.from_phone, a.phone_runs = "", {}
    seen = {}

    def respond(text, tag, source="voice"):
        seen.update(text=text, tag=tag, source=source, silent=a.silent, phone=a.from_phone)
        a.events.publish("said", {"text": said_by_brain})

    a.respond = respond
    a.hear = lambda audio: ("what's the weather", "en")

    class Speaker:
        def synthesize(self, text, tag, folder, name="say"):
            path = folder / f"{name}.mp3"
            path.write_bytes(b"MP3")
            seen["spoken"] = text
            return path, []

    a.speaker = Speaker()
    return a, seen


def test_a_request_from_the_phone_is_silent_at_home_and_its_answer_comes_back_as_sound():
    a, seen = bare_assistant()
    reply = queue.Queue()
    a._from_phone({"device": "Sam's iPhone", "text": "weather?", "audio": None, "reply": reply})
    result = reply.get_nowait()
    assert seen["silent"] is True and seen["phone"] == "Sam's iPhone" and seen["source"] == "phone"
    assert result["heard"] == "weather?" and result["answer"] == "It's sunny."
    assert result["audio"] == "TVAz" and result["mime"] == "audio/mpeg"     # base64 of b"MP3"
    assert a.silent is False and a.from_phone == ""                          # back as it was


def test_a_spoken_request_from_the_phone_is_transcribed_at_home():
    a, seen = bare_assistant()
    reply = queue.Queue()
    a._from_phone({"device": "p", "text": "", "audio": np.zeros(16000, "float32"), "reply": reply})
    assert seen["text"] == "what's the weather"
    assert reply.get_nowait()["heard"] == "what's the weather"


def test_a_question_for_a_phone_request_is_asked_on_screens_only_and_cant_be_remembered():
    a = Assistant.__new__(Assistant)
    a.events, a.pending_answer, a.speech, a.from_phone = Events(), queue.Queue(), None, "Sam's iPhone"
    seen = []
    a._panel_answer = lambda tag, aloud=True, seconds=10.0: seen.append((aloud, seconds)) or True
    a._ask_yes_or_no = lambda *args, **kw: pytest.fail("asked out loud at home")
    assert a.confirm("send the email", "en") is True
    assert seen == [(False, 60.0)]                       # and a minute to answer, from a pocket
    confirm = [e for e in a.events.history if e["kind"] == "confirm"][0]
    assert confirm["always"] is False and confirm["from"] == "Sam's iPhone"


def test_ask_phone_waits_for_the_loop_and_gives_up_in_time():
    a = Assistant.__new__(Assistant)
    a.jobs = queue.Queue()
    assert a.ask_phone("p", text="hi", timeout=0.05) is None
    kind, data = a.jobs.get_nowait()
    assert kind == "phone" and data["device"] == "p" and data["text"] == "hi"
    assert data["deadline"] is not None


def test_a_phone_request_that_timed_out_never_runs_later():
    a, seen = bare_assistant()
    a.jobs = queue.Queue()
    a.ask_phone("p", text="turn off the lights", timeout=0.01)
    _kind, data = a.jobs.get_nowait()
    time.sleep(0.02)
    a._from_phone(data)
    assert seen == {}                                    # respond was never called
    assert a.phone_waiting == 0


def test_only_two_phone_requests_wait_at_once():
    a = Assistant.__new__(Assistant)
    a.jobs = queue.Queue()
    a.ask_phone("p", text="one", timeout=0.01)
    a.ask_phone("p", text="two", timeout=0.01)
    assert "error" in a.ask_phone("p", text="three", timeout=0.01)
    assert a.jobs.qsize() == 2


def test_phone_requests_are_held_while_a_question_waits():
    from rafiq.assistant import WAIT_FOR_ANSWER

    assert "phone" in WAIT_FOR_ANSWER


def test_a_phone_request_inside_another_leaves_the_outer_ones_phone_set():
    a, _seen = bare_assistant()
    a.from_phone = "outer phone"
    a._from_phone({"device": "inner phone", "text": "hi", "audio": None, "reply": queue.Queue()})
    assert a.from_phone == "outer phone"


def test_a_phones_yes_counts_only_for_its_own_requests():
    a = Assistant.__new__(Assistant)
    a.pending_answer, a.question_id, a.question_from = queue.Queue(), "q1", ""
    a.pending_answer.put(("q1", True, "Sam's iPhone"))          # a question asked at home, or for MCP
    assert a._clicked() is None
    a.question_from = "Tablet"
    a.pending_answer.put(("q1", True, "Sam's iPhone"))          # another phone's request
    assert a._clicked() is None
    a.question_from = "Sam's iPhone"
    a.pending_answer.put(("q1", True, "Sam's iPhone"))
    assert a._clicked() is True


def test_a_phones_no_counts_for_any_question():
    a = Assistant.__new__(Assistant)
    a.pending_answer, a.question_id, a.question_from = queue.Queue(), "q1", ""
    a.pending_answer.put(("q1", False, "Sam's iPhone"))
    assert a._clicked() is False


def test_the_home_panels_yes_still_answers_a_phones_question():
    a = Assistant.__new__(Assistant)
    a.pending_answer, a.question_id, a.question_from = queue.Queue(), "q1", "Sam's iPhone"
    a.pending_answer.put(("q1", True))
    assert a._clicked() is True


@pytest.mark.parametrize("text", ["dictate", "go to sleep", "interpret between English and French"])
def test_from_the_phone_nothing_takes_the_home_microphone_or_speaker(text):
    a = Assistant.__new__(Assistant)
    a.history, said = None, []
    a.say = lambda words, tag, cards=None: said.append(words)
    a.dictate = lambda tag: pytest.fail("dictation started at home")
    a.go_to_sleep = lambda: pytest.fail("Rafiq went to sleep at home")
    a._interpret_unless_off = lambda pair, tag: pytest.fail("the interpreter started at home")
    a._respond(text, "en", "phone")
    from rafiq import lang

    assert said == [lang.phrase("phone_not_here", "en")]


def test_a_busy_rafiq_is_said_as_429(phone):
    assistant, port, token = phone
    assistant.busy = True
    status, body, _ = call(port, "/api/ask", {"text": "hi"}, token)
    assert status == 429 and "other requests" in json.loads(body)["error"]


def test_with_the_tailnet_name_known_only_that_name_is_answered():
    assert mobile.host_ok("desk.tail1234.ts.net", 8768, "desk.tail1234.ts.net")
    assert mobile.host_ok("desk.tail1234.ts.net:443", 8768, "desk.tail1234.ts.net")
    assert not mobile.host_ok("other.tail1234.ts.net", 8768, "desk.tail1234.ts.net")
    assert mobile.host_ok("127.0.0.1:8768", 8768, "desk.tail1234.ts.net")


def test_every_change_to_the_phone_files_holds_a_file_lock(devices, monkeypatch):
    import fcntl

    held = []
    real = fcntl.flock
    monkeypatch.setattr(fcntl, "flock", lambda f, how: held.append(how) or real(f, how))
    devices.redeem(devices.start_pairing("p"))
    devices.remove("p")
    assert held == [fcntl.LOCK_EX] * 3


def test_the_page_takes_the_code_as_shown_and_unlocks_sound_with_the_tap():
    from importlib import resources

    page = resources.files("rafiq").joinpath("mobile.html").read_text()
    assert 'maxlength="9"' in page                    # ABCD-EFGH
    assert "unlock();" in page and 'case "forgotten"' in page
    assert "innerHTML" not in page


# ---------------------------------------------------------------------- the commands

def test_rafiq_mobile_pair_shows_a_code_and_the_steps(capsys, monkeypatch):
    from rafiq import __main__ as cli

    monkeypatch.setattr(cli, "tailnet_name", lambda: "desk.tail1234.ts.net")
    assert cli.main(["mobile", "pair", "Sam's iPhone"]) == 0
    out = capsys.readouterr().out
    code = out.split("Sam's iPhone: ")[1].split()[0]
    assert len(code) == 9 and code[4] == "-"
    assert "sudo tailscale serve --bg unix:" in out and str(mobile.socket_path()) in out
    assert "https://desk.tail1234.ts.net/" in out
    assert "enabled = true" in out                       # it's off by default
    from rafiq import config

    name, _token = mobile.Devices(config.home() / "mobile").redeem(code)   # as shown, with the dash
    assert name == "Sam's iPhone"


def test_rafiq_mobile_devices_and_unpair(capsys):
    from rafiq import __main__ as cli
    from rafiq import config

    devices = mobile.Devices(config.home() / "mobile")
    _, token = devices.redeem(devices.start_pairing("Tablet"))
    assert cli.main(["mobile", "devices"]) == 0
    assert capsys.readouterr().out.strip() == "Tablet"
    assert cli.main(["mobile", "unpair", "Tablet"]) == 0
    assert devices.check(token) is None
    assert cli.main(["mobile", "unpair", "Tablet"]) == 1
    assert cli.main(["mobile", "devices"]) == 0
    assert "No phone is paired" in capsys.readouterr().out


def test_the_tailnet_name_comes_from_tailscale_status():
    from rafiq.__main__ import tailnet_name

    class Done:
        returncode, stdout = 0, json.dumps({"Self": {"DNSName": "desk.tail1234.ts.net."}})

    assert tailnet_name(lambda *a, **k: Done()) == "desk.tail1234.ts.net"

    def missing(*a, **k):
        raise FileNotFoundError("tailscale")

    assert tailnet_name(missing) == ""


def test_this_computer_s_tailnet_addresses_come_from_the_same_status():
    from rafiq.__main__ import tailnet_self

    class Done:
        returncode = 0
        stdout = json.dumps({"Self": {"DNSName": "desk.tail1234.ts.net.",
                                      "TailscaleIPs": ["100.64.0.5", "fd7a:115c:a1e0::5", 7]}})

    assert tailnet_self(lambda *a, **k: Done()) == ("desk.tail1234.ts.net", ("100.64.0.5", "fd7a:115c:a1e0::5"))

    class Stopped:
        returncode, stdout = 1, ""

    assert tailnet_self(lambda *a, **k: Stopped()) == ("", ())


def test_the_phone_page_is_off_by_default():
    from rafiq import config

    assert config.load()["mobile"] == {"enabled": False, "listen": "socket", "port": 8768}


# ---------------------------------------------------------------------- re-review (#230): runs, memory, reading

def test_while_a_phone_request_runs_a_request_from_home_waits_for_it():
    a, _seen = bare_assistant()
    a.jobs, a.from_phone = queue.Queue(), "Sam's iPhone"
    a._typed = lambda data: pytest.fail("a home request ran inside the phone's run")
    a.submit("text", text="read my bank statement")
    a._run_jobs()
    assert a.jobs.get_nowait()[0] == "text"            # held, for after the phone's request


def test_a_phone_request_waits_while_a_home_request_is_answered():
    a, _seen = bare_assistant()
    a.jobs = queue.Queue()
    a._from_phone = lambda data: pytest.fail("the phone joined a home run (dictation, the interpreter)")
    with a.events.run():
        a.submit("phone", device="p", text="hi", audio=None, reply=queue.Queue(), deadline=None)
        a._run_jobs()
    assert a.jobs.get_nowait()[0] == "phone"


def test_a_phone_request_never_takes_over_a_run_it_didnt_open():
    a, seen = bare_assistant()
    a.jobs = queue.Queue()
    with a.events.run() as home:
        a._from_phone({"device": "p", "text": "hi", "audio": None, "reply": queue.Queue(), "deadline": None})
    assert seen == {} and home not in a.phone_runs
    assert a.jobs.get_nowait()[0] == "phone"           # back in the queue, for when the home request is done


def test_the_answer_survives_a_long_question_full_of_other_events():
    a, _seen = bare_assistant()

    def respond(text, tag, source="voice"):
        for _ in range(400):                           # the microphone level, five times a second, for 80 s
            a.events.publish("level", {"level": 0.1}, keep=False)
        a.events.publish("said", {"text": "Done, the lights are off."})

    a.respond = respond
    reply = queue.Queue()
    a._from_phone({"device": "p", "text": "lights off", "audio": None, "reply": reply, "deadline": None})
    assert reply.get_nowait()["answer"] == "Done, the lights are off."
    assert a.events.watchers == []                     # its collector is gone


def test_a_phone_has_a_conversation_of_its_own_and_home_approvals_dont_answer_for_it():
    from rafiq.brain import Memory

    a, _seen = bare_assistant()
    home = Memory()
    a.brain = type("B", (), {"memory": home, "approvals_apply": True})()
    during = {}

    def respond(text, tag, source="voice"):
        during.update(memory=a.brain.memory, apply=a.brain.approvals_apply)

    a.respond = respond
    a._from_phone({"device": "p", "text": "what did I just ask?", "audio": None, "reply": queue.Queue(),
                   "deadline": None})
    assert during["memory"] is not home and during["apply"] is False
    assert a.brain.memory is home and a.brain.approvals_apply is True
    first = during["memory"]
    a._from_phone({"device": "p", "text": "and before?", "audio": None, "reply": queue.Queue(), "deadline": None})
    assert during["memory"] is first                   # the same phone keeps its own conversation


def test_with_home_approvals_off_a_tool_approved_always_is_asked_again():
    from test_brain_panel import tools_with

    from rafiq import config
    from rafiq.brain import Brain

    asked = []

    class Approvals:
        def allowed(self, name):
            return True

        def remember(self, name):
            pytest.fail("remembered from a phone")

    brain = Brain(config.load(), tools_with([]), lambda action, tag, **kw: asked.append(action) or True,
                  approvals=Approvals())
    brain.approvals_apply = False
    (step,) = brain._queue(["power"])
    brain._run_tool("power", {}, "en", None, brain.tools, None, None, step)
    assert asked == ["shut down the computer"]


def test_a_phones_question_never_comes_back_as_always_and_not_after_its_deadline():
    a = Assistant.__new__(Assistant)
    a.events, a.pending_answer, a.speech, a.from_phone = Events(), queue.Queue(), None, "p"
    a._panel_answer = lambda tag, aloud=True, seconds=10.0: "always"
    a.phone_deadline = time.monotonic() + 60
    assert a.confirm("send the email", "en") is True
    a.phone_deadline = time.monotonic() - 1               # the phone was told "no answer in time"
    assert a.confirm("send the email", "en") is False


def test_a_question_asked_inside_another_gives_the_outer_one_its_id_back():
    a = Assistant.__new__(Assistant)
    a.events, a.pending_answer, a.speech, a.from_phone = Events(), queue.Queue(), None, ""
    ids = []

    def inner(tag, aloud=True, seconds=10.0):
        ids.append(a.question_id)
        if len(ids) == 1:                                 # the outer question waits; another one is asked
            a.confirm("inner", "en", aloud=False)
            ids.append(a.question_id)
        return False

    a._panel_answer = inner
    a.confirm("outer", "en", aloud=False)
    outer_id, inner_id, after = ids
    assert inner_id != outer_id and after == outer_id


def test_from_the_phone_forget_that_forgets_only_the_phones_own_request(tmp_path):
    from rafiq.history import History

    history = History(tmp_path / "history.json")
    history.add("call mum", "Calling.", "en", "voice")
    assert history.forget_last("phone") is None            # the last one was asked at home
    history.add("lights off", "Done.", "en", "phone")
    assert history.forget_last("phone").heard == "lights off"


def test_the_reading_at_home_can_be_put_back_as_it_was():
    from rafiq.reader import Reader, Segment

    reader = Reader(interrupt=lambda: None)
    reader.load([Segment("one", "en"), Segment("two", "en")])
    reader.index, reader.state = 1, "paused"
    kept = reader.saved()
    reader.load([Segment("a phone's page", "en")])
    reader.put_back(kept)
    assert reader.saved() == kept and reader.index == 1 and reader.state == "paused"


def test_pairing_takes_json_only(phone, devices):
    _, port, _ = phone
    code = devices.start_pairing("Tablet")
    status, _body, _ = call(port, "/api/pair", raw=json.dumps({"code": code}).encode(), kind="text/plain")
    assert status == 415
    assert call(port, "/api/pair", {"code": code})[0] == 200       # the code still works: nothing was tried


def test_a_phone_follows_rafiq_in_two_tabs_at_most(phone):
    _, port, token = phone
    first, second = open_stream(port, token), open_stream(port, token)
    next_event(first)
    next_event(second)
    status, _body, _ = call(port, "/events", token=token)
    assert status == 429
    first.close()
    second.close()


def test_recordings_decoded_at_once_are_limited(devices, monkeypatch):
    monkeypatch.setattr(mobile, "MAX_DECODING", 0)
    monkeypatch.setattr(mobile, "decode", lambda data: pytest.fail("decoded over the limit"))
    server = mobile.serve(FakeAssistant(), "127.0.0.1", 0, devices)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _, token = devices.redeem(devices.start_pairing("p"))
    try:
        port = server.server_address[1]
        assert call(port, "/api/voice", token=token, raw=b"OggS", kind="audio/ogg")[0] == 429
    finally:
        server.shutdown()


def test_connections_over_the_limit_are_closed_at_once(devices, monkeypatch):
    import socket

    monkeypatch.setattr(mobile, "MAX_CONNECTIONS", 1)
    server = mobile.serve(FakeAssistant(), "127.0.0.1", 0, devices)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]
    try:
        slow = socket.create_connection(("127.0.0.1", port))      # says nothing, holding the one slot
        time.sleep(0.2)
        with pytest.raises((urllib.error.URLError, ConnectionError, http_client_errors())):
            urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=3).read()
        slow.close()
    finally:
        server.shutdown()


def http_client_errors():
    import http.client

    return http.client.HTTPException


def test_letting_go_before_the_microphone_opens_records_nothing():
    from importlib import resources

    page = resources.files("rafiq").joinpath("mobile.html").read_text()
    assert "if (starting) released = true;" in page and "if (discard) recorder.stop();" in page


def test_a_phones_resume_reading_leaves_the_paused_reading_at_home_as_it_was():
    from types import SimpleNamespace

    from rafiq import config, lang
    from rafiq.reader import Reader, Segment

    a = Assistant.__new__(Assistant)
    a.cfg, a.events, a.history, a.silent, a.nudger = config.load(), Events(), None, True, None
    a.reader = Reader(interrupt=lambda: None)
    a.reader.load([Segment(f"sentence {i}", "en") for i in range(300)])
    a.reader.index, a.reader.state = 140, "paused"
    kept = a.reader.saved()
    said = []
    a.say = lambda words, tag, cards=None: said.append(words)
    a.overlay = SimpleNamespace(show_again=lambda: True)
    a.practice = SimpleNamespace(active=False, rate=lambda: 1.0, tag="en", home="en")
    a.ctx = SimpleNamespace(tag="en")
    a.set_state = lambda state: None
    a.speaker = SimpleNamespace(rate=1.0)
    a.publish_spending = lambda tag: None
    a.read = lambda: pytest.fail("read aloud at home")

    def think(text, tag, ctx, on_speech=None):
        a.reader.resume()                                   # what the reading tool does for "resume reading"
        return "Resumed."

    a.brain = SimpleNamespace(think=think)
    a._respond("resume reading", "en", "phone")
    assert a.reader.saved() == kept                         # still paused at sentence 140
    assert said == [lang.phrase("phone_not_here", "en")]
