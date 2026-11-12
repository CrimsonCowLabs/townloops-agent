"""The town's one Telegram sender (best-effort; silent on failure).

Standard library only, so an alert still goes out from a cron interpreter
that lacks the skill deps — the nightly digest died on a missing import for
weeks and took its alerts with it. `engine.orchestrator._telegram_notify`
(the older entry point most skills call) delegates here.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request


def telegram(text: str) -> bool:
    """Send `text` to $TELEGRAM_CHAT_ID; False when unset or it didn't go."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not (token and chat):
        return False
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=json.dumps({"chat_id": chat, "text": text[:4000]}).encode(),
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10):
            return True
    except (urllib.error.URLError, OSError, ValueError):
        return False


def town() -> str:
    return os.environ.get("TOWNLOOPS_TOWN_SLUG", "?")
