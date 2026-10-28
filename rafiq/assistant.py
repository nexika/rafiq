"""The loop: wait for "Rafiq", hear the request, think, answer, and listen a little longer for a
follow-up without the name.

Everything that uses the microphone or the speaker happens on this one thread: requests from the
panel and alarms are queued as jobs and run here, so a confirmation question never competes with the
wake-word listener for the microphone.
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import queue
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime

import numpy as np

from . import config as config_mod
from . import dictation, interpret, kws, lang, modes, notify, nudges, overlay, presence, privacy, timing, voiceid, wake
from . import history as history_mod
from .audio import RATE, Endpointer, Microphone, SileroVAD, calibrate, echo_cancel_source, level, measure_noise
from .bargein import BargeIn
from .brain import Brain
from .budget import Budget
from .facts import Facts
from .gestures import GestureControl, SystemDesktop
from .gestures import settings as gesture_settings
from .models import Models
from .nudges import Nudge, Nudger, OpenRouteService
from .panel import EVENTS_VERSION
from .paper_print import MorningPaper
from .permissions import Approvals
from .practice import Practice
from .reader import Reader
from .scheduler import Alarm, Scheduler
from .stt import WAKE_SECONDS, SpeechToText, Transcriber
from .tasks import ClaudeAgent, Task, Tasks
from .tools import Tool, brief, dev, load_all, media
from .tools import scenes as scenes_mod
from .tts import Speaker, SpeechStream

MIN_UTTERANCE_SECONDS = 0.35
LISTEN_SECONDS = 8.0
CONFIRM_SECONDS = 10.0
LEVEL_SECONDS = 0.2          # how often the panel's microphone meter moves
MUTED_RECORDING = "Rafiq is muted, so it does not record anything"

logger = logging.getLogger(__name__)


@dataclass
class Context:
    """What a tool may use: settings, the scheduler, the reader, the language of the current request, a way
    to say something later (a countdown) from another thread, who to tell when the mode changes
    (the panel shows it), the long-term memory, the background tasks (None without Claude), and the
    phone watcher when a phone is set up; for a scene, the other tools and a way to ask before one
    that changes something; the hand gestures; and, for a request from a satellite, its room;
    the spending so far; the language practice session; and a way to record the room (a song to
    recognise) or to wait until Rafiq is quiet before recording this computer, only when Rafiq is running."""
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
    gestures: GestureControl | None = None
    room: str = ""
    budget: Budget | None = None
    practice: Practice | None = None
    reader: Reader | None = None
    record: Callable[[float], np.ndarray] | None = None
    quiet: Callable[[], None] | None = None


@dataclass
class Events:
    """What happened, for the panel: the last 100 events, and a queue per open panel. Watchers are
    called with each event as it happens (the answer overlay is one). Events published inside `run()`
    carry its run_id, so a client built apart from Rafiq (the widget) can group a request's steps."""
    history: deque = field(default_factory=lambda: deque(maxlen=100))
    listeners: list = field(default_factory=list)
    watchers: list = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)
    # A context variable, not an attribute, holding the run and the thread that opened it: the panel's
    # thread and the background tasks never pick up the request running on the loop, even where a new
    # thread inherits its parent's context (free-threaded Python).
    current: contextvars.ContextVar = field(default_factory=lambda: contextvars.ContextVar("rafiq_run", default=None))

    def run_id(self) -> str | None:
        run = self.current.get()
        return run[0] if run is not None and run[1] == threading.get_ident() else None

    @contextmanager
    def run(self) -> Iterator[str]:
        """One request, from what was heard or typed to the answer. A run inside a run is the same run."""
        outer = self.run_id()
        if outer is not None:
            yield outer
            return
        run_id = uuid.uuid4().hex[:8]
        token = self.current.set((run_id, threading.get_ident()))
        self.publish("run", {"status": "started"})
        end: dict = {"status": "failed"}
        try:
            yield run_id
            end["status"] = "done"
        except Exception as exc:         # said in the run, so a client grouping by run_id sees why
            end["error"] = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            self.publish("run", end)
            self.current.reset(token)

    def publish(self, kind: str, data: dict, keep: bool = True) -> None:
        """Send an event to every open panel. Passing ones (keep=False, like the microphone level)
        are not replayed to a panel opened later."""
        event = {"kind": kind, "time": time.time(), "version": EVENTS_VERSION, **data}
        run_id = self.run_id()
        if run_id is not None:
            event.setdefault("run_id", run_id)
        # The log is redacted (privacy.RedactFilter); the panel, on this computer only, is not.
        logger.log(logging.DEBUG if kind == "state" or not keep else logging.INFO, "%s %s", kind,
                   json.dumps(data, ensure_ascii=False, default=str))
        with self.lock:
            if keep:
                self.history.append(event)
            for q in list(self.listeners):
                try:
                    q.put_nowait(event)
                except queue.Full:
                    pass
        for watch in list(self.watchers):
            try:
                watch(kind, data)
            except Exception:            # a broken watcher must not stop Rafiq from answering
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

    def drop(self, texts: set[str]) -> None:
        """Forget what was heard, typed or said with these texts, here and in the open panels. What was
        heard counts with the name before it too ("Rafiq, what time is it?")."""
        def gone(e: dict) -> bool:
            text = e.get("text")
            return e["kind"] in ("heard", "typed", "said") and isinstance(text, str) and (
                text in texts or (e["kind"] == "heard" and wake.find(text)[1] in texts))

        with self.lock:
            dropped = [e["text"] for e in self.history if gone(e)]
            kept = [e for e in self.history if not gone(e)]
            self.history.clear()
            self.history.extend(kept)
        # Open panels remove these from the page; not kept, so the texts are not replayed later.
        self.publish("forgotten", {"texts": sorted(texts | set(dropped))}, keep=False)


class Assistant:
    history: history_mod.History | None = None   # what was heard and answered, set in __init__
    nudger: Nudger | None = None             # calendar nudges, when turned on

    def __init__(self, cfg: dict | None = None):
        self.cfg = cfg or config_mod.load()
        speech = self.cfg["speech"]
        self.wake_names = wake.names(self.cfg)           # names it answers to besides "Rafiq"
        self.events = Events()
        self.overlay = overlay.Overlay(overlay.pick_backend(self.cfg))
        self.events.watchers.append(self.overlay.feed)
        self.silent = bool(self.cfg["overlay"].get("silent"))   # answers on screen only
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
        self.stt_lock = threading.Lock()     # satellites' requests use the same model from other threads
        self.budget = Budget(self.cfg)
        self.speaker = Speaker(self.cfg, on_start=self._mute, on_end=self._unmute, budget=self.budget)
        barge = self.cfg["interruption"]
        self.barge_in = BargeIn(self._transcribe_stop, self._interrupted, factor=barge["factor"],
                                min_level=barge["min_level"], frames=barge["frames"]) if barge["enabled"] else None
        self.scheduler = Scheduler(config_mod.data_dir() / "alarms.json", self._alarm_due)
        self.practice = Practice(config_mod.data_dir() / "practice.json", on_event=self.events.publish)
        self.reader = Reader(interrupt=self.speaker.stop)
        # Hand gestures: the camera tracker posts to the panel, so `rafiq run` sets the url once it serves.
        self.gestures = GestureControl(SystemDesktop(), self.speaker.stop, self.events.publish,
                                       options=gesture_settings(self.cfg))
        self.presence = self._make_presence()
        self.facts = Facts(config_mod.data_dir() / "memory.json")
        self.tools = load_all(self.cfg)
        self.approvals = Approvals()
        self.brain = Brain(self.cfg, self.tools, self.confirm, on_event=self.events.publish, budget=self.budget,
                           approvals=self.approvals)
        self.paper = MorningPaper(self.cfg)
        self.ctx = Context(self.cfg, self.scheduler, say=lambda text, tag: self.submit("say", text=text, tag=tag),
                           on_mode=self._mode_changed, facts=self.facts, presence=self.presence,
                           confirm=self.confirm, tools=self.tools, gestures=self.gestures, budget=self.budget,
                           practice=self.practice, reader=self.reader, record=self.record,
                           quiet=self.quiet)
        self.nudger = self._nudger()
        self.brain = Brain(self.cfg, self.tools, self.confirm, on_event=self.events.publish, facts=self.facts,
                           budget=self.budget, approvals=self.approvals)
        if "claude" in self.brain.backends() and self.cfg.get("anthropic_api_key"):
            self.ctx.tasks = Tasks(ClaudeAgent(self.cfg, self.tools), self._task_done, on_event=self.events.publish)
        self.models = Models(self.cfg, on_event=self.events.publish, on_whisper=self._use_whisper)
        self.keyboard = dictation.Keyboard()
        self.dictating = False
        self.history = history_mod.History(config_mod.data_dir() / "history.json",
                                           self.cfg["history"]["retention_days"])
        self.tag = "en"                      # the language of the conversation so far
        self.follow_up_until = 0.0
        self.pending_answer: queue.Queue = queue.Queue()   # a yes or no clicked in the panel
        self.confirming = False
        self.muted = False                   # `rafiq mute`: the name and follow-ups are not listened for
        self.interrupted = threading.Event()                # "stop" said over Rafiq's last sentence
        self.state = "starting"
        self._level_at = 0.0
        self.speech: SpeechStream | None = None       # the answer being spoken while it is written
        self._stop = threading.Event()
        # Your own recordings of the name (`rafiq enroll`), if any; else a small Whisper model listens.
        self.spotter = kws.Spotter.load()
        self.mel = kws.Mel() if self.spotter else None
        if self.spotter:   # (rafiq enroll --sensitivity refits the threshold)
            self.name_seconds = max(len(t) for t in self.spotter.templates) * 0.02
        # Your voice print (`rafiq voiceid`), if any: then only your spoken yes confirms an action.
        self.voice_print = voiceid.VoicePrint.load()
        self.embedder = voiceid.Embedder() if self.voice_print else None

    def _use_whisper(self, size: str) -> None:
        """A new Whisper size from the panel: the next request is heard with it (it loads then)."""
        self.stt = SpeechToText.from_config(self.cfg)   # activate_whisper set speech.whisper_model

    # ------------------------------------------------------------------ state shown in the panel

    def set_project(self, path: str) -> str:
        """The project folder for the developer tools, chosen in the panel."""
        folder = dev.set_project(path)
        if not folder.startswith("error"):
            self.cfg["dev"]["project"] = folder
            self.events.publish("project", {"path": folder})
        return folder

    def set_state(self, state: str) -> None:
        if state == "idle" and self.muted:
            state = "muted"
        self.state = state
        self.events.publish("state", {"state": state})

    def status(self) -> dict:
        """What the panel's drawer shows: the settings it may change (never a key), the spend so far
        and the timers and reminders waiting."""
        return {"settings": config_mod.settable(self.cfg), "choices": config_mod.CHOICES,
                "spend": self.brain.spend(),
                "tasks": [{"kind": a.kind, "label": a.label, "due": a.due} for a in self.scheduler.pending()]}

    def change_settings(self, changes: dict) -> None:
        """Save settings changed in the panel; the brain and the voice read them on their next use."""
        config_mod.save_settings(changes, self.cfg)
        self.events.publish("settings", {"settings": config_mod.settable(self.cfg)})

    def _publish_level(self, frame: np.ndarray) -> None:
        now = time.monotonic()
        if now - self._level_at >= LEVEL_SECONDS:
            self._level_at = now
            self.events.publish("level", {"level": level(frame)}, keep=False)
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
            self._publish_level(frame)
            utterance = self.endpointer.push(frame, self.vad(frame))
            if utterance is not None and len(utterance) / RATE >= MIN_UTTERANCE_SECONDS:
                return utterance
        return None

    def hear(self, audio: np.ndarray, languages: list[str] | None = None):
        self.set_state("thinking")
        with timing.Timer("stt", model=getattr(self.stt.local, "size", "")):
            heard = self.transcribe(audio, hint=self.tag, languages=languages)
        tag = lang.dialect(heard.lang, heard.text, self.tag)
        self.events.publish("heard", {"text": heard.text, "tag": tag})
        return heard.text, tag

    def transcribe(self, audio: np.ndarray, hint: str | None = None, languages: list[str] | None = None):
        with self.stt_lock:
            # Offline mode: nothing leaves the computer. Interpreting: Whisper, narrowed to the two languages.
            if modes.offline(self.cfg) or languages:
                return self.stt.local.transcribe(audio, languages=languages)
            return self.stt.transcribe(audio, hint=hint)

    # ------------------------------------------------------------------ talking

    def say(self, text: str, tag: str, cards: list[dict] | None = None) -> None:
        self.events.publish("said", {"text": text, "tag": tag, **({"cards": cards} if cards else {})})
        if not self.silent:
            self.speaker.rate = self.practice.rate()     # slower for beginners while practising
            self.speaker.say(text, tag)

    def notify(self, text: str, event: str, tag: str | None, now: datetime | None = None) -> None:
        """A long job ended (`rafiq notify`): say so in the conversation's language, unless muted or
        in quiet hours, when the panel shows it only."""
        tag = tag or self.tag
        sentence = notify.message(text, event, tag)
        spoken = notify.should_speak(self.cfg, self.muted, now)
        self.events.publish("notify", {"text": sentence, "event": event, "spoken": spoken})
        if spoken:
            self.speaker.say(sentence, lang.script_language(sentence, tag))

    def quiet(self) -> None:
        """Wait until Rafiq has finished speaking, before a tool records the room or this computer, so its
        own voice is not in the recording. Muted (now, or while it finished), nothing may be recorded."""
        if self.muted:
            raise RuntimeError(MUTED_RECORDING)
        if self.speech:
            self.speech.wait()
        if self.muted:
            raise RuntimeError(MUTED_RECORDING)

    def record(self, seconds: float) -> np.ndarray:
        """About `seconds` of what the microphone hears, for a tool (a song to recognise). Muting from the
        panel during the recording stops it: nothing heard is returned."""
        self.quiet()
        self.set_state("listening")
        try:
            self.mic.drain()
            frames, heard, deadline = [], 0, time.monotonic() + seconds + 2
            while heard < seconds * RATE and time.monotonic() < deadline:
                frame = self.mic.read(timeout=0.5)
                if self.muted:
                    raise RuntimeError(MUTED_RECORDING)
                if frame is not None:
                    frames.append(frame)
                    heard += len(frame)
        finally:
            self.set_state("thinking")
        return np.concatenate(frames) if frames else np.zeros(0, dtype=np.float32)

    def confirm(self, action: str, tag: str, can_remember: bool = True) -> bool | str:
        """Ask out loud before an action that changes something; only a clear yes goes ahead. The
        panel's yes and no buttons answer too. When the tool may be remembered, the panel's Always
        button or a spoken "yes, always" answers "always": yes, and don't ask again for this tool.
        With a voice print, a spoken yes must be in your voice; when Rafiq is not sure it is, only the
        panel's Yes button goes ahead."""
        while not self.pending_answer.empty():
            self.pending_answer.get_nowait()
        if self.speech:                      # finish what was being said before asking
            self.speech.wait()
        self.events.publish("confirm", {"action": action, "tag": tag, "always": can_remember})
        self.confirming = True
        try:
            return self._ask_yes_or_no(action, tag, can_remember)
        finally:
            self.confirming = False

    def _ask_yes_or_no(self, action: str, tag: str, can_remember: bool = True) -> bool | str:
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
                    self.events.publish("confirmed", {"yes": bool(answer)})
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
                if answer is None and lang.is_always(text):   # a bare "always" is a yes too
                    answer = True
                if answer and not self._your_voice(audio):
                    self.say(lang.phrase("voice_unsure", tag), tag)
                    return self._panel_answer(tag)
                if answer is not None:
                    self.events.publish("confirmed", {"yes": answer})
                    if not answer:
                        self.say(lang.phrase("cancelled", tag), tag)
                    return "always" if answer and can_remember and lang.is_always(text) else answer
                break
        self.events.publish("confirmed", {"yes": False})
        self.say(lang.phrase("cancelled", tag), tag)
        return False

    def _your_voice(self, audio: np.ndarray) -> bool:
        """Whether a spoken yes is in the enrolled voice (always, without a voice print)."""
        if self.voice_print is None:
            return True
        score = self.voice_print.score(self.embedder(audio))
        match = self.voice_print.matches(score)
        self.events.publish("voice", {"score": round(score, 3), "threshold": round(self.voice_print.threshold, 3),
                                      "match": match})
        return match

    def _panel_answer(self, tag: str) -> bool:
        """Wait for the panel's Yes or No; nothing said out loud counts now."""
        self.set_state("listening")
        deadline = time.monotonic() + CONFIRM_SECONDS
        while time.monotonic() < deadline:
            try:
                answer = self.pending_answer.get_nowait()
                self.events.publish("confirmed", {"yes": bool(answer)})
                return answer
            except queue.Empty:
                pass
            self.next_utterance(timeout=0.5)
        self.events.publish("confirmed", {"yes": False})
        self.say(lang.phrase("cancelled", tag), tag)
        return False

    def respond(self, text: str, tag: str, source: str = "voice") -> None:
        """Think about one request and say the answer, starting with its first sentence while the
        rest is still being written, and keep both in the history."""
        with self.events.run():
            self._respond(text, tag, source)

    def _respond(self, text: str, tag: str, source: str) -> None:
        if not text.strip():
            self.say(lang.phrase("not_heard", tag), tag)
            return
        if lang.is_forget(text) and self.history is not None:
            last = self.history.forget_last()
            if last:
                self.forget([last])
            self.say(lang.phrase("forgotten" if last else "nothing_to_forget", tag), tag)
            return
        pair = interpret.parse_request(text)
        if pair:
            self.interpret(pair)
            return
        if lang.is_show_again(text):
            if not self.overlay.show_again():
                self.say(lang.phrase("nothing_to_show", tag), tag)
            return
        if lang.is_sleep(text):
            self.go_to_sleep()
            return
        practising = self.practice.active
        if practising:          # a learner's French can sound like English to speech-to-text
            tag = self.practice.tag
        self.tag = tag
        self.ctx.tag = tag
        if self.nudger:                      # a nudge with no language of its own is said in yours
            self.nudger.user_tag = tag
        if dictation.wants_dictation(text):
            self.dictate(tag)
            return
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
        self.speaker.rate = self.practice.rate()             # slower for beginners while practising
        self.speech = None if self.silent else self.speaker.stream()   # silent: on screen only

        def voice(text: str, guess: str) -> str:
            if self.practice.active:                         # just started, or still going
                return self.practice.tag
            if practising:                                   # just ended: the summary is in the learner's language
                return lang.script_language(text, self.practice.home)
            return guess

        def speak(text: str, guess: str) -> None:
            self.speaker.rate = self.practice.rate()
            self.speech.put(text, voice(text, guess))

        try:
            answer = self.brain.think(text, tag, self.ctx, on_speech=speak if self.speech else None)
            if self.history is not None:
                self.history.add(text, answer, tag, source)
            self.events.publish("said", {"text": answer,
                                         "tag": voice(answer, lang.script_language(answer, tag))})
        finally:
            if self.speech:
                self.speech.close()
            self.speech = None
        self.publish_spending(tag)
        if self.reader.active:            # read_aloud or "resume reading" loaded something
            self.read()
        self.follow_up_until = time.monotonic() + self.cfg["speech"]["follow_up_seconds"]

    # ------------------------------------------------------------------ what was heard, and where it goes

    def forget(self, entries: list[history_mod.Entry]) -> None:
        """After entries leave the history: take them out of the panel's events and the conversation
        the model is given next time, too."""
        if not entries:
            return
        for entry in entries:
            self.brain.memory.forget(entry.heard, entry.said)
        self.events.drop({e.heard for e in entries} | {e.said for e in entries})

    def privacy(self) -> dict:
        return history_mod.privacy(self.brain.backends(), self.speaker.engines())

    # ------------------------------------------------------------------ dictation

    def dictate(self, tag: str) -> None:
        """Type what is said into the focused window, one utterance at a time, until "stop dictating"
        (or the shortcut again). The panel shows the "dictating" state the whole time, so text never
        goes into a window unannounced."""
        session = dictation.Session()
        self.dictating = True
        self.events.publish("dictation", {"on": True, "tag": tag})
        self.say(lang.phrase("dictating", tag), tag)
        self.set_state("dictating")
        try:
            while self.dictating and not self._stop.is_set():
                audio = self.next_utterance(timeout=0.5)
                if audio is None or not self.dictating:
                    continue
                heard = self.transcribe(audio, hint=tag)
                stopped, text = session.add(heard.text, heard.lang)
                if text:
                    self.keyboard.type(text)
                    self.events.publish("dictated", {"text": text, "tag": heard.lang})
                if stopped:
                    break
        except dictation.TypingFailed as exc:
            self.events.publish("error", {"error": str(exc)})
        finally:
            self.dictating = False
            self.events.publish("dictation", {"on": False, "tag": tag})
        self.say(lang.phrase("dictation_stopped", tag), tag)

    def read(self) -> None:
        """Read aloud what the reader holds, until the end, a pause or a stop (from the panel)."""
        def say(text: str, tag: str) -> None:
            if not self.speaker.say(text, tag):      # no voice works: don't notify sentence after sentence
                self.reader.stop()

        self.reader.run(say, on_progress=lambda i, n: self.events.publish("reading", {"index": i, "total": n,
                                                                                    "state": "reading"}))
        self.events.publish("reading", {"index": self.reader.index, "total": len(self.reader.segments),
                                        "state": self.reader.state})

    def interpret(self, pair: interpret.Pair) -> None:
        """Interpreter mode: say what each person says in the other's language until one says stop."""
        def listen(timeout: float):
            self.set_state("listening")
            return self.next_utterance(timeout=timeout)

        interpret.Session(pair, listen, self.hear, self.brain.translate, self.say, self.events.publish).run()
        self.follow_up_until = 0.0

    def publish_spending(self, tag: str) -> None:
        """Show the spending in the panel; say so once a day when a cap is near or reached."""
        self.events.publish("spending", self.budget.summary())
        alert = self.budget.alert()
        if alert:
            self.say(lang.phrase(alert, tag), tag)

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

    def ask(self, text: str, tag: str | None = None, speak: bool = False, timeout: float | None = None) -> str | None:
        """A request from `rafiq ask`, answered on the loop's thread like a typed one, so the tools (Firefox's
        one session) are shared. What Rafiq said, or None when it took longer than timeout."""
        reply: queue.Queue = queue.Queue(maxsize=1)
        self.submit("text", text=text, tag=tag, speak=speak, reply=reply)
        try:
            return reply.get(timeout=timeout)
        except queue.Empty:
            return None

    def announce(self, text: str, tag: str) -> None:
        """Say the result of a background task, without the private data in it (privacy.redact)."""
        self.submit("result", text=text, tag=tag)

    def _run_jobs(self) -> None:
        while True:
            try:
                kind, data = self.jobs.get_nowait()
            except queue.Empty:
                return
            try:
                if kind == "text":
                    self._typed(data)
                elif kind == "listen":
                    self._listen_for_request(greet=False)
                elif kind == "notify":
                    self.notify(data["text"], data["event"], data["lang"])
                elif kind == "say":
                    self.say(data["text"], data["tag"], data.get("cards"))
                elif kind == "result":
                    self.say(privacy.redact(data["text"]), data["tag"])
                elif kind == "show_again":
                    self.overlay.show_again()
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
                elif kind == "dictate":               # the shortcut: start, or stop when dictating
                    if self.dictating:
                        self.dictating = False
                        continue
                    self.dictate(self.tag)
                elif kind == "read":
                    self.read()
            except Exception as exc:
                self.events.publish("error", {"error": f"{type(exc).__name__}: {exc}"})
            self.set_state("dictating" if self.dictating else "idle")

    def _typed(self, data: dict) -> None:
        """A typed request. One from `rafiq ask` carries a reply queue that gets what Rafiq said, and is said
        out loud only with --speak."""
        text, reply = data["text"], data.get("reply")
        tag = data.get("tag") or lang.dialect(lang.guess(text, self.tag), text, self.tag)
        heard = self.events.subscribe() if reply else None
        silent = self.silent
        if reply and not data.get("speak"):
            self.silent = True
        try:
            with self.events.run():
                self.events.publish("typed", {"text": text})
                self.respond(text, tag, source="typed")
        finally:
            self.silent = silent
            if reply:
                self.events.unsubscribe(heard)
                said = []
                while not heard.empty():
                    event = heard.get_nowait()
                    if event["kind"] == "said":
                        said.append(event["text"])
                reply.put("\n".join(said))

    def offer_paper(self) -> None:
        """The timer's offer: ask, and print on a yes. Silent when there is nothing to print."""
        result = self.paper.print_today(lambda action: self.confirm(action, self.tag))
        self.events.publish("paper", {"result": result})
        if result.startswith("printed"):
            self.say(lang.phrase("paper_printed", self.tag), self.tag)
        elif result.startswith("error: printing failed"):
            self.say(lang.phrase("paper_failed", self.tag), self.tag)

    def _alarm_due(self, alarm: Alarm) -> None:
        if alarm.kind == "prayer-plan":
            self._plan_prayers()
            return
        key = "timer_done" if alarm.kind == "timer" else "reminder"
        self.submit("say", text=lang.phrase(key, alarm.tag, label=alarm.label), tag=alarm.tag,
                    cards=[{"kind": "timer", "text": alarm.label}])

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

    def _plan_prayers(self) -> None:
        """Prayer reminders for today and tomorrow, when the user has set some."""
        if not self.cfg.get("prayer", {}).get("enabled"):
            return
        from .tools import prayer

        try:
            prayer.plan_day(self.cfg, self.scheduler)
        except Exception as exc:                 # no network for the city, a wrong setting: say so in the panel
            self.events.publish("error", {"error": f"prayer reminders: {type(exc).__name__}: {exc}"})

    def _nudger(self) -> Nudger | None:
        """Calendar nudges, when turned on and everything they need is set up."""
        if not nudges.settings(self.cfg)["enabled"]:
            return None
        why = nudges.missing(self.cfg)
        if why:
            self.events.publish("error", {"error": f"calendar nudges are off: {why}"})
            return None
        from .tools import google

        opts = nudges.settings(self.cfg)
        return Nudger(opts, google.upcoming, OpenRouteService(opts["routing_key"]), user_tag=self.tag)

    def _nudge_due(self, nudge: Nudge) -> None:
        self.submit("say", text=nudge.text, tag=nudge.tag)

    def _listen_for_request(self, greet: bool = True) -> None:
        if greet:
            self.say(lang.phrase("listening", self.tag), self.tag)
        self.set_state("listening")
        audio = self.next_utterance(timeout=LISTEN_SECONDS)
        if audio is None:
            return
        with self.events.run():
            text, tag = self.hear(audio)
            self.respond(text, tag)

    # ------------------------------------------------------------------ the main loop

    def _spot_with_recordings(self, audio: np.ndarray) -> None:
        """Wake when the start of what was said matches your recordings of the name. Most speech
        that is not the name stops here, after a few hundred milliseconds of arithmetic."""
        score, tag = self.spotter.best(self.mel(audio[:int(kws.MAX_SECONDS * RATE)]))
        if score > self.spotter.threshold:
            return
        if tag and self.tag.split("-")[0] != tag:     # "يا رفيق" is answered in Arabic; Egyptian stays Egyptian
            self.tag = tag
        self.events.publish("wake", {"score": round(score, 3), "threshold": round(self.spotter.threshold, 3)})
        if len(audio) / RATE > self.name_seconds + 1.3:    # more than the name: the request came with it
            with self.events.run():
                text, tag = self.hear(audio)
                found, command = wake.find(text, self.wake_names)
                self.respond(command if found else text, tag)
        else:
            self._listen_for_request(greet=True)

    def handle_idle_utterance(self, audio: np.ndarray) -> None:
        if time.monotonic() < self.follow_up_until:      # a follow-up: no name needed
            with self.events.run():
                text, tag = self.hear(audio)
                found, rest = wake.find(text, self.wake_names)
                self.respond(rest if found else text, tag)
            return
        if self.spotter:
            self._spot_with_recordings(audio)
            return
        # The name comes first, so only the start is checked: faster, and less to mishear.
        spotted = self.wake_stt.transcribe(audio[:int(WAKE_SECONDS * RATE)], beam_size=1, hotwords=None,
                                           prompt=wake.prompt(self.wake_names))
        found, rest = wake.find(spotted.text, self.wake_names)
        if not found:
            return
        self.events.publish("wake", {"text": spotted.text})
        if len(audio) / RATE > WAKE_SECONDS or len(rest.split()) >= 2:   # "Rafiq, what's the weather?" at once
            with self.events.run():
                text, tag = self.hear(audio)
                found, command = wake.find(text, self.wake_names)
                self.respond(command if found else text, tag)
        else:
            self.tag = lang.dialect(spotted.lang, spotted.text, self.tag)
            self._listen_for_request(greet=True)

    def run(self) -> None:
        self.scheduler.start()
        self.scheduler.tick()                            # alarms that came due while Rafiq was off
        self._plan_prayers()
        if self.nudger:
            self.nudger.start(self._nudge_due)
        if self.presence:
            self.presence.start(every=float(self.cfg["presence"]["check_seconds"]))
        self.start_the_day()
        threading.Thread(target=self.brain.warm_up, name="rafiq-warm-up", daemon=True).start()
        self.mic.start()
        self.events.publish("privacy", self.privacy())
        if self.cfg["audio"]["auto_calibration"]:
            self.set_state("calibrating")
            self.calibrate()
        if self.cfg["gestures"].get("enabled"):
            started = self.gestures.start()
            if started.startswith("error"):
                self.events.publish("error", {"error": started})
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
            self.gestures.stop()
            self.mic.stop()
            self.scheduler.stop()
            if self.nudger:
                self.nudger.stop()
            if self.presence:
                self.presence.stop()

    def stop(self) -> None:
        self._stop.set()
        self.speaker.stop()
