"""Execution side of the widget-based operator-confirmation flow.

Hermes proposes destructive actions by emitting a fenced JSON block in
its chat reply; the chat panel renders a Confirm widget from that
proposal. When the operator clicks Confirm, `/v2/chat/confirm` calls
into THIS module — NOT into a Hermes-callable skill. Hermes physically
cannot reach these functions, which is the entire point.

All handlers mutate the state DB via the same helpers the dashboard's
per-row buttons use, so the effect is identical to (e.g.) approving
each event one at a time in the UI.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from dashboard import db_v2
from engine import business_promote
from engine import event_cancel
from engine import event_state as _event_state
from engine import listing_website_cleanup
from engine import workqueue as _wq


# Whitelist of intent kinds. Anything Hermes proposes that isn't in this
# set is refused by /v2/chat/confirm.
ALLOWED_KINDS = frozenset({"decline_events", "approve_events",
                           "promote_businesses", "decline_businesses",
                           "confirm_cancellations", "dismiss_cancellations",
                           "clear_listing_websites"})


def _iso_utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def validate_proposal(proposal: Any) -> tuple[bool, str]:
    """Return (ok, error_message). Called from /v2/chat/confirm before
    dispatch — a proposal that fails validation never mutates DB."""
    if not isinstance(proposal, dict):
        return (False, "proposal must be a JSON object")
    kind = proposal.get("kind")
    if kind not in ALLOWED_KINDS:
        return (False, f"unknown intent kind: {kind!r}")
    uids = proposal.get("uids")
    if not isinstance(uids, list) or not uids:
        return (False, "uids must be a non-empty list")
    if len(uids) > 500:
        return (False, "refusing >500 uids in one confirm")
    for u in uids:
        if not isinstance(u, str) or not u or len(u) > 100:
            return (False, "uids must all be non-empty strings ≤ 100 chars")
    return (True, "")


def execute(proposal: dict, *, note: Optional[str] = None) -> dict:
    """Dispatch to the matching handler. Assumes `validate_proposal`
    already passed. Returns a summary dict the confirm route writes into
    the follow-up agent message."""
    kind = proposal["kind"]
    uids = proposal["uids"]
    if kind == "decline_events":
        return _decline_events(uids, note=note)
    if kind == "approve_events":
        return _approve_events(uids, note=note)
    if kind == "promote_businesses":
        return _promote_businesses(uids)
    if kind == "decline_businesses":
        return _decline_businesses(uids, note=note)
    if kind == "confirm_cancellations":
        return _confirm_cancellations(uids)
    if kind == "dismiss_cancellations":
        return _dismiss_cancellations(uids)
    if kind == "clear_listing_websites":
        return _clear_listing_websites(uids)
    # Unreachable if validate_proposal was called first.
    return {"error": f"no handler for kind: {kind}"}


# --------------------------------------------------------------------------
# Individual handlers
# --------------------------------------------------------------------------

def _decline_events(uids: list[str], *, note: Optional[str] = None) -> dict:
    """Flip approval_status → declined for each uid that's still `pending`.
    Uids already in a terminal state (declined, shipped) are skipped and
    reported."""
    now = _iso_utc_now()
    declined = 0
    skipped: list[str] = []
    with db_v2.connect() as c:
        for uid in uids:
            cur = c.execute(
                "UPDATE events SET approval_status='declined', "
                "  approval_note=?, approval_at=?, "
                "  state='declined', state_updated_at=? "
                "WHERE event_uid=? AND approval_status='pending'",
                (note, now, now, uid),
            )
            if cur.rowcount:
                declined += 1
            else:
                skipped.append(uid)
    return {
        "kind": "decline_events",
        "declined": declined,
        "skipped": len(skipped),
        "skipped_uids": skipped,
    }


def _approve_events(uids: list[str], *, note: Optional[str] = None) -> dict:
    """Flip approval_status → approved AND enqueue the first pipeline action for each
    successful approval. Mirrors the dashboard's per-row approve button."""
    now = _iso_utc_now()
    approved = 0
    enqueued = 0
    skipped: list[str] = []
    with db_v2.connect() as c:
        # connect() is autocommit; wrap the batch in one explicit transaction so
        # each approval and its first-action enqueue commit together (or the
        # whole batch rolls back) — no event left approved with no queue row.
        c.execute("BEGIN")
        try:
            for uid in uids:
                cur = c.execute(
                    "UPDATE events SET approval_status='approved', "
                    "  approval_note=?, approval_at=?, "
                    "  state='approved', state_updated_at=? "
                    "WHERE event_uid=? AND approval_status='pending'",
                    (note, now, now, uid),
                )
                if cur.rowcount:
                    approved += 1
                    _wq.enqueue(c, uid, _event_state.next_action_for("approved"))
                    enqueued += 1
                else:
                    skipped.append(uid)
            c.execute("COMMIT")
        except Exception:
            c.execute("ROLLBACK")
            raise
    return {
        "kind": "approve_events",
        "approved": approved,
        "enqueued": enqueued,
        "skipped": len(skipped),
        "skipped_uids": skipped,
    }


def _promote_businesses(uids: list[str]) -> dict:
    """Approve verified business candidates for promotion — the next
    `business-discovery promote` run creates, enriches and ships them. Same
    effect as the /candidates Promote button."""
    with db_v2.connect() as c:
        return {"kind": "promote_businesses", **business_promote.approve(c, uids)}


def _decline_businesses(uids: list[str], *, note: Optional[str] = None) -> dict:
    """Decline business candidates for good (and their same-name twins)."""
    with db_v2.connect() as c:
        return {"kind": "decline_businesses", **business_promote.decline(c, uids, note=note)}


def _confirm_cancellations(uids: list[str]) -> dict:
    """Confirm flagged events as cancelled — the next `event-cancellations
    apply` run marks them cancelled on TownLoops."""
    with db_v2.connect() as c:
        return {"kind": "confirm_cancellations", **event_cancel.confirm(c, uids)}


def _dismiss_cancellations(uids: list[str]) -> dict:
    """The events are still on: clear the flags for good."""
    with db_v2.connect() as c:
        return {"kind": "dismiss_cancellations", **event_cancel.dismiss(c, uids)}


def _clear_listing_websites(uids: list[str]) -> dict:
    """Confirm listing-page website clears — the operator-triggered
    `listing-website-cleanup apply` then clears them live, then locally, and
    re-enriches. Inactive in autonomous mode."""
    with db_v2.connect() as c:
        return {"kind": "clear_listing_websites", **listing_website_cleanup.confirm(c, uids)}
