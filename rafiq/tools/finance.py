"""Money: the subscriptions kept on the panel's Subscriptions page. Read only when the user asks."""

from __future__ import annotations

from .. import subscriptions
from . import tool


@tool("subscriptions", "The user's subscriptions: monthly and yearly totals, each one with its next charge, "
      "and alerts (charged tomorrow, amount changed). Only when the user asks about them.")
def list_subscriptions() -> str:
    return "Subscriptions (the user's own records):\n" + subscriptions.describe(subscriptions.default_store())
