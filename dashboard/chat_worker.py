#!/usr/bin/env python3
"""Spawned chat worker — POST one operator turn to Hermes' api_server,
write the reply back into the placeholder `agent` row.

Invoked from `dashboard/skills.spawn_chat_worker(op_id, placeholder_id)`
as:

    python3 -m dashboard.chat_worker <op_message_id> <placeholder_message_id>

Runs in the dashboard container's env — reads `HERMES_API_URL` and
`HERMES_API_KEY` from process env (wired by compose). Fails placeholder
with a human-readable body on every error path so the panel never sits
in "thinking…" forever.
"""

from __future__ import annotations

import json
import os
import re
import sys

import requests

from dashboard import chat_db


# Whitelist of intent kinds Hermes is allowed to propose via a fenced JSON
# block in its reply. Anything else is dropped silently — the widget just
# doesn't appear, and the reply text still renders. Kept in sync with
# `dashboard.intent_handlers.ALLOWED_KINDS`; duplicated here so the worker
# doesn't need the intent_handlers import.
_ALLOWED_PROPOSAL_KINDS = frozenset({"decline_events", "approve_events",
                                    "promote_businesses", "decline_businesses"})

# `.*?` non-greedy so we can find the LAST fenced json block if Hermes emits
# more than one (unlikely but not forbidden). re.DOTALL to span newlines.
_JSON_FENCE_RE = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL | re.IGNORECASE)


def _extract_proposal(reply_text: str) -> tuple[str, str | None]:
    """Look for a trailing ```json {...} ``` block in Hermes' reply.

    Returns (visible_body, tool_call_json_string_or_None). The visible
    body has the fenced block stripped so the operator sees clean prose,
    not the raw JSON. If parsing fails / kind isn't whitelisted, we
    return the original text unchanged with no tool_call.
    """
    matches = list(_JSON_FENCE_RE.finditer(reply_text))
    if not matches:
        return (reply_text, None)
    # Take the LAST fence — if Hermes wrote examples earlier in prose,
    # the actionable proposal is the closing one.
    m = matches[-1]
    try:
        obj = json.loads(m.group(1))
    except (TypeError, ValueError):
        return (reply_text, None)
    if not isinstance(obj, dict):
        return (reply_text, None)

    # Accept two shapes for the fenced block:
    #   (A) wrapped:  {"proposal": {kind, uids, ...}, "preview": [...]}
    #   (B) flat:     {kind, uids, count, filters}   (what Hermes tends to
    #                                                 write when paraphrasing)
    if isinstance(obj.get("proposal"), dict):
        proposal = obj["proposal"]
        preview  = obj.get("preview") if isinstance(obj.get("preview"), list) else []
    elif "kind" in obj and "uids" in obj:
        proposal = obj
        preview  = obj.get("preview") if isinstance(obj.get("preview"), list) else []
    else:
        return (reply_text, None)

    if proposal.get("kind") not in _ALLOWED_PROPOSAL_KINDS:
        return (reply_text, None)
    uids = proposal.get("uids")
    if not isinstance(uids, list) or not uids:
        return (reply_text, None)

    # Wrap in an envelope with a `state` machine the confirm/cancel
    # routes update in place. Store the whole envelope (not just the
    # proposal) so those routes don't need a separate table.
    envelope = {
        "proposal": proposal,
        "preview":  preview,
        "state":    "proposed",
    }
    stripped = (reply_text[: m.start()] + reply_text[m.end():]).strip()
    if not stripped:
        stripped = "(proposal only — see widget below)"
    return (stripped, json.dumps(envelope))


HERMES_API_URL = (os.environ.get("HERMES_API_URL") or "").rstrip("/")
HERMES_API_KEY = os.environ.get("HERMES_API_KEY") or ""

# Hermes tool-invoking turns have been observed at 60-90s; give ourselves
# headroom for a slow LLM cycle before we surface a timeout to the operator.
TIMEOUT_S = 180


def _fail(placeholder_id: int, body: str, err: str) -> None:
    """Flip the placeholder row from processing → failed, iff it's still
    processing (orphan sweep may have gotten there first)."""
    chat_db.update_message_status(
        placeholder_id,
        body=body,
        status="failed",
        error=err[:1000],
        expect_status="processing",
    )


def _deliver(placeholder_id: int, body: str, *, thread_id: str,
             session_id: str, tool_call_json: str | None = None) -> None:
    """Success path. If the orphan sweep already flipped the placeholder
    to 'failed', fall back to inserting a fresh agent row so the reply
    is never dropped."""
    n = chat_db.update_message_status(
        placeholder_id,
        body=body,
        status="delivered",
        tool_call_json=tool_call_json,
        expect_status="processing",
    )
    if n == 0:
        chat_db.insert_message(
            "agent", body,
            thread_id=thread_id,
            hermes_session_id=session_id,
            tool_call_json=tool_call_json,
            status="delivered",
        )


def main() -> int:
    if len(sys.argv) < 3:
        print("usage: python3 -m dashboard.chat_worker <op_message_id> "
              "<placeholder_message_id>", file=sys.stderr)
        return 2

    try:
        op_id = int(sys.argv[1])
        placeholder_id = int(sys.argv[2])
    except ValueError:
        print("message ids must be integers", file=sys.stderr)
        return 2

    op_msg = chat_db.get_message(op_id)
    if not op_msg:
        _fail(placeholder_id, "Original message not found.",
              f"op_message_id={op_id} missing")
        return 1

    thread_id = op_msg["thread_id"]

    if not HERMES_API_URL or not HERMES_API_KEY:
        _fail(placeholder_id,
              "Hermes API not configured for this dashboard — "
              "natural-language chat is unavailable. Use `/help` for the "
              "slash-command fast path.",
              "HERMES_API_URL / HERMES_API_KEY unset")
        return 1

    session_id = chat_db.thread_session_id(thread_id)

    try:
        r = requests.post(
            f"{HERMES_API_URL}/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {HERMES_API_KEY}",
                "Content-Type": "application/json",
                "X-Hermes-Session-Id": session_id,
            },
            json={
                "model": "hermes-agent",
                "messages": [{"role": "user", "content": op_msg["body"]}],
                "stream": False,
            },
            timeout=TIMEOUT_S,
        )
    except requests.Timeout:
        _fail(placeholder_id,
              f"Hermes didn't respond within {TIMEOUT_S}s. "
              "It may be busy — try again in a minute, or use a slash command.",
              f"POST /v1/chat/completions timed out after {TIMEOUT_S}s")
        return 1
    except requests.RequestException as e:
        _fail(placeholder_id,
              f"Couldn't reach Hermes: {type(e).__name__}. "
              "Is the agent container running?",
              f"{type(e).__name__}: {e}")
        return 1

    if r.status_code == 429:
        _fail(placeholder_id,
              "Hermes is rate-limited right now. Give it a minute, "
              "or use a `/help` slash command in the meantime.",
              f"HTTP 429: {r.text[:200]}")
        return 1
    if r.status_code >= 500:
        _fail(placeholder_id,
              f"Hermes returned HTTP {r.status_code}. Try again shortly.",
              f"HTTP {r.status_code}: {r.text[:200]}")
        return 1
    if not r.ok:
        _fail(placeholder_id,
              f"Hermes returned HTTP {r.status_code}.",
              f"HTTP {r.status_code}: {r.text[:200]}")
        return 1

    # OpenAI-compatible shape: {choices:[{message:{content:"…"}}]}
    try:
        data = r.json()
        reply = data["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError) as e:
        _fail(placeholder_id,
              "Hermes returned an unparseable response.",
              f"{type(e).__name__}: {e}: body={r.text[:400]}")
        return 1

    if not reply or not reply.strip():
        _fail(placeholder_id,
              "Hermes returned an empty reply.",
              f"empty content in response: {r.text[:200]}")
        return 1

    visible_body, tool_call_json = _extract_proposal(reply)
    _deliver(placeholder_id, visible_body,
             thread_id=thread_id, session_id=session_id,
             tool_call_json=tool_call_json)
    return 0


if __name__ == "__main__":
    sys.exit(main())
