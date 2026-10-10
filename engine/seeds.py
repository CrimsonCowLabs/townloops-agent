"""Seed lifecycle writes and the autonomous seed review — what
event-seed-discovery's `probe`, `review`, `retire` and `restore` run.

`review` (autonomous mode; the judgement is engine/autonomy's): proposed
seeds are dry-run (`trial`) — scrape, extract, judge what the page lists —
and approved or rejected on the result; approved seeds get their role
(primary/syndicator) and trust refreshed, and `autonomy.judge_seed` decides
the rest. A seed that looks dead is first looked for again on its own site
(`find_moved`: where its page now redirects, else a same-site search), each
candidate passing the same trial; found → the seed URL moves, else retired.

Network and LLM calls come in as `Tools`, so the skill (and its tests)
supply them.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from typing import Callable, Optional
from urllib.parse import urlparse

from engine import autonomy, event_dedup, feed_probe
from engine.derive_helpers import iso_utc_now, seed_ref
from engine.event_extract import build_extraction_prompt, parse_json_array, utc_times


@dataclass(frozen=True)
class Tools:
    fetch: Callable[[str], tuple[bool, str, str]]          # url → (ok, markdown, err)
    scrape: Callable[[str], tuple[bool, int, str, str]]    # url → (ok, status, raw html, final url)
    search: Callable[[str], list[dict]]                     # query → [{url, title, …}]
    llm: Callable[..., tuple[bool, str, str]]              # prompt → (ok, stdout, err)


def record_feed(conn: sqlite3.Connection, seed_url: str, html: str, final_url: str) -> dict:
    """Probe the seed's page for a machine-readable feed and record it."""
    found = feed_probe.detect(html, final_url)
    detail = {"feeds": found["feeds"]}
    if final_url != seed_url:   # short link / redirect: feeds resolve against the target
        detail["resolved_url"] = final_url
    conn.execute(
        "UPDATE event_seeds SET feed_type = ?, feed_url = ?, feed_detail = ?, feed_probed_at = ? "
        "WHERE url = ?",
        (found["type"], found["url"], json.dumps(detail), iso_utc_now(), seed_url))
    return found


def set_status(conn: sqlite3.Connection, seed: dict, status: str, d: autonomy.Decision) -> None:
    """Move a seed to `status`, recording the decision behind it."""
    conn.execute("UPDATE event_seeds SET status = ?, status_note = ?, status_changed_at = ? "
                 "WHERE seed_id = ?", (status, f"[autonomy] {d.reason}", iso_utc_now(),
                                       seed["seed_id"]))
    autonomy.record(conn, "seed", seed_ref(seed["name"]), d)
    conn.commit()


def trial(conn: sqlite3.Connection, profile: dict, mode: autonomy.Settings, tools: Tools,
          name: str, url: str) -> autonomy.Decision:
    """Dry-run one seed URL: scrape, extract, judge what it lists."""
    ok, md, err = tools.fetch(url)
    if not ok:
        return autonomy.Decision("reject", "trial-unreachable", {"url": url, "error": err[:200]})
    failure = autonomy.page_failure(md)
    if failure:
        return autonomy.judge_trial([], [], failure, mode)
    ok, out, err = tools.llm(build_extraction_prompt(profile, name, url, md))
    if not ok:
        return autonomy.Decision("defer", "ai-unavailable", {"url": url})
    items = []
    for e in parse_json_array(out):
        if not (isinstance(e, dict) and e.get("title") and e.get("date")):
            continue
        start, end = utc_times(e.get("date"), e.get("end_at"))
        items.append({"title": e["title"], "date": e["date"], "date_utc": start,
                      "end_at_utc": end, "venue": e.get("venue"), "location": e.get("location"),
                      "source_url": e.get("source_url")})
    if not items:
        return autonomy.judge_trial([], [], "empty-listing", mode)
    ctx = autonomy.judge_context(conn, profile, mode)
    d = autonomy.judge_trial(items, autonomy.judge_items(items, md, ctx, tools.llm), None, mode)
    d.evidence["url"] = url
    return d


def find_moved(conn: sqlite3.Connection, profile: dict, mode: autonomy.Settings, tools: Tools,
               seed: dict) -> tuple[Optional[str], list[str]]:
    """A working calendar URL for a dead seed on its own site: where the seed
    page now redirects, else what a same-site search finds. Each candidate
    must pass the same trial as a proposed seed."""
    tried: list[str] = []
    candidates: list[str] = []
    ok, code, html, final_url = tools.scrape(seed["url"])
    if ok:
        record_feed(conn, seed["url"], html, final_url)
        if final_url and final_url.rstrip("/") != seed["url"].rstrip("/"):
            candidates.append(final_url)
    host = (urlparse(seed["url"]).hostname or "").lower()
    if host:
        for r in tools.search(f"site:{host.removeprefix('www.')} events calendar"):
            u = (r.get("url") or "").strip()
            if u and (urlparse(u).hostname or "").lower().removeprefix("www.") == \
                    host.removeprefix("www.") and u.rstrip("/") != seed["url"].rstrip("/"):
                candidates.append(u)
    for url in list(dict.fromkeys(candidates))[:3]:
        tried.append(url)
        if conn.execute("SELECT 1 FROM event_seeds WHERE url = ? AND seed_id != ?",
                        (url, seed["seed_id"])).fetchone():
            continue
        if trial(conn, profile, mode, tools, seed["name"], url).verdict == "approve":
            return url, tried
    return None, tried


def review(conn: sqlite3.Connection, profile: dict, mode: autonomy.Settings, tools: Tools, *,
           trials: int, moves: int) -> dict:
    """One autonomous review run: trial up to `trials` proposed seeds (those
    with a feed first), then re-judge every approved seed — role, trust,
    retire — looking for at most `moves` moved calendars."""
    out = {"trialed": 0, "approved": 0, "rejected": 0, "deferred": 0, "roles": 0,
           "retired": 0, "relocated": 0}
    proposed = [dict(r) for r in conn.execute(
        "SELECT * FROM event_seeds WHERE status = 'proposed' "
        "ORDER BY feed_url IS NULL, trial_at IS NOT NULL, trial_at, seed_id LIMIT ?",
        (trials,))]
    for seed in proposed:
        out["trialed"] += 1
        d = trial(conn, profile, mode, tools, seed["name"], seed["url"])
        # The trial score is the trust a seed starts with (an approved seed's
        # is then earned from moderator outcomes).
        conn.execute("UPDATE event_seeds SET trial_at = ?, "
                     "trust_score = COALESCE(?, trust_score) WHERE seed_id = ?",
                     (iso_utc_now(), d.evidence.get("trust"), seed["seed_id"]))
        if d.verdict == "approve":
            set_status(conn, seed, "approved", d)
            out["approved"] += 1
        elif d.verdict == "reject":
            set_status(conn, seed, "rejected", d)
            out["rejected"] += 1
        else:
            autonomy.record(conn, "seed", seed_ref(seed["name"]), d)
            out["deferred"] += 1
        conn.commit()

    autonomy.refresh_trust(conn)
    for seed in [dict(r) for r in conn.execute("SELECT * FROM event_seeds WHERE status = 'approved'")]:
        role = autonomy.classify_role(conn, seed, mode)
        if role.verdict in (event_dedup.PRIMARY, event_dedup.SYNDICATOR) \
                and seed.get("source_role") != role.verdict:
            conn.execute("UPDATE event_seeds SET source_role = ? WHERE seed_id = ?",
                         (role.verdict, seed["seed_id"]))
            autonomy.record(conn, "seed-role", seed_ref(seed["name"]), role)
            seed["source_role"] = role.verdict
            autonomy.refresh_trust(conn, [seed["name"]])   # a syndicator's dedup losses don't count
            out["roles"] += 1
        d = autonomy.judge_seed(conn, seed, mode)
        if d.verdict == "retire":
            set_status(conn, seed, "retired", d)
            out["retired"] += 1
        elif d.verdict == "check-moved" and out["relocated"] + out["retired"] < moves:
            moved, tried = find_moved(conn, profile, mode, tools, seed)
            if moved:
                now = iso_utc_now()
                conn.execute(
                    "UPDATE event_seeds SET previous_url = url, url = ?, status_note = ?, "
                    "  status_changed_at = ?, health_status = NULL, health_note = NULL, "
                    "  health_baseline = NULL, health_flagged_at = NULL, health_since = ? "
                    "WHERE seed_id = ?",
                    (moved, f"[autonomy] calendar moved from {seed['url']}", now, now,
                     seed["seed_id"]))
                autonomy.record(conn, "seed", seed_ref(seed["name"]), autonomy.Decision(
                    "relocate", "calendar-moved", {**d.evidence, "from": seed["url"],
                                                   "to": moved}))
                conn.commit()
                out["relocated"] += 1
            else:
                set_status(conn, seed, "retired", autonomy.Decision(
                    "retire", d.reason, {**d.evidence, "searched": tried}))
                out["retired"] += 1
    conn.commit()
    return out
