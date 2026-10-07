"""One-off import of the CMO's stranded TownLoops work into this town agent.

Before the town agent existed, the CMO Hermes researched entities
(`townloops-research/data/drafts.db`) and discovered businesses
(`townloops-business-discovery/data/candidates.db`). Its unshipped drafts
and verified, unpromoted candidates only survive in restic snapshot
`71b37ab2`. This brings them in for the operator. Nothing here writes to
TownLoops (it only reads), and nothing it imports ships on its own:

- a verified candidate → a `verified` business-discovery candidate
  (`source_signal` 'cmo-import:<signal>'), proposed like any swept one;
- a business draft → the same, with the draft's fields under
  `verify.draft`, which the promote run fills into the business's blanks;
- any other draft → a local entity with only its name, `needs-review`, the
  draft recorded as its one review candidate (`performers.py review` /
  `resolve --accept` fills it).

Each record is checked against live first (website, or a ≥0.85 name, as
business-discovery does — website only for performers/creators, whose
identity is never taken from a name) and against the local tables; matches are
reported, not imported. Re-running imports nothing twice: candidate and
entity uids are deterministic.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Callable, Optional

from engine import business_discovery as bd
from engine import discovery, research
from engine.derive_helpers import (
    best_name_match, insert_entity, iso_utc_now, mint_entity_uid, record_activity,
)
from engine.entity_enrich import SPECS, validate_payload
from engine.performer_research import KINDS as PEOPLE_KINDS, REVIEW_KINDS, REVIEW_STATUS

SIGNAL = "cmo-import"
DRAFT_SIGNAL = f"{SIGNAL}:research-draft"
_OUTCOMES = ("imported", "skipped", "already_live", "not_imported")

# CMO drafts store social handles; local rows store profile URLs.
_SOCIAL_URLS: dict[str, Callable[[str], str]] = {
    "instagram": lambda h: f"https://www.instagram.com/{h}/",
    "facebook": lambda h: f"https://www.facebook.com/{h}",
    "tiktok": lambda h: f"https://www.tiktok.com/@{h}",
    "twitter": lambda h: f"https://x.com/{h}",
}


# --------------------------------------------------------------------------
# Loading the restored CMO databases
# --------------------------------------------------------------------------

def _rows(path: Path, sql: str) -> list[dict]:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql)]
    finally:
        conn.close()


def load_drafts(path: Path) -> list[dict]:
    """The CMO research drafts that never shipped."""
    return _rows(path, "SELECT * FROM drafts WHERE pushed_at IS NULL ORDER BY saved_at, url")


def load_candidates(path: Path) -> list[dict]:
    """Every CMO business candidate; run_import takes only the verified ones."""
    return _rows(path, "SELECT * FROM candidates ORDER BY first_seen_at, canonical_url")


# --------------------------------------------------------------------------
# Import
# --------------------------------------------------------------------------

def run_import(conn: sqlite3.Connection, client, town_slug: str, *,
               drafts: list[dict], candidates: list[dict]) -> dict:
    """Import `candidates` (CMO candidates.db rows) and `drafts` (unshipped
    drafts.db rows) for `town_slug`. Returns a report: per section, the
    records `imported`, `skipped` (imported before), `already_live` and
    `not_imported` (with why), plus `summary` counts. Doesn't commit."""
    indexes: dict[str, bd.LiveIndex] = {}

    def live(kind: str) -> bd.LiveIndex:
        if kind not in indexes:
            indexes[kind] = bd.LiveIndex.build(client.paginate(SPECS[kind].table), kind=kind)
        return indexes[kind]

    report = {"candidates": {k: [] for k in _OUTCOMES}, "drafts": {k: [] for k in _OUTCOMES}}
    out = report["candidates"]
    for c in candidates:
        name = c.get("display_name")
        if c.get("town_slug") != town_slug:
            out["not_imported"].append({"name": name, "url": c["canonical_url"],
                                        "why": f"another town ({c.get('town_slug')})"})
        elif c.get("status") != "verified":
            out["not_imported"].append({"name": name, "url": c["canonical_url"],
                                        "why": f"CMO status {c.get('status')}: "
                                               f"{c.get('reject_reason') or ''}".rstrip(": ")})
        else:
            verify = json.loads(c.get("verify_json") or "null") or {}
            _import_candidate(conn, town_slug, live(bd.KIND), out, url=c["canonical_url"],
                              name=name, signal=f"{SIGNAL}:{c.get('source_signal') or 'sweep'}",
                              verify=verify, first_seen=c.get("first_seen_at"))

    out = report["drafts"]
    for d in drafts:
        kind = d.get("kind")
        name = (json.loads(d.get("payload_json") or "{}").get("name") or "").strip()
        if kind not in (bd.KIND, *REVIEW_KINDS) or not name:
            out["not_imported"].append({"name": name or None, "url": d["url"], "kind": kind,
                                        "why": "no name" if name else f"unknown kind {kind!r}"})
            continue
        fields = draft_fields(kind, d)
        if kind == bd.KIND:
            verify = {"verified": True, "reasons": [_draft_note(d)], "draft": fields}
            _import_candidate(conn, town_slug, live(kind), out,
                              url=fields.get("website") or d["url"], name=name,
                              signal=DRAFT_SIGNAL, verify=verify, first_seen=d.get("fetched_at"))
        else:
            _import_review_draft(conn, live(kind), out, kind, name, d, fields)

    report["summary"] = {section: {k: len(report[section][k]) for k in _OUTCOMES}
                         for section in ("candidates", "drafts")}
    return report


def draft_fields(kind: str, draft: dict) -> dict:
    """The draft's payload as this kind's row fields: social handles become
    profile URLs, a logo/placeholder image is dropped (null beats generic),
    and values that fail the payload schema are dropped."""
    payload = json.loads(draft.get("payload_json") or "{}")
    fields = {k: v for k, v in payload.items() if k not in ("name", "slug") and v}
    if draft.get("image_url") and not fields.get("image_path"):
        fields["image_path"] = draft["image_url"]
    for key, to_url in _SOCIAL_URLS.items():
        handle = fields.get(key)
        if isinstance(handle, str) and not handle.startswith(("http://", "https://")):
            fields[key] = to_url(handle.strip().lstrip("@"))
    image = fields.get("image_path")
    if image and research.is_generic_image(image, fields.get("image_alt") or ""):
        fields.pop("image_path")
        fields.pop("image_alt", None)
    clean, _ = validate_payload(kind, fields)
    return clean


def _draft_note(draft: dict) -> str:
    when = (draft.get("fetched_at") or "")[:10]
    note = (draft.get("source_note") or "").strip()
    return f"CMO research draft ({when})" + (f": {note}" if note else "")


def _import_candidate(conn: sqlite3.Connection, town_slug: str, index: bd.LiveIndex,
                      out: dict, *, url: str, name: Optional[str], signal: str,
                      verify: dict, first_seen: Optional[str]) -> None:
    """Store one business candidate — `verified`, or `already-live` /
    operator-declined `rejected`, as a sweep would — unless it's stored already."""
    canon = discovery.canonicalize_url(url)
    uid = bd.candidate_uid(town_slug, canon)
    item = {"name": name, "url": canon, "candidate_uid": uid}
    if conn.execute("SELECT 1 FROM discovery_candidates WHERE candidate_uid = ?",
                    (uid,)).fetchone():
        out["skipped"].append({**item, "why": "already in the candidate store"})
        return
    match = index.match(canon, name)
    declined = None if match else bd.declined_match(conn, town_slug, name)
    if match:
        status, reason = "already-live", match.reason
        out["already_live"].append({**item, "live_slug": match.slug, "why": reason})
    elif declined:
        status, reason = "rejected", bd.declined_reason(declined)
        out["not_imported"].append({**item, "why": reason})
    else:
        status, reason = "verified", None
        out["imported"].append(item)
    now = iso_utc_now()
    conn.execute(
        """INSERT INTO discovery_candidates
             (candidate_uid, town_slug, kind, canonical_url, display_name, source_signal,
              status, live_slug, reject_reason, verify_json, first_seen_at, last_seen_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (uid, town_slug, bd.KIND, canon, name, signal, status,
         match.slug if match else None, reason,
         json.dumps({**verify, "imported_from": SIGNAL}, ensure_ascii=False),
         first_seen or now, now),
    )


def _import_review_draft(conn: sqlite3.Connection, index: bd.LiveIndex, out: dict,
                         kind: str, name: str, draft: dict, fields: dict) -> None:
    """A local `needs-review` entity holding only the draft's name; the draft
    is its one review candidate, so accepting it fills the blanks."""
    table = SPECS[kind].table
    entity_uid = mint_entity_uid(kind, name, f"{SIGNAL}:{draft['url']}")
    item = {"name": name, "url": draft["url"], "kind": kind, "entity_uid": entity_uid}
    if conn.execute(f"SELECT 1 FROM {table} WHERE entity_uid = ?", (entity_uid,)).fetchone():
        out["skipped"].append({**item, "why": "imported before"})
        return
    # A performer/creator is never identified by name alone — only its own site.
    match = index.match(discovery.canonicalize_url(fields.get("website") or draft["url"]),
                        None if kind in PEOPLE_KINDS else name)
    if match:
        out["already_live"].append({**item, "live_slug": match.slug, "why": match.reason})
        return
    local = best_name_match(name, [dict(r) for r in conn.execute(
        f"SELECT entity_uid, name FROM {table}")])
    if local:
        out["not_imported"].append({**item, "entity_uid": local["entity_uid"],
                                    "why": f"same name as local {kind} {local['name']!r}"})
        return
    why = _draft_note(draft)
    research_record = {"status": "imported", "why": why, "at": iso_utc_now(),
                       "venues": [], "candidates": [{"url": draft["url"], "fields": fields}]}
    insert_entity(conn, table, entity_uid, {
        "source": DRAFT_SIGNAL, "source_kind": "import", "name": name,
        "source_url": draft["url"], "enrichment_status": REVIEW_STATUS,
        "extra_json": json.dumps({"research": research_record}, ensure_ascii=False,
                                 sort_keys=True),
    })
    record_activity(conn, SIGNAL, REVIEW_STATUS, entity_uid,
                    detail=f"{kind} {name}: imported — {why}", ok=False, error=why[:200])
    out["imported"].append(item)
