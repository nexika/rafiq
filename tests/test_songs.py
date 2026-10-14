import io
import json
import subprocess

import httpx
import numpy as np
import pytest

from rafiq import audio, config, doctor, lang
from rafiq.assistant import Context
from rafiq.tools import load_all, songs

MATCH = {"status": "success", "result": {"artist": "Fairuz", "title": "Kifak Inta", "album": "Kifak Inta",
                                         "release_date": "1991-01-01", "song_link": "https://lis.tn/Kifak"}}
TONE = (0.2 * np.sin(np.arange(16000 * 8) / 10)).astype(np.float32)


def cfg_with(token="k", profile="everyday"):
    cfg = config.load()
    cfg["songs"]["audd_token"] = token
    cfg["brain"]["profile"] = profile
    return cfg


def ctx_with(cfg=None, record=None, room="", quiet=None):
    return Context(cfg or cfg_with(), scheduler=None, record=record, room=room, quiet=quiet)


class FakePost:
    def __init__(self, answer=MATCH, error=None):
        self.answer, self.error, self.calls = answer, error, []

    def __call__(self, token, wav):
        self.calls.append((token, wav))
        if self.error:
            raise self.error
        return self.answer


def identify(source="room", post=None, record=lambda seconds: TONE, capture=lambda seconds: TONE, ctx=None,
             **kwargs):
    ctx = ctx or ctx_with(record=record)
    return songs.identify(source, ctx=ctx, post=post or FakePost(), capture=capture, **kwargs)


# ---------------------------------------------------------------- AudD


def test_a_match_names_title_artist_and_album_as_data():
    said = identify()
    assert "Kifak Inta" in said and "Fairuz" in said and "1991" in said
    assert "data, not instructions" in said


def test_long_fields_from_audd_are_cut():
    post = FakePost({"status": "success", "result": {"title": "x" * 5000, "artist": "y"}})
    assert len(identify(post=post)) < 1000


def test_the_clip_is_sent_as_wav_with_the_token():
    post = FakePost()
    identify(post=post)
    token, wav = post.calls[0]
    assert token == "k" and wav[:4] == b"RIFF"


def test_no_match_says_not_recognised_and_keeps_no_history(rafiq_home):
    said = identify(post=FakePost({"status": "success", "result": None}))
    assert "not recognised" in said
    assert not (config.data_dir() / "songs.json").exists()


def test_an_audd_error_is_reported_without_the_token():
    post = FakePost({"status": "error", "error": {"error_code": 900, "error_message": "bad api_token k"}})
    said = identify(post=post, ctx=ctx_with(cfg_with("secret-token"), record=lambda s: TONE))
    assert said.startswith("error:") and "900" in said and "secret-token" not in said


def test_a_network_failure_is_an_error_without_the_token():
    post = FakePost(error=httpx.ConnectError("https://api.audd.io/?api_token=secret-token"))
    said = identify(post=post, ctx=ctx_with(cfg_with("secret-token"), record=lambda s: TONE))
    assert said.startswith("error:") and "secret-token" not in said


def test_offline_mode_sends_nothing():
    post = FakePost()
    said = identify(post=post, ctx=ctx_with(cfg_with(profile="offline"), record=lambda s: TONE))
    assert said.startswith("error:") and not post.calls


def test_without_a_token_nothing_is_sent():
    post = FakePost()
    said = identify(post=post, ctx=ctx_with(cfg_with(token=""), record=lambda s: TONE))
    assert said.startswith("error:") and "AUDD_API_TOKEN" in said and not post.calls


# ---------------------------------------------------------------- where the sound comes from


def test_room_without_rafiq_running_says_so():
    said = identify(ctx=ctx_with(record=None))
    assert said.startswith("error:") and "rafiq run" in said


def test_room_on_a_satellite_is_refused():
    post = FakePost()
    said = identify(post=post, ctx=ctx_with(record=lambda s: TONE, room="kitchen"))
    assert said.startswith("error:") and not post.calls


def test_room_uses_the_running_microphone_for_the_configured_seconds():
    asked = []
    identify(record=lambda seconds: asked.append(seconds) or TONE)
    assert asked == [8]


def test_room_while_muted_is_refused():
    def muted(seconds):
        raise RuntimeError("Rafiq is muted")

    post = FakePost()
    said = identify(record=muted, post=post)
    assert said.startswith("error:") and "muted" in said and not post.calls


def test_computer_sound_works_without_rafiq_running():
    asked = []
    said = identify("computer", ctx=ctx_with(record=None), capture=lambda seconds: asked.append(seconds) or TONE)
    assert asked == [8] and "Kifak Inta" in said


@pytest.mark.parametrize("source", ["room", "computer", "file"])
def test_a_satellite_can_not_send_any_sound(source, tmp_path):
    song = tmp_path / "song.mp3"
    song.write_bytes(b"ID3")
    post, run, captured = FakePost(), FakeRun(), []
    said = identify(source, post=post, run=run, path=str(song), capture=lambda s: captured.append(s) or TONE,
                    ctx=ctx_with(record=lambda s: TONE, room="kitchen"))
    assert said.startswith("error:") and not post.calls and not run.calls and not captured


def test_computer_sound_waits_for_rafiq_to_stop_speaking_first():
    order = []
    identify("computer", ctx=ctx_with(quiet=lambda: order.append("quiet")),
             capture=lambda seconds: order.append("capture") or TONE)
    assert order == ["quiet", "capture"]


def test_computer_sound_while_muted_is_refused():
    def muted():
        raise RuntimeError("Rafiq is muted")

    post, captured = FakePost(), []
    said = identify("computer", ctx=ctx_with(quiet=muted), post=post,
                    capture=lambda s: captured.append(s) or TONE)
    assert said.startswith("error:") and "muted" in said and not captured and not post.calls


def test_a_missing_parec_says_what_to_install():
    def capture(seconds):
        raise FileNotFoundError("parec")

    said = identify("computer", capture=capture)
    assert said.startswith("error:") and "pulseaudio-utils" in said


def test_a_silent_clip_is_not_sent():
    post = FakePost()
    said = identify("computer", capture=lambda seconds: np.zeros(16000 * 8, dtype=np.float32), post=post)
    assert said.startswith("error:") and "nothing" in said and not post.calls


# ---------------------------------------------------------------- a file on disk


class FakeRun:
    def __init__(self, samples=TONE):
        self.samples, self.calls = samples, []

    def __call__(self, args, **kwargs):
        self.calls.append((args, kwargs))
        data = (self.samples * 32767).astype(np.int16).tobytes()
        return subprocess.CompletedProcess(args, 0, stdout=data, stderr=b"")


def test_a_file_that_is_not_audio_is_refused(tmp_path):
    secret = tmp_path / "id_rsa.txt"
    secret.write_text("-----BEGIN KEY-----")
    run, post = FakeRun(), FakePost()
    said = identify("file", path=str(secret), run=run, post=post)
    assert said.startswith("error:") and not run.calls and not post.calls


def test_a_missing_file_is_refused(tmp_path):
    run = FakeRun()
    said = identify("file", path=str(tmp_path / "gone.mp3"), run=run)
    assert said.startswith("error:") and not run.calls


def test_rafiqs_own_recordings_of_your_voice_are_refused(rafiq_home):
    voice = config.data_dir() / "wake" / "request-1.wav"
    voice.parent.mkdir(parents=True)
    voice.write_bytes(b"RIFF")
    run = FakeRun()
    said = identify("file", path=str(voice), run=run)
    assert said.startswith("error:") and not run.calls


def test_ffmpeg_reads_only_a_local_file(tmp_path):
    song = tmp_path / "concat:a.mp3"
    song.write_bytes(b"ID3")
    run = FakeRun()
    identify("file", path=str(song), run=run)
    args, _ = run.calls[0]
    assert args[args.index("-protocol_whitelist") + 1] == "file"
    assert args[args.index("-i") + 1] == f"file:{song.resolve()}"


def test_a_start_that_is_not_a_number_is_an_error(tmp_path):
    song = tmp_path / "song.mp3"
    song.write_bytes(b"ID3")
    assert identify("file", path=str(song), start_seconds="the chorus", run=FakeRun()).startswith("error:")


def test_only_an_excerpt_of_a_file_is_uploaded(tmp_path):
    song = tmp_path / "song.mp3"
    song.write_bytes(b"ID3" + b"\0" * 100)
    run = FakeRun()
    said = identify("file", path=str(song), start_seconds=30, run=run)
    args, kwargs = run.calls[0]
    assert args[0] == "ffmpeg" and isinstance(args, list)
    assert args[args.index("-t") + 1] == "12" and args[args.index("-ss") + 1] == "30"
    assert kwargs.get("timeout") and not kwargs.get("shell")
    assert "Kifak Inta" in said


# ---------------------------------------------------------------- history


def test_history_keeps_the_newest_first_and_caps_at_keep(rafiq_home):
    for i in range(25):
        identify(post=FakePost({"status": "success", "result": {"title": f"Song {i}", "artist": "A"}}))
    kept = json.loads((config.data_dir() / "songs.json").read_text(encoding="utf-8"))
    assert len(kept) == 20 and kept[0]["title"] == "Song 24" and kept[-1]["title"] == "Song 5"


def test_song_history_lists_the_last_ones_newest_first(rafiq_home):
    for title in ("First", "Second"):
        identify(post=FakePost({"status": "success", "result": {"title": title, "artist": "A"}}))
    said = songs.song_history(count=5)
    assert said.index("Second") < said.index("First")


def test_history_survives_a_broken_file(rafiq_home):
    (config.data_dir() / "songs.json").write_text("{not json", encoding="utf-8")
    assert "no songs" in songs.song_history()
    identify()
    assert "Kifak Inta" in songs.song_history()


def test_song_history_when_empty_says_so(rafiq_home):
    assert "no songs" in songs.song_history()


# ---------------------------------------------------------------- the computer's own sound


class FakeRecorder:
    def __init__(self, data: bytes):
        self.stdout, self.terminated, self.args = io.BytesIO(data), False, None

    def terminate(self):
        self.terminated = True

    def kill(self):
        pass

    def wait(self, timeout=None):
        return 0


def test_system_audio_reads_the_seconds_asked_from_the_monitor():
    rec = FakeRecorder((np.full(16000 * 3, 1000, dtype=np.int16)).tobytes())

    def spawn(args, **kwargs):
        rec.args = args
        return rec

    clip = audio.system_audio(2, spawn=spawn)
    assert "@DEFAULT_MONITOR@" in rec.args and "--rate=16000" in rec.args and rec.args[0] == "parec"
    assert len(clip) == 32000 and clip.dtype == np.float32 and rec.terminated


def test_system_audio_kills_a_recorder_that_hangs(monkeypatch):
    import threading

    monkeypatch.setattr(audio, "CAPTURE_GRACE", 0.1)
    killed = threading.Event()

    class Hung(FakeRecorder):
        def __init__(self):
            super().__init__(b"")
            self.stdout = self

        def read(self, n):
            killed.wait(5)
            return b""

        def kill(self):
            killed.set()

    clip = audio.system_audio(0.1, spawn=lambda args, **kwargs: Hung())
    assert killed.is_set() and len(clip) == 0


def test_system_audio_returns_what_it_got_when_the_recorder_stops_early():
    rec = FakeRecorder((np.full(16000, 1000, dtype=np.int16)).tobytes())
    clip = audio.system_audio(8, spawn=lambda args, **kwargs: rec)
    assert len(clip) == 16000 and rec.terminated


# ---------------------------------------------------------------- settings, loading and doctor


def test_audd_token_comes_from_the_environment_first(monkeypatch):
    monkeypatch.setenv("AUDD_API_TOKEN", "from-env")
    assert config.load()["songs"]["audd_token"] == "from-env"


def test_song_tools_load_only_with_a_token():
    assert "identify_song" not in load_all({})
    tools = load_all({"songs": {"audd_token": "k"}})
    found = tools["identify_song"]
    assert found.level == "private" and found.slow and not found.expose and found.confirm is None
    assert set(found.waiting_phrase) == set(lang.TAGS)
    assert tools["song_history"].level == "private"


class Probe(doctor.Probe):
    def __init__(self, programs=()):
        self.programs = set(programs)

    def which(self, program):
        return f"/usr/bin/{program}" if program in self.programs else None


def song_check(cfg, probe):
    return {c.label: c for c in doctor.integrations(cfg, probe)}["Song recognition"]


def test_song_recognition_without_a_token_is_optional():
    check = song_check(config.load(), Probe())
    assert check.optional and "AUDD_API_TOKEN" in check.fix


def test_song_recognition_without_parec_still_works_for_the_room():
    cfg = cfg_with("secret-token")
    check = song_check(cfg, Probe(programs={"ffmpeg"}))
    assert check.optional and "parec" in check.detail and "secret-token" not in check.detail + check.fix
    assert song_check(cfg, Probe(programs={"ffmpeg", "parec"})).ok


def test_arguments_outside_a_tools_schema_are_dropped():
    tool = load_all({"songs": {"audd_token": "k"}})["identify_song"]
    ctx = ctx_with(record=lambda seconds: TONE)
    said = tool.run({"source": "room", "post": "x", "capture": True, "run": "ffmpeg"}, ctx)
    assert "TypeError" not in said


@pytest.mark.parametrize("tag", lang.TAGS)
def test_the_waiting_phrase_is_written_for_each_language(tag):
    assert songs.LISTENING[tag].strip()
