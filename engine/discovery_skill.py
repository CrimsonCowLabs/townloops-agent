"""Shared CLI flow for business-discovery and community-discovery skills.

Each skill's discover.py is a 5-line wrapper that calls `main(kind=...)`
below. All the actual work — argument parsing, town-profile loading,
DB writes, verify + promote logic — lives here so the two skills stay
in lockstep.

Subcommands (identical across both skills):
  sweep    — fan out Firecrawl search + directory scrape + facebook search;
             insert new rows into discovery_candidates (status='new').
  verify   — for each status='new' row, adversarial verify; mark verified
             or rejected. Adversarial = "default to rejecting on doubt."
  promote  — for verified + high-confidence rows, create entity table row
             (businesses / communities) + enqueue enrichment. Skips rows
             already promoted (idempotent).

The three commands can be run independently or chained by the orchestrator.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import yaml

from engine import db as _db
from engine import discovery, policy
from engine import townloops_api
from engine.ingest_core import DEFAULT_TZ


_LOG_PREFIX = "[discovery]"


# --------------------------------------------------------------------------
# Town profile
# --------------------------------------------------------------------------

def _default_town_profile_path() -> Path:
    return Path(os.environ.get(
        "TOWN_PROFILE_PATH",
        os.path.join(
            os.environ.get("HERMES_HOME", os.path.expanduser("~/.hermes")),
            "..",
            "town-profile.yaml",
        ),
    ))


def load_town_profile(path: Optional[Path] = None) -> dict:
    """Load the town-profile.yaml describing seed URLs, query terms, etc.

    Returns an empty dict on missing file — the discovery skills fall back
    to what they can derive from state/categories.json, and just skip the
    directory / facebook sources if seeds aren't provided.
    """
    p = path or _default_town_profile_path()
    if not p.exists():
        return {}
    try:
        return yaml.safe_load(p.read_text()) or {}
    except (OSError, yaml.YAMLError) as exc:
        print(f"{_LOG_PREFIX} town-profile.yaml unreadable: {exc}", file=sys.stderr)
        return {}


# --------------------------------------------------------------------------
# UID + timestamps
# --------------------------------------------------------------------------

def _iso_now() -> str:
    return datetime.now(tz=DEFAULT_TZ).isoformat()


def _iso_utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _candidate_uid(town_slug: str, canonical_url: str, kind: str) -> str:
    return hashlib.sha1(
        f"{town_slug}|{canonical_url}|{kind}".encode("utf-8")
    ).hexdigest()


def _entity_uid(kind: str, name: str) -> str:
    name_norm = " ".join((name or "").lower().split())
    return hashlib.sha1(f"{kind}|{name_norm}".encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# Sweep
# --------------------------------------------------------------------------

def _business_query_templates(profile: dict, town_name: str, region: str) -> list[str]:
    terms = ((profile.get("discovery") or {}).get("business") or {}).get(
        "category_query_terms"
    ) or []
    templates: list[str] = []
    loc = f"{town_name}, {region}".strip(", ")
    for term in terms:
        templates.append(f"{term} in {loc}")
        templates.append(f"best {term} {town_name}")
        templates.append(f"local {term} {town_name}")
    return templates


def _community_query_groups(profile: dict) -> list[dict]:
    return ((profile.get("discovery") or {}).get("community") or {}).get(
        "query_groups"
    ) or []


def _seed_directories(profile: dict, kind: str) -> list[str]:
    return ((profile.get("discovery") or {}).get(kind) or {}).get(
        "seed_directories"
    ) or []


def cmd_sweep(kind: str, args, conn: sqlite3.Connection, profile: dict) -> int:
    town_slug = os.environ.get("TOWNLOOPS_TOWN_SLUG", "")
    if not town_slug:
        print(f"{_LOG_PREFIX} TOWNLOOPS_TOWN_SLUG not set", file=sys.stderr)
        return 1
    town_name = profile.get("name") or town_slug
    region = profile.get("region") or ""
    allow_gov = kind == "community"

    all_candidates: list[discovery.Candidate] = []
    errors: list[str] = []

    # Source 1: web search
    if kind == "business":
        templates = _business_query_templates(profile, town_name, region)
        if not templates:
            print(f"{_LOG_PREFIX} no business.category_query_terms in profile — skipping web search",
                  file=sys.stderr)
        else:
            web = discovery.sweep_web_search(
                town_name, region, templates,
                kind="business",
                per_source=args.per_source,
                allow_gov=False,
                error_sink=errors,
            )
            all_candidates.extend(web)
    else:  # community
        groups = _community_query_groups(profile)
        if not groups:
            print(f"{_LOG_PREFIX} no community.query_groups in profile — skipping web search",
                  file=sys.stderr)
        for grp in groups:
            cat_slug = grp.get("category_slug")
            terms = grp.get("terms") or []
            if not cat_slug or not terms:
                continue
            templates = [f"{t} in {town_name}" for t in terms] + [
                f'"{t}" {town_name}' for t in terms
            ]
            hits = discovery.sweep_web_search(
                town_name, region, templates,
                kind="community",
                per_source=args.per_source,
                allow_gov=True,
                error_sink=errors,
            )
            for h in hits:
                h.target_category_slug = cat_slug
            all_candidates.extend(hits)

    # Source 2: seed directories
    seeds = _seed_directories(profile, kind)
    if seeds:
        dir_hits = discovery.sweep_directories(
            seeds, kind=kind, per_source=args.per_source, error_sink=errors,
        )
        all_candidates.extend(dir_hits)

    # Source 3: facebook — only when profile opts in
    if profile.get("discovery", {}).get(kind, {}).get("facebook_terms"):
        fb_terms = profile["discovery"][kind]["facebook_terms"]
        fb_hits = discovery.sweep_facebook(
            town_name, fb_terms, kind=kind,
            per_source=args.per_source, error_sink=errors,
        )
        all_candidates.extend(fb_hits)

    # Insert into DB
    inserted = 0
    seen_this_run = 0
    for c in all_candidates:
        seen_this_run += 1
        uid = _candidate_uid(town_slug, c.canonical_url, c.kind)
        try:
            conn.execute(
                """
                INSERT INTO discovery_candidates
                  (candidate_uid, town_slug, kind, target_category_slug,
                   canonical_url, display_name, source_signal,
                   first_seen_at, last_seen_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (uid, town_slug, c.kind, c.target_category_slug,
                 c.canonical_url, c.display_name, c.source_signal,
                 _iso_now(), _iso_now()),
            )
            inserted += 1
        except sqlite3.IntegrityError:
            conn.execute(
                "UPDATE discovery_candidates SET last_seen_at = ? "
                "WHERE candidate_uid = ?",
                (_iso_now(), uid),
            )
    conn.commit()

    print(json.dumps({
        "kind": kind, "town_slug": town_slug,
        "seen": seen_this_run, "inserted_new": inserted,
        "errors": len(errors),
    }))
    return 0


# --------------------------------------------------------------------------
# Verify
# --------------------------------------------------------------------------

def cmd_verify(kind: str, args, conn: sqlite3.Connection, profile: dict) -> int:
    town_name = profile.get("name") or os.environ.get("TOWNLOOPS_TOWN_SLUG", "")
    region = profile.get("region") or ""

    rows = conn.execute(
        """
        SELECT candidate_uid, canonical_url
        FROM discovery_candidates
        WHERE status = 'new' AND kind = ?
        ORDER BY first_seen_at
        LIMIT ?
        """,
        (kind, args.limit),
    ).fetchall()

    verified = 0
    rejected = 0
    for r in rows:
        try:
            v = discovery.verify_candidate(
                r["canonical_url"], town_name=town_name, region=region,
            )
        except Exception as exc:
            v = {"verified": False, "reasons": [f"exception: {exc}"]}
        new_status = "verified" if v.get("verified") else "rejected"
        reject_reason = None
        if not v.get("verified"):
            reject_reason = "; ".join(v.get("reasons") or []) or "unspecified"
        conn.execute(
            """
            UPDATE discovery_candidates
            SET status = ?, verify_json = ?, reject_reason = ?, last_seen_at = ?
            WHERE candidate_uid = ?
            """,
            (new_status, json.dumps(v, ensure_ascii=False),
             reject_reason, _iso_now(), r["candidate_uid"]),
        )
        if new_status == "verified":
            verified += 1
        else:
            rejected += 1
    conn.commit()

    print(json.dumps({
        "kind": kind, "verified": verified, "rejected": rejected,
        "processed": len(rows),
    }))
    return 0


# --------------------------------------------------------------------------
# Promote
# --------------------------------------------------------------------------

_ENTITY_TABLE = {"business": "businesses", "community": "communities"}


def cmd_promote(kind: str, args, conn: sqlite3.Connection, profile: dict) -> int:
    """Move verified, high-confidence candidates into the entity table and
    enqueue enrichment.

    A promoted candidate becomes an entity row with source='discovery',
    source_kind='auto-promoted'. Enrichment then fills description,
    address/phone/socials/etc. via engine.enrichment.
    """
    town_slug = os.environ.get("TOWNLOOPS_TOWN_SLUG", "")
    town_name = profile.get("name") or town_slug

    available_slugs = policy.load_category_slugs()

    rows = conn.execute(
        """
        SELECT candidate_uid, canonical_url, display_name,
               target_category_slug, verify_json
        FROM discovery_candidates
        WHERE status = 'verified' AND kind = ?
        ORDER BY first_seen_at
        LIMIT ?
        """,
        (kind, args.limit),
    ).fetchall()

    promoted = 0
    skipped = 0
    table = _ENTITY_TABLE[kind]
    for r in rows:
        verify = json.loads(r["verify_json"] or "{}")
        ok, why = policy.is_high_confidence(verify, town_name)
        if not ok:
            conn.execute(
                "UPDATE discovery_candidates SET status = 'skipped', "
                "reject_reason = ?, last_seen_at = ? WHERE candidate_uid = ?",
                (f"low-confidence: {why}", _iso_now(), r["candidate_uid"]),
            )
            skipped += 1
            continue

        name = r["display_name"] or r["canonical_url"]
        target_cat = r["target_category_slug"]
        if not target_cat and kind == "business":
            target_cat = policy.resolve_category_slug(
                kind, name, verify.get("markdown_head") or "", available_slugs,
            )

        entity_uid = _entity_uid(kind, name)
        # Avoid re-inserting if we already have this name-normalized row.
        exists = conn.execute(
            f"SELECT 1 FROM {table} WHERE entity_uid = ?", (entity_uid,)
        ).fetchone()
        if exists:
            conn.execute(
                "UPDATE discovery_candidates SET status = 'promoted', "
                "research_entity_uid = ?, last_seen_at = ? "
                "WHERE candidate_uid = ?",
                (entity_uid, _iso_now(), r["candidate_uid"]),
            )
            promoted += 1
            continue

        cols = [
            "entity_uid", "source", "source_kind", "name",
            "address" if kind == "business" else None,
            "website", "phone" if kind == "business" else None,
            "category_slug", "source_url",
            "first_seen_at", "last_seen_at",
            "content_hash", "last_modified_at",
        ]
        cols = [c for c in cols if c is not None]
        vals: list = [
            entity_uid, "discovery", "auto-promoted", name,
        ]
        if kind == "business":
            vals.append(verify.get("address"))
        vals.extend([
            r["canonical_url"],
        ])
        if kind == "business":
            vals.append(verify.get("phone"))
        vals.extend([
            target_cat, r["canonical_url"],
            _iso_now(), _iso_now(),
            _content_hash(name, r["canonical_url"], target_cat),
            _iso_utc_now(),
        ])
        conn.execute(
            f"INSERT INTO {table} ({', '.join(cols)}) "
            f"VALUES ({', '.join(['?'] * len(cols))})",
            vals,
        )
        # Enqueue for enrichment.
        conn.execute(
            "INSERT OR IGNORE INTO enrichment_queue (kind, entity_uid, queued_at) "
            "VALUES (?, ?, ?)",
            (kind, entity_uid, _iso_utc_now()),
        )
        conn.execute(
            "UPDATE discovery_candidates SET status = 'promoted', "
            "research_entity_uid = ?, last_seen_at = ? "
            "WHERE candidate_uid = ?",
            (entity_uid, _iso_now(), r["candidate_uid"]),
        )
        promoted += 1
    conn.commit()

    print(json.dumps({
        "kind": kind, "promoted": promoted, "skipped": skipped,
        "processed": len(rows),
    }))
    return 0


def _content_hash(*parts: Optional[str]) -> str:
    payload = "|".join((p or "") for p in parts)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def build_parser(prog: str) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog=prog)
    sub = p.add_subparsers(dest="cmd", required=True)

    sw = sub.add_parser("sweep", help="Discover candidates via search + directories")
    sw.add_argument("--per-source", type=int, default=25)

    vf = sub.add_parser("verify", help="Adversarial-verify status=new candidates")
    vf.add_argument("--limit", type=int, default=20)

    pr = sub.add_parser("promote", help="Auto-promote verified high-confidence rows")
    pr.add_argument("--limit", type=int, default=10)

    ls = sub.add_parser("list", help="List candidates in the queue")
    ls.add_argument("--status", type=str, default=None,
                    help="Filter by status (new/verified/rejected/promoted/skipped)")
    ls.add_argument("--limit", type=int, default=50)
    return p


def cmd_list(kind: str, args, conn: sqlite3.Connection, profile: dict) -> int:
    where = ["kind = ?"]
    params: list = [kind]
    if args.status:
        where.append("status = ?")
        params.append(args.status)
    params.append(args.limit)
    rows = conn.execute(
        f"""
        SELECT candidate_uid, status, target_category_slug, canonical_url,
               display_name, source_signal, reject_reason
        FROM discovery_candidates
        WHERE {' AND '.join(where)}
        ORDER BY first_seen_at DESC
        LIMIT ?
        """,
        params,
    ).fetchall()
    for r in rows:
        print(json.dumps(dict(r), ensure_ascii=False))
    return 0


def main(kind: str, argv: Optional[list[str]] = None) -> int:
    prog = f"{kind}-discovery"
    parser = build_parser(prog)
    args = parser.parse_args(argv)

    profile = load_town_profile()
    conn = _db.connect()
    conn.row_factory = sqlite3.Row

    dispatch = {
        "sweep": cmd_sweep,
        "verify": cmd_verify,
        "promote": cmd_promote,
        "list": cmd_list,
    }
    return dispatch[args.cmd](kind, args, conn, profile)
