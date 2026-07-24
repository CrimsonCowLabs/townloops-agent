"""Structured JSON logging for the town agent.

Replaces `print(..., file=sys.stderr)` scattered throughout `engine/` and the
skills. Under docker the stream lands in `docker logs`; under systemd it's
picked up by journald. In both cases the JSON envelope makes it queryable.

Usage:

    from engine.log import get_logger
    log = get_logger("enrichment")
    log.warning("firecrawl rejected key", extra={"http_status": 401, "url": url})

Extras become top-level fields in the emitted JSON. The record's own attribute
names (`msg`, `levelname`, `name`, etc.) are protected — `extras` keys that
collide with them are automatically prefixed with `x_` to preserve both.

Env:
  TOWNLOOPS_LOG_LEVEL   default INFO. One of DEBUG, INFO, WARNING, ERROR.
  TOWNLOOPS_LOG_FILE    optional; if set, ALSO write to that file (append).
                        The stream handler on stdout always runs.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any


_CONFIGURED = False

_RESERVED_ATTRS = frozenset({
    "args", "asctime", "created", "exc_info", "exc_text", "filename",
    "funcName", "levelname", "levelno", "lineno", "message", "module",
    "msecs", "msg", "name", "pathname", "process", "processName",
    "relativeCreated", "stack_info", "thread", "threadName", "taskName",
})


class JSONFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc)
                    .isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "name": record.name,
            "msg": record.getMessage(),
        }
        town = os.environ.get("TOWNLOOPS_TOWN_SLUG")
        if town:
            payload["town"] = town
        # Anything the caller passed via `extra={...}` lands on the record as
        # a plain attribute. Copy those into the payload, protecting reserved
        # LogRecord names.
        for key, value in record.__dict__.items():
            if key in _RESERVED_ATTRS or key.startswith("_"):
                continue
            if key in payload:
                payload[f"x_{key}"] = value
            else:
                payload[key] = value
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        try:
            return json.dumps(payload, default=str, ensure_ascii=False)
        except (TypeError, ValueError):
            # Fallback: emit the message alone if a non-serializable extra sneaks in.
            return json.dumps({"ts": payload["ts"], "level": payload["level"],
                                "name": payload["name"], "msg": payload["msg"]})


def _configure() -> None:
    global _CONFIGURED
    if _CONFIGURED:
        return
    root = logging.getLogger("townloops")
    root.setLevel(os.environ.get("TOWNLOOPS_LOG_LEVEL", "INFO").upper())
    fmt = JSONFormatter()

    stream = logging.StreamHandler(stream=sys.stdout)
    stream.setFormatter(fmt)
    root.addHandler(stream)

    log_file = os.environ.get("TOWNLOOPS_LOG_FILE")
    if log_file:
        try:
            fh = logging.FileHandler(log_file)
            fh.setFormatter(fmt)
            root.addHandler(fh)
        except OSError as exc:
            # Can't create/open — fall back to stdout-only, tell someone.
            root.warning("log file open failed",
                          extra={"path": log_file, "err": str(exc)})

    # Don't propagate to the Python root logger (Hermes may install its own
    # handlers that would double-print).
    root.propagate = False
    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    """Return `townloops.<name>` logger, wired to JSON on stdout."""
    _configure()
    return logging.getLogger(f"townloops.{name}")
