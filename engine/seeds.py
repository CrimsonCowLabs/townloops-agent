"""Seed lifecycle writes and the autonomous seed review — what
event-seed-discovery's `probe`, `review`, `retire`, `restore`, `move`,
`hold` and `release` run.

Maintenance (`move`, `hold`, `release`, `restore`) is how an agent or the
operator fixes a seed — never raw SQL: each change is reversible and its
decision recorded (`autonomy why <seed>`). A `held` seed is out of service
on purpose (`HOLD_REASONS`): event-find never scrapes it and `review` never
touches it.

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

from engine import autonomy, blocked_page, event_dedup, feed_probe, seed_adapters
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


def set_adapter(conn: sqlite3.Connection, seed_id: int, adapter: str,
                config: Optional[dict]) -> None:
    """How event-find reads the seed (engine/seed_adapters): `generic`
    clears the adapter and its config. ValueError for an unknown adapter or
    seed. Commits."""
    if adapter != seed_adapters.GENERIC and adapter not in seed_adapters.ADAPTERS:
        raise ValueError(f"unknown adapter {adapter!r} "
                         f"(known: {', '.join(sorted(seed_adapters.ADAPTERS))}, generic)")
    if config is not None and not isinstance(config, dict):
        raise ValueError("adapter config must be a JSON object")
    generic = adapter == seed_adapters.GENERIC
    cur = conn.execute("UPDATE event_seeds SET adapter = ?, adapter_config = ? WHERE seed_id = ?",
                       (None if generic else adapter,
                        None if generic or not config else json.dumps(config), seed_id))
    if not cur.rowcount:
        raise ValueError(f"no seed {seed_id}")
    conn.commit()


HOLD_REASONS = ("manual-only", "awaiting-adapter")
# Verdicts that change a seed's URL (review's relocation, `move`) — what a
# consumer of the decision log checks for "the seed reads another page now".
RELOCATE, MOVE = "relocate", "move"
URL_CHANGES = frozenset({RELOCATE, MOVE})


def set_status(conn: sqlite3.Connection, seed: dict, status: str, d: autonomy.Decision, *,
               note: Optional[str] = None, hold_reason: Optional[str] = None) -> None:
    """Move a seed to `status`, recording the decision behind it. The status
    note is the decision's reason, then `note`; leaving `held` clears the
    hold reason."""
    conn.execute("UPDATE event_seeds SET status = ?, status_note = ?, status_changed_at = ?, "
                 "  hold_reason = ? WHERE seed_id = ?",
                 (status, f"[autonomy] {d.reason}" + (f": {note}" if note else ""),
                  iso_utc_now(), hold_reason, seed["seed_id"]))
    autonomy.record(conn, "seed", seed_ref(seed["name"]), d)
    conn.commit()


def _last_hold(conn: sqlite3.Connection, seed: dict) -> dict:
    """The evidence of the decision that held the seed ({} if none)."""
    d = autonomy.decisions(conn, target=seed_ref(seed["name"]), subject="seed",
                           verdict="hold", exact=True, limit=1)
    return d[0]["evidence"] if d else {}


def held(conn: sqlite3.Connection) -> list[dict]:
    """Held seeds, each with its `hold_reason` and the `hold_note` it was
    held with (from the hold decision) — the digest's and dashboard's list."""
    cur = conn.cursor()
    cur.row_factory = sqlite3.Row
    out = []
    for r in cur.execute("SELECT * FROM event_seeds WHERE status = 'held' "
                         "ORDER BY name COLLATE NOCASE"):
        seed = dict(r)
        seed["hold_note"] = _last_hold(conn, seed).get("note")
        out.append(seed)
    return out


def hold(conn: sqlite3.Connection, seed: dict, reason: str, note: Optional[str] = None) -> None:
    """Take a seed out of service (`HOLD_REASONS`) until `release`."""
    if reason not in HOLD_REASONS:
        raise ValueError(f"unknown hold reason {reason!r} (known: {', '.join(HOLD_REASONS)})")
    evidence = {"was": seed["status"], **({"note": note} if note else {})}
    set_status(conn, seed, "held", autonomy.Decision("hold", reason, evidence),
               note=note, hold_reason=reason)


def release(conn: sqlite3.Connection, seed: dict, mode: autonomy.Settings) -> str:
    """Put a held seed back. In autonomous mode it is proposed, so `review`
    trials it (first among those without a feed) before event-find reads it
    again; in supervised mode it is approved only if it was approved when
    held, else proposed for the operator. Returns the new status."""
    if seed["status"] != "held":
        raise ValueError(f"seed {seed['seed_id']} is not held (status {seed['status']})")
    held_from = _last_hold(conn, seed).get("was")
    if mode.autonomous:
        status, reason = "proposed", "back-to-trial"
    elif held_from == "approved":
        status, reason = "approved", "back-in-service"
    else:
        status, reason = "proposed", "awaiting-approval"
    conn.execute("UPDATE event_seeds SET trial_at = NULL WHERE seed_id = ?", (seed["seed_id"],))
    set_status(conn, seed, status, autonomy.Decision(
        "release", reason, {"hold_reason": seed.get("hold_reason"), "held_from": held_from,
                            "to": status}))
    return status


def _set_url(conn: sqlite3.Connection, seed_id: int, url: str,
             note: Optional[str] = None) -> None:
    """The seed reads `url` now, the old one kept as `previous_url`; its
    health baseline was the old page's, so it starts again. `note` replaces
    the status note (kept without one — a held seed's says why). Caller
    commits."""
    now = iso_utc_now()
    conn.execute(
        "UPDATE event_seeds SET previous_url = url, url = ?, "
        "  status_note = COALESCE(?, status_note), "
        "  status_changed_at = ?, health_status = NULL, health_note = NULL, "
        "  health_baseline = NULL, health_flagged_at = NULL, health_since = ? "
        "WHERE seed_id = ?", (url, note, now, now, seed_id))


def move(conn: sqlite3.Connection, seed: dict, url: str, *,
         fetch: Callable[[str], tuple[bool, str, str]],
         read_adapter: Callable[[dict], seed_adapters.Listing] = seed_adapters.read,
         note: Optional[str] = None) -> autonomy.Decision:
    """Correct a seed's URL, the old one kept for `restore_previous_url`.
    Verified first the way event-find will read it: an adapter seed through
    its adapter at the new URL, otherwise the page through `fetch` — event-find's
    listing fetch, which fails a challenge/error page. ValueError when refused (nothing
    changes)."""
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.netloc:
        raise ValueError(f"not an http(s) URL: {url!r}")
    if url == seed["url"]:
        raise ValueError(f"seed {seed['seed_id']} already reads {url}")
    other = conn.execute("SELECT seed_id FROM event_seeds WHERE url = ? AND seed_id != ?",
                         (url, seed["seed_id"])).fetchone()
    if other:
        raise ValueError(f"seed {other[0]} already reads {url}")
    adapter = seed_adapters.adapter_of(seed)
    evidence = {"from": seed["url"], "to": url}
    if adapter != seed_adapters.GENERIC:
        try:
            listing = read_adapter({**seed, "url": url})
        except seed_adapters.AdapterError as e:
            raise ValueError(f"{adapter} adapter can't read {url}: {e}") from None
        evidence.update(checked=f"adapter:{adapter}", events=len(listing.events))
    else:
        ok, md, err = fetch(url)   # a challenge or error page is a failed read
        if not ok:
            raise ValueError(f"{url} is not a readable page: {err}"[:300])
        evidence.update(checked="page", chars=len(md))
    if note:
        evidence["note"] = note
    d = autonomy.Decision(MOVE, "url-corrected", evidence)
    _set_url(conn, seed["seed_id"], url)
    autonomy.record(conn, "seed", seed_ref(seed["name"]), d)
    conn.commit()
    return d


def restore(conn: sqlite3.Connection, seed: dict, *, previous_url: bool = False) -> str:
    """Undo a retirement or rejection (→ approved) and, with `previous_url`,
    a `move` or relocation — the URLs swap back, any other status kept.
    ValueError when there is nothing to undo. Returns the status."""
    undo_status = seed["status"] in ("retired", "rejected")
    if previous_url and not seed.get("previous_url") and not undo_status:
        raise ValueError(f"seed {seed['seed_id']} has no previous URL")
    if not previous_url and not undo_status:
        raise ValueError(f"seed {seed['seed_id']} is not retired or rejected"
                         + (" — a held seed comes back with release"
                            if seed["status"] == "held" else ""))
    evidence = {"was": seed["status"]}
    if previous_url and seed.get("previous_url"):
        evidence.update({"from": seed["url"], "to": seed["previous_url"]})
        _set_url(conn, seed["seed_id"], seed["previous_url"])
    d = autonomy.Decision("restore", "operator", evidence)
    if undo_status:
        set_status(conn, seed, "approved", d)
        return "approved"
    autonomy.record(conn, "seed", seed_ref(seed["name"]), d)
    conn.commit()
    return seed["status"]


def _trial_failure(blocked: str) -> str:
    """A blocked page's reason as the trial's: `not-found`, `challenge` or
    `empty-page` (an empty widget too)."""
    if blocked == blocked_page.NOT_FOUND or blocked in (blocked_page.http_status(404),
                                                        blocked_page.http_status(410)):
        return "not-found"
    if blocked in (blocked_page.NEAR_EMPTY, blocked_page.NO_ACTION):
        return "empty-page"
    return "challenge"


def trial(conn: sqlite3.Connection, profile: dict, mode: autonomy.Settings, tools: Tools,
          name: str, url: str) -> autonomy.Decision:
    """Dry-run one seed URL: scrape, extract, judge what it lists."""
    ok, md, err = tools.fetch(url)
    if not ok:
        return autonomy.Decision("reject", "trial-unreachable", {"url": url, "error": err[:200]})
    blocked = blocked_page.reason(md)
    if blocked:
        d = autonomy.judge_trial([], [], _trial_failure(blocked), mode)
        d.evidence.update({"url": url, "page": blocked})
        return d
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
                _set_url(conn, seed["seed_id"], moved,
                         f"[autonomy] calendar moved from {seed['url']}")
                autonomy.record(conn, "seed", seed_ref(seed["name"]), autonomy.Decision(
                    RELOCATE, "calendar-moved", {**d.evidence, "from": seed["url"],
                                                   "to": moved}))
                conn.commit()
                out["relocated"] += 1
            else:
                set_status(conn, seed, "retired", autonomy.Decision(
                    "retire", d.reason, {**d.evidence, "searched": tried}))
                out["retired"] += 1
    conn.commit()
    return out
