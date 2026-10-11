"""Keep private data out of what Rafiq says on its own and out of its log.

redact() replaces email addresses, keys and tokens, phone numbers and amounts of money with a mark
("[email]", "[key]", "[phone]", "[amount]"). It runs on every log line and on results from background
tasks before they are said aloud. Answers to a request and your own reminders are not redacted: you
asked for them.

The log is data/logs/rafiq.log, a new file each day, and files older than [privacy] log_days (30 by
default) are deleted.
"""

from __future__ import annotations

import logging
import re
import time
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

LOG_NAME = "rafiq.log"

_NUMBER = r"\d+(?:[.,  ]\d{3})*(?:[.,]\d+)?"
_CURRENCY = (r"[$€£¥]|USD|EUR|GBP|EGP|SAR|AED|MAD|dollars?|euros?|pounds?|"
             r"جنيه|جنيهات|دولار|دولارات|ريال|درهم|دراهم|يورو")

# In this order: a key is replaced before its digits can look like a phone number.
_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"(?i)\b(api[_-]?key|access[_-]?token|token|secret|password|passwd|pwd)(\s*[:=]\s*)\S+"),
     r"\1\2[key]"),
    (re.compile(r"(?i)\b(bearer)\s+[\w.~+/=-]+"), r"\1 [key]"),
    (re.compile(r"\b(?:sk-[\w-]{16,}|gh[pousr]_\w{20,}|github_pat_\w{20,}|xox[abpr]-[\w-]{10,}"
                r"|AIza[\w-]{35}|AKIA[0-9A-Z]{16})"), "[key]"),
    (re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"), "[email]"),
    # Any long run of letters and digits: hashes, ids, keys from services not listed above.
    (re.compile(r"(?<![\w-])(?=[A-Za-z0-9_-]*\d)(?=[A-Za-z0-9_-]*[A-Za-z])[A-Za-z0-9_-]{32,}(?![\w-])"), "[key]"),
    (re.compile(rf"(?:[$€£¥]\s?{_NUMBER}|{_NUMBER}\s?(?:{_CURRENCY}))(?!\w)"), "[amount]"),
]
_PHONE = re.compile(r"(?<!\w)[+(]?\d(?:[ ().-]{0,2}\d){7,14}(?!\w)")
_DATE = re.compile(r"\d{4}-\d\d-\d\d")


def _phone(match: re.Match) -> str:
    text = match.group()
    digits = sum(c.isdigit() for c in text)
    if _DATE.match(text) or digits < (8 if text.startswith("+") else 9):
        return text
    return "[phone]"


def redact(text: str) -> str:
    """text with email addresses, keys, phone numbers and amounts replaced by a mark."""
    for pattern, mark in _PATTERNS:
        text = pattern.sub(mark, text)
    return _PHONE.sub(_phone, text)


class RedactFilter(logging.Filter):
    """Redacts each record's message, with its arguments filled in, and its traceback."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg, record.args = redact(record.getMessage()), None
        if record.exc_info:
            record.exc_text = redact(logging.Formatter().formatException(record.exc_info))
            record.exc_info = None
        return True


def prune_logs(folder: Path, days: int, now: float | None = None) -> list[Path]:
    """Delete the log files older than days; returns them."""
    cutoff = (now if now is not None else time.time()) - days * 86400
    gone = []
    for path in sorted(folder.glob(LOG_NAME + "*")):
        try:
            if path.is_file() and path.stat().st_mtime < cutoff:
                path.unlink()
                gone.append(path)
        except OSError:
            pass
    return gone


_handlers: list[logging.Handler] = []


def start_logging(folder: Path, days: int) -> logging.Logger:
    """Write Rafiq's log, redacted, to folder/rafiq.log: a new file each day, kept for days."""
    folder.mkdir(parents=True, exist_ok=True)
    prune_logs(folder, days)
    handler = TimedRotatingFileHandler(folder / LOG_NAME, when="midnight", backupCount=days, encoding="utf-8")
    handler.addFilter(RedactFilter())
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logger = logging.getLogger("rafiq")
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)
    _handlers.append(handler)
    return logger


def stop_logging() -> None:
    logger = logging.getLogger("rafiq")
    while _handlers:
        handler = _handlers.pop()
        logger.removeHandler(handler)
        handler.close()
