"""The morning paper: print today's, when the user asks for it."""

from __future__ import annotations

from .. import paper
from . import tool


@tool("print_paper", "Print today's morning paper on the printer. It prints at most once a day.",
      confirm=paper.ACTION)
def print_paper(ctx=None) -> str:
    # The brain has already asked; the answer was yes.
    return paper.MorningPaper(ctx.cfg).print_today(lambda _action: True)
