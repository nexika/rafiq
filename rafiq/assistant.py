"""The loop: wait for "Rafiq", hear the request, think, answer, and listen a little longer for a
follow-up without the name.

Everything that uses the microphone or the speaker happens on this one thread: requests from the
panel and alarms are queued as jobs and run here, so a confirmation question never competes with the
wake-word listener for the microphone.
"""

from __future__ import annotations

import os
import queue
import threading
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

import numpy as np

from . import config as config_mod
from . import kws, lang, modes, presence, timing, wake
from .audio import RATE, Endpointer, Microphone, SileroVAD, calibrate, echo_cancel_source, measure_noise
from .bargein import BargeIn
from .brain import Brain
from .facts import Facts
from .paper import MorningPaper
from .scheduler import Alarm, Scheduler
from .stt import WAKE_PROMPT, WAKE_SECONDS, SpeechToText, Transcriber
from .tasks import ClaudeAgent, Task, Tasks
from .tools import Tool, brief, load_all, media
from .tools import scenes as scenes_mod
from .tts import Speaker, SpeechStream

MIN_UTTERANCE_SECONDS = 0.35
LISTEN_SECONDS = 8.0
CONFIRM_SECONDS = 10.0


@dataclass
class Context:
    """What a tool may use: settings, the scheduler, the language of the current request, a way
    to say something later (a countdown) from another thread, who to tell when the mode changes
    (the panel shows it), the long-term memory, the background tasks (None without Claude), and the
    phone watcher when a phone is set up; for a scene, the other tools and a way to ask before one
    that changes something."""
    cfg: dict
    scheduler: Scheduler
    tag: str = "en"
    say: Callable[[str, str], None] | None = None
    on_mode: Callable[[str], None] | None = None
    facts: Facts | None = None
    tasks: Tasks | None = None
    presence: presence.Presence | None = None
    confirm: Callable[[str, str], bool] | None = None
    tools: dict[str, Tool] | None = None


@dataclass
class Events:
    """What happened, for the panel: the last 100 events, and a queue per open panel."""
    history: deque = field(default_factory=lambda: deque(maxlen=100))
    listeners: list = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def publish(self, kind: str, data: dict) -> None:
        event = {"kind": kind, "time": time.time(), **data}
        with self.lock:
            self.history.append(event)
            for q in list(self.listeners):
                try:
                    q.put_nowait(event)
                except queue.Full:
                    pass

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=200)
        with self.lock:
            self.listeners.append(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self.lock:
            if q in self.listeners:
                self.listeners.remove(q)


class Assistant:
    def __init__(self, cfg: dict | None = None):
        self.cfg = cfg or config_mod.load()
        speech = self.cfg["speech"]
        self.events = Events()
        self.jobs: queue.Queue = queue.Queue()
        self.mic = Microphone.from_config(self.cfg)
        if self.mic.device is None:          # no microphone chosen: the echo-cancelled one, if loaded
            self.mic.device = self._echo_cancelled_device()
            self.mic.fallback = self.mic.device is not None
        self.vad = SileroVAD()
        self.endpointer = Endpointer(end_seconds=speech["end_silence_seconds"],
                                     max_seconds=speech["max_utterance_seconds"])
        self.wake_stt = Transcriber(speech["wake_model"], speech["languages"])
        self.stt = SpeechToText.from_config(self.cfg)   # the name is always heard on this computer
        self.speaker = Speaker(self.cfg, on_start=self._mute, on_end=self._unmute)
        barge = self.cfg["interruption"]
        self.barge_in = BargeIn(self._transcribe_stop, self._interrupted, factor=barge["factor"],
                                min_level=barge["min_level"], frames=barge["frames"]) if barge["enabled"] else None
        self.scheduler = Scheduler(config_mod.data_dir() / "alarms.json", self._alarm_due)
        self.presence = self._make_presence()
        self.facts = Facts(config_mod.data_dir() / "memory.json")
        self.tools = load_all(self.cfg)
        self.brain = Brain(self.cfg, self.tools, self.confirm, on_event=self.events.publish)
        self.paper = MorningPaper(self.cfg)
        self.ctx = Context(self.cfg, self.scheduler, say=lambda text, tag: self.submit("say", text=text, tag=tag),
                           on_mode=self._mode_changed, facts=self.facts, presence=self.presence,
                           confirm=self.confirm, tools=self.tools)
        self.brain = Brain(self.cfg, self.tools, self.confirm, on_event=self.events.publish, facts=self.facts)
        if "claude" in self.brain.backends() and self.cfg.get("anthropic_api_key"):
            self.ctx.tasks = Tasks(ClaudeAgent(self.cfg, self.tools), self._task_done, on_event=self.events.publish)
        self.tag = "en"                      # the language of the conversation so far
        self.follow_up_until = 0.0
        self.pending_answer: queue.Queue = queue.Queue()   # a yes or no clicked in the panel
        self.confirming = False
        self.muted = False                   # `rafiq mute`: the name and follow-ups are not listened for
        self.interrupted = threading.Event()                # "stop" said over Rafiq's last sentence
        self.state = "starting"
        self.speech: SpeechStream | None = None       # the answer being spoken while it is written
        self._stop = threading.Event()
        # Your own recordings of the name (`rafiq enroll`), if any; else a small Whisper model listens.
        self.spotter = kws.Spotter.load()
        self.mel = kws.Mel() if self.spotter else None
        if self.spotter:   # (rafiq enroll --sensitivity refits the threshold)
            self.name_seconds = max(len(t) for t in self.spotter.templates) * 0.02

    # ------------------------------------------------------------------ state shown in the panel

    def set_state(self, state: str) -> None:
        if state == "idle" and self.muted:
            state = "muted"
        self.state = state
        self.events.publish("state", {"state": state})

    @property
    def mode(self) -> str:
        return modes.current(self.cfg)

    def set_mode(self, mode: str) -> None:
        modes.switch(self.cfg, mode)
        self._mode_changed(self.mode)

    def _mode_changed(self, mode: str) -> None:
        self.events.publish("mode", {"mode": mode})

    def _mute(self) -> None:
        """Rafiq starts speaking: the microphone only listens for "stop", or not at all."""
        self.mic.drain()
        if self.barge_in:
            self.barge_in.start(lambda timeout: self.mic.read(timeout=timeout))
        else:
            self.mic.muted = True
        self.set_state("speaking")

    def set_muted(self, muted: bool | None = None) -> bool:
        """Stop or start listening (None: the other way round). Muting also ends the conversation:
        no follow-up, and a question asked before an action is answered no. Called from the panel's
        thread, so it only sets flags."""
        self.muted = (not self.muted) if muted is None else muted
        self.events.publish("muted", {"muted": self.muted})
        if self.muted:
            self.follow_up_until = 0.0
            if self.confirming:
                self.pending_answer.put(False)
        if self.state in ("idle", "muted"):
            self.set_state("idle")
        return self.muted

    def go_to_sleep(self) -> None:
        """"Go to sleep": the follow-up window closes at once; the name wakes Rafiq again."""
        self.follow_up_until = 0.0
        self.events.publish("sleep", {})

    def _unmute(self) -> None:
        if self.barge_in:
            self.barge_in.stop()
        time.sleep(0.15)                     # the speaker's tail would be heard as speech
        self.mic.drain()
        self.vad.reset()
        self.endpointer.reset()
        self.mic.muted = False

    # ------------------------------------------------------------------ interrupting

    def _echo_cancelled_device(self) -> str | None:
        """PipeWire's echo-cancelled microphone through the pulse device, when the module is loaded."""
        barge = self.cfg["interruption"]
        source = echo_cancel_source() if barge["enabled"] and barge["echo_cancel"] else None
        if source is None:
            return None
        os.environ.setdefault("PULSE_SOURCE", source)
        return "pulse"

    def _transcribe_stop(self, audio: np.ndarray) -> str:
        try:   # the small model only: it is fast, and a stop word is short
            return self.wake_stt.transcribe(audio, beam_size=1, hotwords=None).text
        except Exception as exc:
            self.events.publish("error", {"error": f"{type(exc).__name__}: {exc}"})
            return ""

    def _interrupted(self) -> None:
        self.interrupted.set()
        self.speaker.stop()
        self.events.publish("interrupted", {})

    # ------------------------------------------------------------------ hearing

    def calibrate(self) -> None:
        """Listen to the room for a moment and fit the speech thresholds to its noise."""
        speech = self.cfg["speech"]
        frames, probs = measure_noise(self.mic, self.vad, float(self.cfg["audio"]["calibration_seconds"]))
        cal = calibrate(frames, probs, threshold=0.5, end_seconds=speech["end_silence_seconds"])
        self.endpointer.apply(cal)
        self.events.publish("calibrated", {"threshold": cal.threshold, "end_seconds": cal.end_seconds,
                                           "noise_rms": round(cal.noise_rms, 4), "rate": self.mic.rate})

    def next_utterance(self, timeout: float | None, idle: bool = False) -> np.ndarray | None:
        """The next thing said, or None when nothing starts before the timeout (None: wait forever).
        While muted, what is heard when idle (waiting for the name or a follow-up) is dropped."""
        deadline = None if timeout is None else time.monotonic() + timeout
        while not self._stop.is_set():
            self._run_jobs()
            if deadline and time.monotonic() > deadline and not self.endpointer.active:
                return None
            frame = self.mic.read(timeout=0.1)
            if idle and self.muted:
                self.endpointer.reset()          # half an utterance from before the mute
                continue
            if frame is None:
                continue
            utterance = self.endpointer.push(frame, self.vad(frame))
            if utterance is not None and len(utterance) / RATE >= MIN_UTTERANCE_SECONDS:
                return utterance
        return None

    def hear(self, audio: np.ndarray):
        self.set_state("thinking")
        with timing.Timer("stt", model=getattr(self.stt.local, "size", "")):
            if modes.offline(self.cfg):      # offline mode: nothing leaves the computer
                heard = self.stt.local.transcribe(audio)
            else:
                heard = self.stt.transcribe(audio, hint=self.tag)
        tag = lang.dialect(heard.lang, heard.text, self.tag)
        self.events.publish("heard", {"text": heard.text, "tag": tag})
        return heard.text, tag

    # ------------------------------------------------------------------ talking

    def say(self, text: str, tag: str) -> None:
        self.events.publish("said", {"text": text, "tag": tag})
        self.speaker.say(text, tag)

    def confirm(self, action: str, tag: str) -> bool:
        """Ask out loud before an action that changes something; only a clear yes goes ahead. The
        panel's yes and no buttons answer too."""
        while not self.pending_answer.empty():
            self.pending_answer.get_nowait()
        if self.speech:                      # finish what was being said before asking
            self.speech.wait()
        self.events.publish("confirm", {"action": action, "tag": tag})
        self.confirming = True
        try:
            return self._ask_yes_or_no(action, tag)
        finally:
            self.confirming = False

    def _ask_yes_or_no(self, action: str, tag: str) -> bool:
        for _attempt in range(2):
            self.interrupted.clear()
            self.say(lang.phrase("confirm", tag, action=action), tag)
            if self.interrupted.is_set():    # "stop" over the question is a no
                break
            self.set_state("listening")
            deadline = time.monotonic() + CONFIRM_SECONDS
            while time.monotonic() < deadline:
                try:
                    answer = self.pending_answer.get_nowait()
                    self.events.publish("confirmed", {"yes": answer})
                    return answer
                except queue.Empty:
                    pass
                audio = self.next_utterance(timeout=0.5)
                if audio is None:
                    continue
                text, _ = self.hear(audio)
                if lang.is_sleep(text):
                    self.events.publish("confirmed", {"yes": False})
                    self.go_to_sleep()
                    return False
                answer = lang.yes_or_no(text)
                if answer is not None:
                    self.events.publish("confirmed", {"yes": answer})
                    if not answer:
                        self.say(lang.phrase("cancelled", tag), tag)
                    return answer
                break
        self.events.publish("confirmed", {"yes": False})
        self.say(lang.phrase("cancelled", tag), tag)
        return False

    def respond(self, text: str, tag: str) -> None:
        """Think about one request and say the answer, starting with its first sentence while the
        rest is still being written."""
        if not text.strip():
            self.say(lang.phrase("not_heard", tag), tag)
            return
        if lang.is_sleep(text):
            self.go_to_sleep()
            return
        self.tag = tag
        self.ctx.tag = tag
        scene = scenes_mod.match(text, scenes_mod.scenes_from(self.cfg))
        if scene is not None:                            # "film mode": no need to think about it
            self.play_scene(scene, tag)
            return
        self.set_state("thinking")
        # "Put X on Netflix" and the like need no model: the page opens at once.
        direct = media.direct(text, tag)
        if direct:
            self.say(direct, lang.script_language(direct, tag))
            self.follow_up_until = time.monotonic() + self.cfg["speech"]["follow_up_seconds"]
            return
        self.speech = self.speaker.stream()
        try:
            answer = self.brain.think(text, tag, self.ctx, on_speech=self.speech.put)
            self.events.publish("said", {"text": answer, "tag": lang.script_language(answer, tag)})
        finally:
            self.speech.close()
            self.speech = None
        self.follow_up_until = time.monotonic() + self.cfg["speech"]["follow_up_seconds"]

    def play_scene(self, scene: scenes_mod.Scene, tag: str) -> None:
        """Run a scene's steps and say what the steps marked to be read aloud returned, or that it's done."""
        self.ctx.tag = tag
        self.set_state("thinking")
        outcomes = scenes_mod.play(scene, self.ctx)
        self.events.publish("scene", {"id": scene.id, "steps": [[o.tool, o.result[:200]] for o in outcomes]})
        spoken = " ".join(o.result for o in outcomes if o.speak and not o.result.startswith(("error", "skipped")))
        self.say(spoken or lang.phrase("scene_done", tag), lang.script_language(spoken, tag) if spoken else tag)

    def start_the_day(self) -> None:
        """At the first start of the day, queue the day-start scene (music, lights, the daily brief)."""
        name = self.cfg.get("day_start", {}).get("scene")
        today = datetime.now().astimezone().date()
        if name and scenes_mod.first_start_today(config_mod.data_dir() / "day_start.json", today):
            self.submit("scene", name=name)

    # ------------------------------------------------------------------ jobs from other threads

    def submit(self, kind: str, **data) -> None:
        self.jobs.put((kind, data))

    def _run_jobs(self) -> None:
        while True:
            try:
                kind, data = self.jobs.get_nowait()
            except queue.Empty:
                return
            try:
                if kind == "text":
                    text = data["text"]
                    self.events.publish("typed", {"text": text})
                    self.respond(text, lang.dialect(lang.guess(text, self.tag), text, self.tag))
                elif kind == "listen":
                    self._listen_for_request(greet=False)
                elif kind == "say":
                    self.say(data["text"], data["tag"])
                elif kind == "paper":
                    self.offer_paper()
                elif kind == "brief":
                    self.play_brief()
                elif kind == "scene":
                    scene = scenes_mod.scenes_from(self.cfg).get(data["name"])
                    if scene is None:
                        raise KeyError(f"no scene called {data['name']!r} (day_start.scene)")
                    self.play_scene(scene, self.tag)
                elif kind == "mode":
                    self.set_mode(data["mode"])
            except Exception as exc:
                self.events.publish("error", {"error": f"{type(exc).__name__}: {exc}"})
            self.set_state("idle")

    def offer_paper(self) -> None:
        """The timer's offer: ask, and print on a yes. Silent when there is nothing to print."""
        result = self.paper.print_today(lambda action: self.confirm(action, self.tag))
        self.events.publish("paper", {"result": result})
        if result.startswith("printed"):
            self.say(lang.phrase("paper_printed", self.tag), self.tag)
        elif result.startswith("error: printing failed"):
            self.say(lang.phrase("paper_failed", self.tag), self.tag)

    def _alarm_due(self, alarm: Alarm) -> None:
        key = "timer_done" if alarm.kind == "timer" else "reminder"
        self.submit("say", text=lang.phrase(key, alarm.tag, label=alarm.label), tag=alarm.tag)

    def start_of_day(self) -> None:
        """Queue the daily brief, if it is turned on and has not been said yet today."""
        if self.cfg["brief"]["at_first_start"] and brief.claim_today(config_mod.data_dir() / "brief.json",
                                                                     brief.now().date()):
            self.submit("brief")

    def play_brief(self) -> None:
        self.respond(brief.REQUEST, self.cfg["brief"]["language"])

    def _make_presence(self) -> presence.Presence | None:
        settings = self.cfg["presence"]
        if not settings.get("phone_ip"):
            return None
        return presence.Presence(
            config_mod.data_dir() / "presence.json",
            lambda: presence.phone_seen(settings["phone_ip"], settings.get("phone_mac", "")),
            on_away=self._phone_left, on_home=self._phone_back,
            away_after=float(settings["away_minutes"]) * 60)

    def _phone_left(self) -> None:
        self.events.publish("presence", {"state": "away"})
        error = presence.run_scene(self.cfg, "away")
        if error:
            self.events.publish("error", {"error": error})

    def _phone_back(self) -> None:
        self.events.publish("presence", {"state": "home"})
        error = presence.run_scene(self.cfg, "home")
        if error:
            self.events.publish("error", {"error": error})
        if self.cfg["presence"].get("welcome", True):
            self.submit("say", text=lang.phrase("welcome_home", self.tag), tag=self.tag)

    def _task_done(self, task: Task) -> None:
        """Said on this thread, between requests, like an alarm."""
        if task.state == "done":
            text = lang.phrase("task_done", task.tag, result=task.result)
        else:
            text = lang.phrase("task_failed", task.tag, goal=task.goal)
        self.submit("say", text=text, tag=lang.script_language(text, task.tag))

    def _listen_for_request(self, greet: bool = True) -> None:
        if greet:
            self.say(lang.phrase("listening", self.tag), self.tag)
        self.set_state("listening")
        audio = self.next_utterance(timeout=LISTEN_SECONDS)
        if audio is None:
            return
        text, tag = self.hear(audio)
        self.respond(text, tag)

    # ------------------------------------------------------------------ the main loop

    def _spot_with_recordings(self, audio: np.ndarray) -> None:
        """Wake when the start of what was said matches your recordings of the name. Most speech
        that is not the name stops here, after a few hundred milliseconds of arithmetic."""
        score = self.spotter.score(self.mel(audio[:int(kws.MAX_SECONDS * RATE)]))
        if score > self.spotter.threshold:
            return
        self.events.publish("wake", {"score": round(score, 3), "threshold": round(self.spotter.threshold, 3)})
        if len(audio) / RATE > self.name_seconds + 1.3:    # more than the name: the request came with it
            text, tag = self.hear(audio)
            found, command = wake.find(text)
            self.respond(command if found else text, tag)
        else:
            self._listen_for_request(greet=True)

    def handle_idle_utterance(self, audio: np.ndarray) -> None:
        if time.monotonic() < self.follow_up_until:      # a follow-up: no name needed
            text, tag = self.hear(audio)
            found, rest = wake.find(text)
            self.respond(rest if found else text, tag)
            return
        if self.spotter:
            self._spot_with_recordings(audio)
            return
        # The name comes first, so only the start is checked: faster, and less to mishear.
        spotted = self.wake_stt.transcribe(audio[:int(WAKE_SECONDS * RATE)], beam_size=1, hotwords=None,
                                           prompt=WAKE_PROMPT)
        found, rest = wake.find(spotted.text)
        if not found:
            return
        self.events.publish("wake", {"text": spotted.text})
        if len(audio) / RATE > WAKE_SECONDS or len(rest.split()) >= 2:   # "Rafiq, what's the weather?" at once
            text, tag = self.hear(audio)
            found, command = wake.find(text)
            self.respond(command if found else text, tag)
        else:
            self.tag = lang.dialect(spotted.lang, spotted.text, self.tag)
            self._listen_for_request(greet=True)

    def run(self) -> None:
        self.scheduler.start()
        self.scheduler.tick()                            # alarms that came due while Rafiq was off
        if self.presence:
            self.presence.start(every=float(self.cfg["presence"]["check_seconds"]))
        self.start_the_day()
        threading.Thread(target=self.brain.warm_up, name="rafiq-warm-up", daemon=True).start()
        self.mic.start()
        if self.cfg["audio"]["auto_calibration"]:
            self.set_state("calibrating")
            self.calibrate()
        self.set_state("idle")
        self.start_of_day()
        try:
            while not self._stop.is_set():
                audio = self.next_utterance(timeout=None, idle=True)
                if audio is None:
                    continue
                try:
                    self.handle_idle_utterance(audio)
                except Exception as exc:
                    self.events.publish("error", {"error": f"{type(exc).__name__}: {exc}"})
                self.set_state("idle")
        finally:
            self.mic.stop()
            self.scheduler.stop()
            if self.presence:
                self.presence.stop()

    def stop(self) -> None:
        self._stop.set()
        self.speaker.stop()
