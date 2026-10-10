"""What Rafiq has spent on paid services (Claude, Azure voices), today and this month."""

from __future__ import annotations

from . import tool


@tool("get_spending", "What Rafiq has spent on paid services (the Claude model, Azure voices) today and this "
      "month, per service, and how much of the spending caps is used.")
def get_spending(ctx=None) -> str:
    budget = getattr(ctx, "budget", None)
    if budget is None:
        return "error: spending is not tracked here"
    return budget.report()
