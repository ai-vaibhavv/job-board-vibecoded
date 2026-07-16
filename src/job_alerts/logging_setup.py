"""Structured logging with secret redaction.

The redaction filter is the important part. The spec forbids logging webhook
URLs, API keys and authorization headers, and a filter enforces that centrally
instead of trusting every future `logger.debug` call to remember. It applies to
third-party loggers too — httpx logs full request URLs at DEBUG, which would
otherwise leak the search API key in the query string.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from typing import Any

# Each pattern keeps the identifying prefix and masks the secret itself, so logs
# stay debuggable ("the webhook 400'd") without being exploitable.
_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(https://discord(?:app)?\.com/api/webhooks/)\S+", re.I), r"\1<redacted>"),
    (
        re.compile(
            r"([?&](?:key|api_key|apikey|token|access_token|subscription-key)=)[^&\s]+", re.I
        ),
        r"\1<redacted>",
    ),
    (re.compile(r"(X-Subscription-Token['\"]?\s*[:=]\s*['\"]?)[^\s'\",]+", re.I), r"\1<redacted>"),
    (
        re.compile(r"(Ocp-Apim-Subscription-Key['\"]?\s*[:=]\s*['\"]?)[^\s'\",]+", re.I),
        r"\1<redacted>",
    ),
    (
        re.compile(r"(Authorization['\"]?\s*[:=]\s*['\"]?(?:Bearer|Basic)?\s*)[^\s'\",]+", re.I),
        r"\1<redacted>",
    ),
    (re.compile(r"(Cookie['\"]?\s*[:=]\s*['\"]?)[^\n'\"]+", re.I), r"\1<redacted>"),
)


def redact(text: str) -> str:
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    return text


class RedactingFilter(logging.Filter):
    """Masks secrets in every record, whoever emitted it."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = redact(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {k: _redact_value(v) for k, v in record.args.items()}
            else:
                record.args = tuple(_redact_value(a) for a in record.args)
        return True


def _redact_value(value: Any) -> Any:
    return redact(value) if isinstance(value, str) else value


class JsonFormatter(logging.Formatter):
    """One JSON object per line, for shipping to a log aggregator."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "message": redact(record.getMessage()),
        }
        if record.exc_info:
            payload["exception"] = redact(self.formatException(record.exc_info))
        for key, value in record.__dict__.items():
            if key not in _STANDARD_ATTRS and not key.startswith("_"):
                payload[key] = _redact_value(value)
        return json.dumps(payload, ensure_ascii=False, default=str)


_STANDARD_ATTRS = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "message",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "thread",
        "threadName",
        "taskName",
    }
)


class TextFormatter(logging.Formatter):
    """Readable console output."""

    def __init__(self) -> None:
        super().__init__(
            fmt="%(asctime)s  %(levelname)-7s %(name)-28s %(message)s",
            datefmt="%H:%M:%S",
        )


def configure_logging(level: str = "INFO", fmt: str = "text") -> None:
    """Install handlers on the root logger. Safe to call more than once."""
    root = logging.getLogger()
    for handler in root.handlers[:]:
        root.removeHandler(handler)

    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(JsonFormatter() if fmt == "json" else TextFormatter())
    handler.addFilter(RedactingFilter())

    root.addHandler(handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    # httpx logs every request URL at INFO, which is noisy and (for the search
    # API) key-bearing. The filter would mask it, but quieting it is better.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("apscheduler").setLevel(logging.WARNING)
