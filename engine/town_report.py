"""The daily town report (#64): what the town did in a window (default the
last 24h), built from the DB alone — counts plus a few examples per section,
"Needs you" and the standing alerts first (engine/alerts), and the analyst
run's own write-up when it passes one. Saved as
`state/reports/<date>-analyst.md`; the Telegram message is the counts, the
operator's items and the file's path.

The nightly digest (engine/orchestrator.summary) stays as it is beside it;
both take their standing alerts from engine/alerts.
"""

from __future__ import annotations

import os
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

from engine import alerts, analyst_runs, event_quality, live_index
from engine.derive_helpers import dict_rows as _rows

EXAMPLES = 5
ENTITY_TABLES = {"venue": "venues", "business": "businesses", "community": "communities",
                 "performer": "performers", "creator": "creators"}


@dataclass
class Report:
    markdown: str
    telegram: str
    counts: dict = field(default_factory=dict)
    alerts: list = field(default_factory=list)


def build(conn: sqlite3.Connection, since: str, *, narrative: Optional[str] = None,
          now: Optional[datetime] = None) -> Report:
    now = now or datetime.now(timezone.utc)
    town = os.environ.get("TOWNLOOPS_TOWN_SLUG", "?")
    standing = alerts.standing(conn)
    needs = [a for a in standing if a.needs_you]
    other = [a for a in standing if not a.needs_you]
    c = {"sources": _sources(conn, since), "events": _events(conn, since),
         "entities": _entities(conn, since), "holds": _holds(conn),
         "failures": _failures(conn, since), "ambiguous": _ambiguous(conn),
         "quality": event_quality.by_seed(conn, now)}
    runs = analyst_runs.recent(conn, since)

    md = [f"# {town} — analyst report {now:%Y-%m-%d}",
          f"_Since {since} (UTC), built {now.isoformat(timespec='seconds')}_",
          "", "## Needs you"]
    md += [f"- {a.text}" for a in needs] or ["- nothing"]
    md += ["", "## Standing alerts"]
    md += [f"- {a.text}" for a in other] or ["- none"]
    if narrative and narrative.strip():
        md += ["", "## Analyst notes", "", narrative.strip()]

    s = c["sources"]
    md += ["", "## Sources",
           f"- found {s['found']}, trialled {s['trialled']}, adopted {s['adopted']}, "
           f"rejected {s['rejected']}"]
    md += [f"- adopted: {x}" for x in s["adopted_examples"]]
    md += [f"- rejected: {x}" for x in s["rejected_examples"]]

    e = c["events"]
    md += ["", "## Events",
           f"- found {e['found']}, judged {e['judged']} (approved {e['approved']}, declined "
           f"{e['declined']}), submitted {e['submitted']}, shipped {e['shipped']}",
           f"- moderation: published {e['published']}, merged {e['merged']}, rejected "
           f"{e['rejected']}; still pending in TownLoops {e['pending']}"]
    md += [f"- submitted: {x}" for x in e["submitted_examples"]]
    md += [f"- rejected by the moderator: {x}" for x in e["rejected_examples"]]

    n = c["entities"]
    md += ["", "## Entities",
           f"- discovered {n['discovered']} candidate(s) ("
           + (", ".join(f"{k} {v}" for k, v in sorted(n["discovered_by_kind"].items())) or "none")
           + f"); new local entities: "
           + (", ".join(f"{k} {v}" for k, v in sorted(n["new_by_kind"].items())) or "none"),
           f"- deduped against live: {n['deduped']}",
           *[f"  - {x}" for x in n["deduped_examples"]],
           f"- enriched {n['enriched']} ("
           + (", ".join(f"{k} {v}" for k, v in sorted(n["enriched_by_kind"].items())) or "none")
           + f"), {n['with_image']} with an image"]

    q = c["quality"]
    md += ["", "## Event data by seed",
           f"_Share of each seed's events from the last {event_quality.SEED_WINDOW.days} days, "
           "after its detail read, carrying each field. **Bold**: under "
           f"{event_quality.SEED_THIN_RATE:.0%} on a field a submission needs (autonomous: "
           "its events missing it are held as thin)._"]
    md += event_quality.table(q) if q else ["- no events read yet"]

    h = c["holds"]
    md += ["", "## Holds",
           f"- events held for review: {h['events']}"
           + (" (" + ", ".join(f"{k} {v}" for k, v in sorted(h["by_kind"].items())) + ")"
              if h["by_kind"] else ""),
           f"- seeds held: {h['seeds']}"]
    md += [f"- held seed: {x}" for x in h["seed_examples"]]

    amb = c["ambiguous"]
    md += ["", "## Ambiguous live matches",
           "_Not created, not bound: decide which live record each one is._"]
    md += [f"- {x}" for x in amb] or ["- none"]

    f = c["failures"]
    md += ["", "## Failures",
           f"- stuck events: {f['stuck']}; workqueue errors: {f['workqueue_errors']}; "
           f"failed source runs: {f['source_runs_failed']}"]
    md += [f"- {x}" for x in f["examples"]]

    md += ["", "## Analyst runs"]
    md += [f"- #{r['id']} {r['status']} — started {r['started_at']}"
           + (f", ended {r['ended_at']}" if r["ended_at"] else "")
           + (f" — {r['notes']}" if r["notes"] else "") for r in runs] or ["- none"]

    tg = [f"[{town}] daily report ({now:%Y-%m-%d})"]
    if needs:
        tg += ["NEEDS YOU:"] + [f"• {a.text[:300]}" for a in needs[:8]]
    tg += [f"sources: found {s['found']}, trialled {s['trialled']}, adopted {s['adopted']}, "
           f"rejected {s['rejected']}",
           f"events: found {e['found']}, submitted {e['submitted']}, published {e['published']}, "
           f"rejected {e['rejected']}",
           f"entities: discovered {n['discovered']}, deduped {n['deduped']}, enriched "
           f"{n['enriched']} ({n['with_image']} with an image)",
           f"holds: {h['events']} events, {h['seeds']} seeds; failures: stuck {f['stuck']}, "
           f"source runs {f['source_runs_failed']}"]
    if amb:
        tg.append(f"ambiguous live matches: {len(amb)}")
    if other:
        tg.append(f"standing alerts: {len(other)}")
    return Report("\n".join(md) + "\n", "\n".join(tg), c, alerts.as_dicts(standing))


def run(conn: sqlite3.Connection, *, since: Optional[str] = None, hours: int = 24,
        narrative: Optional[str] = None, send: bool = False,
        report_dir: Optional[Path] = None,
        alert: Optional[Callable[[str], None]] = None) -> dict:
    """Tick the immediate alerts, build, save and (with `send`) Telegram the
    report. Returns its path, counts and standing alerts."""
    now = datetime.now(timezone.utc)
    since = since or (now - timedelta(hours=hours)).isoformat(timespec="seconds")
    alerts.tick(conn, alert)
    report = build(conn, since, narrative=narrative, now=now)
    report_dir = report_dir or (Path(os.environ.get("HERMES_HOME", "/home/agent/.hermes"))
                                / "state" / "reports")
    report_dir.mkdir(parents=True, exist_ok=True)
    path = report_dir / f"{now:%Y-%m-%d}-analyst.md"
    path.write_text(report.markdown)
    sent = False
    if send:
        text = f"{report.telegram}\nreport: {path}"
        if alert:
            alert(text)
            sent = True
        else:
            from engine import notify
            sent = notify.telegram(text) is not False
    return {"report_path": str(path), "since": since, "sent": sent,
            "counts": report.counts, "alerts": report.alerts}


# --------------------------------------------------------------------------
# Sections
# --------------------------------------------------------------------------

def _count(conn: sqlite3.Connection, sql: str, args: tuple = ()) -> int:
    return conn.execute(sql, args).fetchone()[0]


def _sources(conn: sqlite3.Connection, since: str) -> dict:
    changed = "status_changed_at >= ?"
    return {
        "found": _count(conn, "SELECT COUNT(*) FROM event_seeds WHERE first_seen_at >= ?",
                        (since,)),
        "trialled": _count(conn, "SELECT COUNT(*) FROM event_seeds WHERE trial_at >= ?",
                           (since,)),
        "adopted": _count(conn, f"SELECT COUNT(*) FROM event_seeds WHERE status = 'approved' "
                                f"AND {changed}", (since,)),
        "rejected": _count(conn, f"SELECT COUNT(*) FROM event_seeds WHERE status = 'rejected' "
                                 f"AND {changed}", (since,)),
        "adopted_examples": [f"{r['name']} — {r['url']}" for r in _rows(
            conn, f"SELECT name, url FROM event_seeds WHERE status = 'approved' AND {changed} "
                  f"ORDER BY status_changed_at DESC LIMIT {EXAMPLES}", (since,))],
        "rejected_examples": [f"{r['name']} — {r['status_note'] or 'no reason recorded'}"
                              for r in _rows(
            conn, f"SELECT name, status_note FROM event_seeds WHERE status = 'rejected' "
                  f"AND {changed} ORDER BY status_changed_at DESC LIMIT {EXAMPLES}", (since,))],
    }


def _events(conn: sqlite3.Connection, since: str) -> dict:
    judged = dict(conn.execute(
        "SELECT approval_status, COUNT(*) FROM events WHERE approval_at >= ? "
        "AND approval_status IN ('approved', 'declined') GROUP BY approval_status",
        (since,)).fetchall())
    decided = dict(conn.execute(
        "SELECT status, COUNT(*) FROM submissions WHERE resource = 'events' AND decided_at >= ? "
        "GROUP BY status", (since,)).fetchall())
    title = ("COALESCE((SELECT COALESCE(e.title, e.name) FROM events e "
             "WHERE e.event_uid = s.local_uid), s.slug)")
    return {
        "found": _count(conn, "SELECT COUNT(*) FROM events WHERE first_seen_at >= ?", (since,)),
        "judged": sum(judged.values()), "approved": judged.get("approved", 0),
        "declined": judged.get("declined", 0),
        "submitted": _count(conn, "SELECT COUNT(*) FROM submissions WHERE resource = 'events' "
                                  "AND submitted_at >= ?", (since,)),
        "shipped": _count(conn, "SELECT COUNT(*) FROM events WHERE state = 'shipped' "
                                "AND state_updated_at >= ?", (since,)),
        "published": decided.get("published", 0), "merged": decided.get("merged", 0),
        "rejected": decided.get("rejected", 0),
        "pending": _count(conn, "SELECT COUNT(*) FROM submissions WHERE resource = 'events' "
                                "AND status = 'pending'"),
        "submitted_examples": [f"{r['t']} ({r['kind']}, `{r['slug']}`)" for r in _rows(
            conn, f"SELECT {title} AS t, s.kind, s.slug FROM submissions s "
                  f"WHERE s.resource = 'events' AND s.submitted_at >= ? "
                  f"ORDER BY s.id DESC LIMIT {EXAMPLES}", (since,))],
        "rejected_examples": [f"{r['t']} (`{r['slug']}`, seed {r['seed'] or '?'})" for r in _rows(
            conn, f"SELECT {title} AS t, s.slug, s.seed FROM submissions s "
                  f"WHERE s.resource = 'events' AND s.status = 'rejected' AND s.decided_at >= ? "
                  f"ORDER BY s.decided_at DESC LIMIT {EXAMPLES}", (since,))],
    }


def _entities(conn: sqlite3.Connection, since: str) -> dict:
    found = dict(conn.execute("SELECT kind, COUNT(*) FROM discovery_candidates "
                              "WHERE first_seen_at >= ? GROUP BY kind", (since,)).fetchall())
    deduped = [f"{r['display_name'] or r['canonical_url']} → {r['live_slug']}" for r in _rows(
        conn, "SELECT display_name, canonical_url, live_slug FROM discovery_candidates "
              "WHERE status = 'already-live' AND last_seen_at >= ? "
              "ORDER BY last_seen_at DESC", (since,))]
    # Creates skipped at ship for a live record (engine/live_index).
    deduped += [f"{_target_name(conn, r['target'])}: {r['detail']}" for r in _rows(
        conn, "SELECT target, detail FROM agent_activity WHERE worker = ? AND action = ? "
              "AND ts >= ? ORDER BY ts DESC", (live_index.WORKER, live_index.DEDUPED, since))]
    new, enriched, images = Counter(), Counter(), 0
    for kind, table in ENTITY_TABLES.items():
        new[kind] = _count(conn, f"SELECT COUNT(*) FROM {table} WHERE first_seen_at >= ? "
                                 f"AND COALESCE(source_kind, '') != 'live'", (since,))
        enriched[kind] = _count(conn, f"SELECT COUNT(*) FROM {table} WHERE enriched_at >= ? "
                                      f"AND enrichment_status = 'enriched'", (since,))
        images += _count(conn, f"SELECT COUNT(*) FROM {table} WHERE enriched_at >= ? "
                               f"AND enrichment_status = 'enriched' "
                               f"AND COALESCE(image_path, '') != ''", (since,))
    return {
        "discovered": sum(found.values()), "discovered_by_kind": found,
        "new_by_kind": {k: v for k, v in new.items() if v},
        "deduped": len(deduped),
        "deduped_examples": deduped[:EXAMPLES],
        "enriched": sum(enriched.values()),
        "enriched_by_kind": {k: v for k, v in enriched.items() if v},
        "with_image": images,
    }


def _target_name(conn: sqlite3.Connection, target: str) -> str:
    """An activity target (an event_uid, or `<kind>:<entity_uid>`) by name."""
    kind, _, uid = target.partition(":") if ":" in target else ("event", "", target)
    if kind == "event":
        row = conn.execute("SELECT COALESCE(title, name) FROM events WHERE event_uid = ?",
                           (uid,)).fetchone()
    elif kind in ENTITY_TABLES:
        row = conn.execute(f"SELECT name FROM {ENTITY_TABLES[kind]} WHERE entity_uid = ?",
                           (uid,)).fetchone()
    else:
        row = None
    return f"{kind} {row[0] if row and row[0] else uid[:12]}"


def _ambiguous(conn: sqlite3.Connection) -> list[str]:
    return [f"{a['kind']} {a['name'] or a['uid'][:12]} — may be live as "
            f"{', '.join(a.get('candidates') or [])} ({a.get('matched_by') or '?'})"
            for a in live_index.ambiguous(conn)]


def _holds(conn: sqlite3.Connection) -> dict:
    from engine import review_hold, seeds
    held = review_hold.held(conn)
    by_kind = Counter(k.label for h in held for k in h["kinds"])
    held_seeds = seeds.held(conn)
    return {"events": len(held), "by_kind": dict(by_kind), "seeds": len(held_seeds),
            "seed_examples": [f"{s['name']} (#{s['seed_id']}) — {s['hold_reason']}"
                              for s in held_seeds[:EXAMPLES]]}


def _failures(conn: sqlite3.Connection, since: str) -> dict:
    stuck = _rows(conn, "SELECT COALESCE(title, name) AS t, state FROM events "
                        "WHERE state LIKE 'stuck-%' ORDER BY state_updated_at DESC")
    wq = _rows(conn, "SELECT next_action, last_error FROM event_workqueue "
                     "WHERE last_error IS NOT NULL AND last_attempt_at >= ? "
                     "ORDER BY last_attempt_at DESC", (since,))
    runs = _rows(conn, "SELECT source, error FROM source_runs WHERE ok = 0 AND started_at >= ? "
                       "ORDER BY started_at DESC", (since,))
    examples = ([f"stuck: {r['t']} ({r['state']})" for r in stuck[:EXAMPLES]]
                + [f"workqueue {r['next_action']}: {(r['last_error'] or '')[:160]}"
                   for r in wq[:EXAMPLES]]
                + [f"source run {r['source']}: {(r['error'] or '')[:160]}"
                   for r in runs[:EXAMPLES]])
    return {"stuck": len(stuck), "workqueue_errors": len(wq),
            "source_runs_failed": len(runs), "examples": examples}
