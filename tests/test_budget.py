from datetime import datetime
from types import SimpleNamespace

import pytest

from rafiq import config
from rafiq.brain import Brain
from rafiq.budget import Budget
from rafiq.tools import REGISTRY, load_all
from rafiq.tts import Speaker


def at(day: int, hour: int = 12, month: int = 10) -> float:
    return datetime(2026, month, day, hour).astimezone().timestamp()


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def make_budget(daily=0.0, monthly=0.0, clock=None):
    cfg = config.load()
    cfg["budget"].update(daily_cap=daily, monthly_cap=monthly)
    cfg["budget"]["token_prices"] = {"claude-haiku-5-5": {"input": 1.0, "output": 5.0}}
    cfg["budget"]["character_prices"] = {"azure": 15.0}
    return Budget(cfg, clock=clock or Clock(at(10)))


def test_tokens_and_characters_are_priced_from_the_config():
    budget = make_budget()
    assert budget.charge_tokens("claude-haiku-5-5", 1_000_000, 100_000) == pytest.approx(1.5)
    assert budget.charge_characters("azure", 200_000) == pytest.approx(3.0)
    assert budget.spent("day") == pytest.approx({"claude": 1.5, "azure": 3.0})


def test_a_model_without_a_price_is_counted_but_costs_nothing():
    budget = make_budget()
    assert budget.charge_tokens("claude-unknown", 1000, 1000) == 0
    assert budget.spent("day") == {"claude": 0}


def test_spending_is_kept_by_day_and_by_month_and_survives_a_restart():
    clock = Clock(at(9))
    make_budget(clock=clock).charge_characters("azure", 100_000)    # 1.5
    clock.t = at(10)
    make_budget(clock=clock).charge_characters("azure", 100_000)
    clock.t = at(1, month=11)
    make_budget(clock=clock).charge_characters("azure", 100_000)
    clock.t = at(10)
    budget = make_budget(clock=clock)
    assert budget.spent("day") == pytest.approx({"azure": 1.5})
    assert budget.spent("month") == pytest.approx({"azure": 3.0})


def test_a_warning_at_80_percent_then_offline_at_the_cap_until_the_next_day():
    clock = Clock(at(10, 9))
    budget = make_budget(daily=1.0, clock=clock)
    budget.charge_characters("azure", 50_000)                         # 0.75
    assert budget.status() == "ok" and budget.alert() is None
    budget.charge_characters("azure", 5_000)                          # 0.825
    assert budget.status() == "warning" and budget.allows_paid()
    assert budget.alert() == "budget_warning"
    assert budget.alert() is None                                     # said once
    budget.charge_characters("azure", 20_000)                         # 1.125
    assert budget.status() == "capped" and not budget.allows_paid()
    assert budget.alert() == "budget_capped" and budget.alert() is None
    clock.t = at(11, 0)
    assert budget.status() == "ok" and budget.allows_paid()


def test_the_monthly_cap_counts_every_day_of_the_month():
    clock = Clock(at(1))
    budget = make_budget(monthly=2.0, clock=clock)
    budget.charge_characters("azure", 100_000)                        # 1.5
    clock.t = at(2)
    assert budget.status() == "ok"
    budget.charge_characters("azure", 40_000)                         # 2.1 this month
    assert budget.status() == "capped"


def test_no_cap_means_never_offline():
    budget = make_budget()
    budget.charge_characters("azure", 10_000_000)
    assert budget.status() == "ok" and budget.allows_paid()


def test_the_spending_tool_reports_today_and_this_month():
    budget = make_budget(daily=2.0)
    budget.charge_tokens("claude-haiku-5-5", 1_000_000, 0)
    load_all({})
    text = REGISTRY["get_spending"].run({}, SimpleNamespace(budget=budget))
    assert "today" in text.lower() and "month" in text.lower()
    assert "claude" in text and "1.00" in text and "50%" in text


# ---------------------------------------------------------------------- the brain and the voice

def claude_response(text, usage):
    block = SimpleNamespace(type="text", text=text, model_dump=lambda: {"type": "text", "text": text})
    return SimpleNamespace(content=[block], usage=SimpleNamespace(input_tokens=usage[0], output_tokens=usage[1]))


class FakeClaude:
    def __init__(self):
        self.calls = 0
        self.messages = self

    def create(self, **kwargs):
        self.calls += 1
        return claude_response("Hello.", (2_000, 400))


def test_each_claude_response_is_charged():
    budget = make_budget()
    cfg = config.load()
    cfg["brain"]["mode"] = "claude"
    brain = Brain(cfg, {}, lambda a, t: True, budget=budget)
    brain._claude = FakeClaude()
    assert brain.think("hi", "en") == "Hello."
    assert budget.spent("day")["claude"] == pytest.approx(0.002 + 0.002)


def test_at_the_cap_the_brain_goes_offline_even_in_claude_mode():
    budget = make_budget(daily=0.01)
    budget.charge_characters("azure", 1_000)                          # 0.015: capped
    cfg = config.load()
    cfg["brain"]["mode"] = "claude"
    cfg["anthropic_api_key"] = "k"
    assert Brain(cfg, {}, lambda a, t: True, budget=budget).backends() == ["ollama"]


def test_azure_characters_are_charged_and_azure_is_skipped_at_the_cap(monkeypatch, tmp_path):
    import httpx

    monkeypatch.setattr(httpx, "post", lambda *a, **k: SimpleNamespace(raise_for_status=lambda: None,
                                                                         content=b"mp3"))
    budget = make_budget(daily=0.01)
    cfg = config.load()
    cfg["voice"].update(azure_key="k", azure_region="westeurope")
    speaker = Speaker(cfg, budget=budget)
    assert speaker.engines()[0] == "azure"
    path, _ = speaker.synthesize("x" * 500, "en", tmp_path)            # 0.0075
    assert path.suffix == ".mp3" and budget.spent("day")["azure"] == pytest.approx(0.0075)
    speaker.synthesize("x" * 500, "en", tmp_path)                      # 0.015: capped
    assert "azure" not in speaker.engines()


def test_rafiq_says_once_when_the_cap_is_near_and_the_panel_gets_a_line():
    from rafiq.assistant import Assistant, Events

    budget = make_budget(daily=1.0)
    budget.charge_characters("azure", 60_000)                         # 0.9
    said, fake = [], SimpleNamespace(budget=budget, events=Events())
    fake.say = lambda text, tag: said.append((text, tag))
    Assistant.publish_spending(fake, "fr")
    Assistant.publish_spending(fake, "fr")
    assert said == [("Attention : tu as utilisé quatre-vingts pour cent de ton plafond de dépenses.", "fr")]
    line = fake.events.history[-1]
    assert line["kind"] == "spending" and line["today"] == 0.9 and line["status"] == "warning"
