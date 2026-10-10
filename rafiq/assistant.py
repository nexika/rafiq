"""The loop: wait for "Rafiq", hear the request, think, answer, and listen a little longer for a
follow-up without the name.

Everything that uses the microphone or the speaker happens on this one thread: requests from the
panel and alarms are queued as jobs and run here, so a confirmation question never competes with the
wake-word listener for the microphone.
"""

from __future__ import annotations

import queue
import threading
import time
from collections import deque
from dataclasses import dataclass, field

import numpy as np

from . import config as config_mod
from . import history as history_mod
from . import kws, lang, wake
from .audio import RATE, Endpointer, Microphone, SileroVAD
from .brain import Brain
from .scheduler import Alarm, Scheduler
from .stt import WAKE_PROMPT, WAKE_SECONDS, Transcriber
from .tools import load_all
from .tts import Speaker

MIN_UTTERANCE_SECONDS = 0.35
LISTEN_SECONDS = 8.0
CONFIRM_SECONDS = 10.0


@dataclass
class Context:
    """What a tool may use: settings, the scheduler and the language of the current request."""
    cfg: dict
    scheduler: Scheduler
    tag: str = "en"


@dataclass
class Events:
    """What happened, for the panel: the last 100 events, and a queue per open panel."""
    history: deque = field(default_factory=lambda: deque(maxlen=100))
    listeners: list = field(default_factory=list)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def publish(self, kind: str, data: dict, keep: bool = True) -> None:
        event = {"kind": kind, "time": time.time(), **data}
        with self.lock:
            if keep:
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
    def __init__(self, cfg: dict | None = None):
        self.cfg = cfg or config_mod.load()
        speech = self.cfg["speech"]
        self.events = Events()
        self.jobs: queue.Queue = queue.Queue()
        self.mic = Microphone()
        self.vad = SileroVAD()
        self.endpointer = Endpointer(max_seconds=speech["max_utterance_seconds"])
        self.wake_stt = Transcriber(speech["wake_model"], speech["languages"])
        self.stt = Transcriber(speech["whisper_model"], speech["languages"])
        self.speaker = Speaker(self.cfg, on_start=self._mute, on_end=self._unmute)
        self.scheduler = Scheduler(config_mod.data_dir() / "alarms.json", self._alarm_due)
        self.ctx = Context(self.cfg, self.scheduler)
        self.tools = load_all(self.cfg)
        self.brain = Brain(self.cfg, self.tools, self.confirm, on_event=self.events.publish)
        self.history = history_mod.History(config_mod.data_dir() / "history.json",
                                           self.cfg["history"]["retention_days"])
        self.tag = "en"                      # the language of the conversation so far
        self.follow_up_until = 0.0
        self.pending_answer: queue.Queue = queue.Queue()   # a yes or no clicked in the panel
        self.state = "starting"
        self._stop = threading.Event()
        # Your own recordings of the name (`rafiq enroll`), if any; else a small Whisper model listens.
        self.spotter = kws.Spotter.load()
        self.mel = kws.Mel() if self.spotter else None
        if self.spotter:   # (rafiq enroll --sensitivity refits the threshold)
            self.name_seconds = max(len(t) for t in self.spotter.templates) * 0.02

    # ------------------------------------------------------------------ state shown in the panel

    def set_state(self, state: str) -> None:
        self.state = state
        self.events.publish("state", {"state": state})

    def _mute(self) -> None:
        self.mic.muted = True
        self.set_state("speaking")

    def _unmute(self) -> None:
        time.sleep(0.15)                     # the speaker's tail would be heard as speech
        self.mic.drain()
        self.vad.reset()
        self.endpointer.reset()
        self.mic.muted = False

    # ------------------------------------------------------------------ hearing

    def next_utterance(self, timeout: float | None) -> np.ndarray | None:
        """The next thing said, or None when nothing starts before the timeout (None: wait forever)."""
        deadline = None if timeout is None else time.monotonic() + timeout
        while not self._stop.is_set():
            self._run_jobs()
            if deadline and time.monotonic() > deadline and not self.endpointer.active:
                return None
            frame = self.mic.read(timeout=0.1)
            if frame is None:
                continue
            utterance = self.endpointer.push(frame, self.vad(frame))
            if utterance is not None and len(utterance) / RATE >= MIN_UTTERANCE_SECONDS:
                return utterance
        return None

    def hear(self, audio: np.ndarray):
        self.set_state("thinking")
        heard = self.stt.transcribe(audio)
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
        self.events.publish("confirm", {"action": action, "tag": tag})
        for _attempt in range(2):
            self.say(lang.phrase("confirm", tag, action=action), tag)
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

    def respond(self, text: str, tag: str, source: str = "voice") -> None:
        """Think about one request, say the answer and keep both in the history."""
        if not text.strip():
            self.say(lang.phrase("not_heard", tag), tag)
            return
        if lang.is_forget(text):
            last = self.history.forget_last()
            if last:
                self.forget([last])
            self.say(lang.phrase("forgotten" if last else "nothing_to_forget", tag), tag)
            return
        self.tag = tag
        self.ctx.tag = tag
        self.set_state("thinking")
        answer = self.brain.think(text, tag, self.ctx)
        self.history.add(text, answer, tag, source)
        self.say(answer, lang.script_language(answer, tag))
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
                    self.respond(text, lang.dialect(lang.guess(text, self.tag), text, self.tag), source="typed")
                elif kind == "listen":
                    self._listen_for_request(greet=False)
                elif kind == "say":
                    self.say(data["text"], data["tag"])
            except Exception as exc:
                self.events.publish("error", {"error": f"{type(exc).__name__}: {exc}"})
            self.set_state("idle")

    def _alarm_due(self, alarm: Alarm) -> None:
        key = "timer_done" if alarm.kind == "timer" else "reminder"
        self.submit("say", text=lang.phrase(key, alarm.tag, label=alarm.label), tag=alarm.tag)

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
        threading.Thread(target=self.brain.warm_up, name="rafiq-warm-up", daemon=True).start()
        self.mic.start()
        self.events.publish("privacy", self.privacy())
        self.set_state("idle")
        try:
            while not self._stop.is_set():
                audio = self.next_utterance(timeout=None)
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

    def stop(self) -> None:
        self._stop.set()
        self.speaker.stop()
