"""What Rafiq has spent on paid services, and the caps that stop it.

Claude is charged by the tokens each response reports, Azure voices by the characters read aloud, at
the prices in the config. Spending is kept by day in data/spending.json. At 80% of a daily or
monthly cap Rafiq warns once; at the cap it goes offline (Ollama and Piper) until the next day, or
the next month for the monthly cap.
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime
from pathlib import Path

from . import config as config_mod

LEVELS = ("ok", "warning", "capped")


class Budget:
    def __init__(self, cfg: dict, path: Path | None = None, clock=time.time):
        self.cfg = cfg["budget"]
        self.path = path or config_mod.data_dir() / "spending.json"
        self.clock = clock
        self.lock = threading.Lock()
        self.days: dict[str, dict[str, dict]] = {}     # "2026-10-10" -> provider -> cost and units
        self.alerted: dict[str, str] = {}               # day -> the last level announced that day
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self.path.read_text())
            self.days, self.alerted = dict(data.get("days", {})), dict(data.get("alerted", {}))
        except (OSError, ValueError, AttributeError):
            pass

    def _save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"days": self.days, "alerted": self.alerted}))
        tmp.replace(self.path)

    def _today(self) -> str:
        return datetime.fromtimestamp(self.clock()).astimezone().strftime("%Y-%m-%d")

    # ------------------------------------------------------------------ charging

    def _charge(self, provider: str, cost: float, units: dict[str, int]) -> float:
        with self.lock:
            entry = self.days.setdefault(self._today(), {}).setdefault(provider, {"cost": 0})
            entry["cost"] += cost
            for key, n in units.items():
                entry[key] = entry.get(key, 0) + n
            self._save()
        return cost

    def charge_tokens(self, model: str, input_tokens: int, output_tokens: int) -> float:
        """A Claude response; prices are per million tokens. A model with no price is counted free."""
        price = self.cfg.get("token_prices", {}).get(model) or {}
        cost = (input_tokens * price.get("input", 0) + output_tokens * price.get("output", 0)) / 1e6
        return self._charge("claude", cost, {"input_tokens": input_tokens, "output_tokens": output_tokens})

    def charge_characters(self, provider: str, characters: int) -> float:
        """Text read aloud by a paid voice; prices are per million characters."""
        cost = characters * self.cfg.get("character_prices", {}).get(provider, 0) / 1e6
        return self._charge(provider, cost, {"characters": characters})

    # ------------------------------------------------------------------ reading

    def spent(self, period: str = "day") -> dict[str, float]:
        """Cost per provider today ("day") or this month ("month")."""
        today = self._today()
        prefix = today if period == "day" else today[:7]
        totals: dict[str, float] = {}
        with self.lock:
            for day, providers in self.days.items():
                if day.startswith(prefix):
                    for provider, entry in providers.items():
                        totals[provider] = totals.get(provider, 0) + entry.get("cost", 0)
        return totals

    def _ratio(self) -> float:
        """The share of the fuller cap used; 0 when no cap is set."""
        ratios = [sum(self.spent(period).values()) / cap for period, cap in
                  (("day", self.cfg.get("daily_cap", 0)), ("month", self.cfg.get("monthly_cap", 0))) if cap > 0]
        return max(ratios, default=0.0)

    def status(self) -> str:
        ratio = self._ratio()
        if ratio >= 1:
            return "capped"
        return "warning" if ratio >= self.cfg.get("warn_at", 0.8) else "ok"

    def allows_paid(self) -> bool:
        return self.status() != "capped"

    def alert(self) -> str | None:
        """"budget_warning" or "budget_capped" the first time that level is reached in a day, else None."""
        level, today = self.status(), self._today()
        if level == "ok" or LEVELS.index(level) <= LEVELS.index(self.alerted.get(today, "ok")):
            return None
        with self.lock:
            self.alerted = {today: level}
            self._save()
        return f"budget_{level}"

    def summary(self) -> dict:
        """For the panel line."""
        return {"today": round(sum(self.spent("day").values()), 4),
                "month": round(sum(self.spent("month").values()), 4),
                "daily_cap": self.cfg.get("daily_cap", 0), "monthly_cap": self.cfg.get("monthly_cap", 0),
                "currency": self.cfg.get("currency", "USD"), "status": self.status()}

    def report(self) -> str:
        """For the spending tool: totals with their parts, and how much of each cap is used."""
        currency = self.cfg.get("currency", "USD")
        parts = []
        for period, label, cap in (("day", "Today", self.cfg.get("daily_cap", 0)),
                                   ("month", "This month", self.cfg.get("monthly_cap", 0))):
            spent = self.spent(period)
            total = sum(spent.values())
            detail = ", ".join(f"{p} {c:.2f}" for p, c in sorted(spent.items())) or "nothing"
            line = f"{label}: {total:.2f} {currency} ({detail})"
            if cap > 0:
                line += f", {total / cap:.0%} of the {cap:.2f} {currency} cap"
            parts.append(line + ".")
        if self.status() == "capped":
            parts.append("A cap is reached: Rafiq is using only its offline brain and voice.")
        return " ".join(parts)
