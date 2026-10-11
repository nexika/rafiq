"""The models page: Ollama models (what fits this computer, install, benchmark, delete, activate) and
the Whisper models that turn speech into text.

Only names from the catalogue can be installed, and only installed ones benchmarked, activated or
deleted: the panel can't be made to pull or remove anything else. Installing and benchmarking take a
while, so they run in the background and report back as "model" events.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from . import config as config_mod


@dataclass(frozen=True)
class Choice:
    name: str
    size_gb: float     # the download, about what it takes in memory
    note: str


CATALOGUE = [
    Choice("qwen2.5:3b", 1.9, "fast, less careful"),
    Choice("qwen2.5:7b", 4.7, "the default: good tool use in six languages"),
    Choice("qwen2.5:14b", 9.0, "more careful, needs a strong computer"),
    Choice("qwen2.5:32b", 20.0, "best answers, needs a lot of memory"),
    Choice("llama3.2:3b", 2.0, "fast, weaker in Arabic"),
    Choice("llama3.1:8b", 4.9, "good in English and European languages"),
    Choice("mistral-nemo:12b", 7.1, "good in French, Spanish and German"),
    Choice("gemma3:4b", 3.3, "small, many languages"),
    Choice("gemma3:12b", 8.1, "many languages, needs a strong computer"),
]
CATALOGUE_NAMES = {c.name for c in CATALOGUE}

# faster-whisper sizes and their download in GB.
WHISPER = {"tiny": 0.08, "base": 0.15, "small": 0.5, "medium": 1.5, "large-v3": 3.1}

BENCHMARK_PROMPT = "In two sentences, say what a voice assistant is."


def fits(size_gb: float, ram_gb: float) -> bool:
    """A model fits when it, its working memory (about a fifth more) and 1.5 GB for the rest of
    Rafiq and the desktop all fit in the computer's memory."""
    return size_gb * 1.2 + 1.5 <= ram_gb


def total_ram_gb() -> float:
    import psutil

    return round(psutil.virtual_memory().total / 1e9, 1)


def whisper_cache() -> Path:
    """Where faster-whisper's downloads land (the Hugging Face cache)."""
    if os.environ.get("HF_HUB_CACHE"):
        return Path(os.environ["HF_HUB_CACHE"])
    return Path(os.environ.get("HF_HOME") or Path.home() / ".cache" / "huggingface") / "hub"


class Ollama:
    """The few calls of Ollama's API the models page needs."""

    def __init__(self, url: str):
        self.url = url

    def tags(self) -> list[str]:
        import httpx

        resp = httpx.get(f"{self.url}/api/tags", timeout=3)
        resp.raise_for_status()
        return [m["name"] for m in resp.json().get("models", [])]

    def pull(self, name: str) -> None:
        import httpx

        resp = httpx.post(f"{self.url}/api/pull", json={"model": name, "stream": False}, timeout=3600)
        resp.raise_for_status()

    def delete(self, name: str) -> None:
        import httpx

        resp = httpx.request("DELETE", f"{self.url}/api/delete", json={"model": name}, timeout=30)
        resp.raise_for_status()

    def generate(self, name: str, prompt: str) -> dict:
        import httpx

        resp = httpx.post(f"{self.url}/api/generate", timeout=600, json={
            "model": name, "prompt": prompt, "stream": False, "options": {"num_predict": 80}})
        resp.raise_for_status()
        return resp.json()


def _background(fn: Callable[[], None]) -> None:
    threading.Thread(target=fn, name="rafiq-models", daemon=True).start()


class Models:
    def __init__(self, cfg: dict, ollama=None, ram_gb: float | None = None, whisper_cache: Path | None = None,
                 on_event: Callable[[str, dict], None] | None = None,
                 on_whisper: Callable[[str], None] | None = None,
                 background: Callable[[Callable[[], None]], None] = _background):
        self.cfg = cfg
        self.ollama = ollama or Ollama(cfg["brain"]["ollama_url"])
        self.ram_gb = ram_gb
        self.cache = whisper_cache
        self.on_event = on_event or (lambda kind, data: None)
        self.on_whisper = on_whisper or (lambda size: None)
        self.background = background
        self.benchmarks: dict[str, float] = {}     # model -> tokens per second, this run

    def _ram(self) -> float:
        if self.ram_gb is None:
            self.ram_gb = total_ram_gb()
        return self.ram_gb

    def installed(self) -> list[str] | None:
        """The installed models, or None when Ollama does not answer."""
        try:
            return self.ollama.tags()
        except Exception:
            return None

    # ------------------------------------------------------------------ reading

    def ollama_page(self) -> dict:
        installed = self.installed()
        have = set(installed or [])
        active, ram = self.cfg["brain"]["ollama_model"], self._ram()
        rows = [{"name": c.name, "size_gb": c.size_gb, "note": c.note, "fits": fits(c.size_gb, ram)}
                for c in CATALOGUE]
        rows += [{"name": name, "size_gb": None, "note": "installed by hand", "fits": True}
                 for name in sorted(have - CATALOGUE_NAMES)]
        for row in rows:
            row.update(installed=row["name"] in have, active=row["name"] == active,
                       tokens_per_second=self.benchmarks.get(row["name"]))
        return {"reachable": installed is not None, "active": active, "ram_gb": ram, "models": rows}

    def whisper_page(self) -> dict:
        cache = self.cache or whisper_cache()
        active = self.cfg["speech"]["whisper_model"]
        return {"active": active, "models": [
            {"name": size, "size_gb": gb, "active": size == active,
             "downloaded": (cache / f"models--Systran--faster-whisper-{size}").is_dir()}
            for size, gb in WHISPER.items()]}

    # ------------------------------------------------------------------ changing

    def _installed_name(self, name) -> str:
        if not isinstance(name, str) or name not in (self.installed() or []):
            raise ValueError(f"{name!r} is not an installed model")
        return name

    def _run(self, action: str, name: str, work: Callable[[], str]) -> None:
        def job():
            try:
                result, ok = work(), True
            except Exception as exc:
                result, ok = f"{type(exc).__name__}: {exc}", False
            self.on_event("model", {"action": action, "name": name, "ok": ok, "result": result})
        self.background(job)

    def install(self, name) -> None:
        if name not in CATALOGUE_NAMES:
            raise ValueError(f"{name!r} is not in the catalogue")

        def work():
            self.ollama.pull(name)
            return "installed"
        self._run("install", name, work)

    def benchmark(self, name) -> None:
        name = self._installed_name(name)

        def work():
            out = self.ollama.generate(name, BENCHMARK_PROMPT)
            seconds = (out.get("eval_duration") or 0) / 1e9
            speed = round((out.get("eval_count") or 0) / seconds, 1) if seconds else 0.0
            self.benchmarks[name] = speed
            return f"{speed} tokens a second"
        self._run("benchmark", name, work)

    def delete(self, name) -> None:
        name = self._installed_name(name)
        if name == self.cfg["brain"]["ollama_model"]:
            raise ValueError("the active model can't be deleted: activate another one first")

        def work():
            self.ollama.delete(name)
            self.benchmarks.pop(name, None)
            return "deleted"
        self._run("delete", name, work)

    def activate(self, name) -> None:
        """Rafiq answers with this model from the next request on (the brain reads the same settings)."""
        name = self._installed_name(name)
        self.cfg["brain"]["ollama_model"] = name
        config_mod.save_choice("brain", "ollama_model", name)
        self.on_event("model", {"action": "activate", "name": name, "ok": True, "result": "active"})

    def activate_whisper(self, size) -> None:
        """Hear with this Whisper size from now on; it downloads the first time it is used."""
        if size not in WHISPER:
            raise ValueError(f"{size!r} is not a Whisper model")
        self.cfg["speech"]["whisper_model"] = size
        config_mod.save_choice("speech", "whisper_model", size)
        self.on_whisper(size)
        self.on_event("model", {"action": "activate", "name": f"whisper {size}", "ok": True, "result": "active"})
