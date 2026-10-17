"""Seed lifecycle writes and the autonomous seed review — what
event-seed-discovery's `probe`, `set-adapter`, `review`, `retire`,
`restore`, `move`, `hold` and `release` run.

`set_adapter` puts a seed on a deterministic reader (engine/seed_adapters)
only after a dry run through it returns events; review's `assign_adapters`
does the same for a seed whose probed feed an adapter reads — assigning it
in autonomous mode, proposing it (nightly digest) in supervised mode.

Maintenance (`move`, `hold`, `release`, `restore`) is how an agent or the
operator fixes a seed — never raw SQL: each change is reversible and its
decision recorded (`autonomy why <seed>`). A `held` seed is out of service
on purpose (`HOLD_REASONS`): event-find never scrapes it and `review` never
touches it — except to assign an `awaiting-adapter` seed the adapter it
waits for, which releases it.

`review` (autonomous mode; the judgement is engine/autonomy's): adapters
are assigned first, then proposed seeds are dry-run (`trial`) — scrape,
extract, judge what the page lists — and approved or rejected on the result; approved seeds get their role
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
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional
from urllib.parse import urlparse

from engine import autonomy, blocked_page, event_dedup, event_provenance, feed_probe, seed_adapters
from engine.derive_helpers import is_http_url, iso_utc_now, seed_ref
from engine.event_extract import build_extraction_prompt, utc_times


@dataclass(frozen=True)
class Tools:
    fetch: Callable[[str], tuple[bool, str, str]]          # url → (ok, markdown, err)
    scrape: Callable[[str], tuple[bool, int, str, str]]    # url → (ok, status, raw html, final url)
    search: Callable[[str], list[dict]]                     # query → [{url, title, …}]
    llm: Callable[..., tuple[bool, str, str]]              # prompt → (ok, stdout, err)
    read_adapter: Callable[[dict], seed_adapters.Listing] = seed_adapters.read


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


# Adapter decisions are subject "seed-adapter", role decisions "seed-role",
# so neither reads as a seed status change. Adapter verdicts: `assign` (a
# trial passed, the adapter is set), `revert` (back to generic),
# `keep-generic` (review's trial failed or was empty) and, in supervised
# mode, `await-operator` — review's proposal (autonomy.judge_adapter).
ADAPTER_SUBJECT, ROLE_SUBJECT = "seed-adapter", "seed-role"
ASSIGN, REVERT = "assign", "revert"
# A failed adapter trial (network, a bad page) is tried again after this
# many days; an empty one only once the feed is probed again.
ADAPTER_RETRY_DAYS = 7


@dataclass(frozen=True)
class AdapterChange:
    """What `set-adapter` did: `status` is the seed's after it (a held
    `awaiting-adapter` seed is released); `changed` False for a no-op."""
    adapter: str
    status: str
    trial_events: Optional[int] = None
    changed: bool = True


def _config_json(config: Optional[dict]) -> Optional[str]:
    return json.dumps(config) if config else None


def _usable(events: list) -> int:
    """Events event-find would keep (a title and a date)."""
    return sum(1 for e in events if isinstance(e, dict) and e.get("title") and e.get("date"))


def adapter_trial(seed: dict, adapter: str, config: Optional[dict],
                  read_adapter: Callable[[dict], seed_adapters.Listing], **context) -> dict:
    """Dry-run `adapter` on the seed as it would read it — nothing is
    written. The trial's evidence: the usable `trial_events`, or the
    `error` the read failed with; `context` is carried along."""
    evidence = {"adapter": adapter, "config": config or None,
                "from": seed_adapters.adapter_of(seed), **context}
    try:
        listing = read_adapter({**seed, "adapter": adapter, "adapter_config": _config_json(config)})
    except seed_adapters.AdapterError as e:
        return {**evidence, "error": str(e)[:300]}
    return {**evidence, "trial_events": _usable(listing.events)}


def _assign(conn: sqlite3.Connection, seed: dict, d: autonomy.Decision,
            mode: autonomy.Settings) -> str:
    """Save a passed trial's adapter and record it; a seed held
    `awaiting-adapter` was waiting for exactly this, so it is released —
    one transaction (the caller has nothing uncommitted). Returns the
    seed's status."""
    status = seed["status"]
    if not conn.in_transaction:
        conn.execute("BEGIN")
    try:
        conn.execute("UPDATE event_seeds SET adapter = ?, adapter_config = ? WHERE seed_id = ?",
                     (d.evidence["adapter"], _config_json(d.evidence.get("config")),
                      seed["seed_id"]))
        autonomy.record(conn, ADAPTER_SUBJECT, seed_ref(seed["name"]), d)
        if status == "held" and seed.get("hold_reason") == "awaiting-adapter":
            status = release(conn, seed, mode, commit=False)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return status


def set_adapter(conn: sqlite3.Connection, seed: dict, adapter: str, config,
                mode: autonomy.Settings, *,
                read_adapter: Callable[[dict], seed_adapters.Listing] = seed_adapters.read
                ) -> AdapterChange:
    """How event-find reads the seed (engine/seed_adapters). A registered
    adapter with a config its params accept is dry-run first and saved only
    when the trial returns usable events; `generic` clears the adapter and
    its config (no trial; a no-op on a generic seed). ValueError when
    refused — nothing changes. Commits."""
    if seed["status"] in ("rejected", "retired"):
        raise ValueError(f"seed {seed['seed_id']} is {seed['status']} — bring it back with "
                         f"`restore --seed-id {seed['seed_id']}` first")
    if adapter == seed_adapters.GENERIC:
        was = seed_adapters.adapter_of(seed)
        if was == seed_adapters.GENERIC:
            return AdapterChange(adapter, seed["status"], changed=False)
        conn.execute("UPDATE event_seeds SET adapter = NULL, adapter_config = NULL "
                     "WHERE seed_id = ?", (seed["seed_id"],))
        autonomy.record(conn, ADAPTER_SUBJECT, seed_ref(seed["name"]), autonomy.Decision(
            REVERT, "back-to-generic", {"from": was, "to": adapter}))
        conn.commit()
        return AdapterChange(adapter, seed["status"])
    if adapter not in seed_adapters.ADAPTERS:
        raise ValueError(f"unknown adapter {adapter!r} "
                         f"(known: {', '.join(sorted(seed_adapters.ADAPTERS))}, generic)")
    if config is not None and not isinstance(config, dict):
        raise ValueError("adapter config must be a JSON object")
    seed_adapters.check_config(adapter, config or {})
    trial = adapter_trial(seed, adapter, config, read_adapter)
    if trial.get("error") or not trial["trial_events"]:
        why = trial.get("error") or "the feed returned no usable events (title and date)"
        raise ValueError(f"{adapter} trial failed, seed {seed['seed_id']} unchanged: {why}")
    status = _assign(conn, seed, autonomy.Decision(ASSIGN, "trial-passed", trial), mode)
    return AdapterChange(adapter, status, trial["trial_events"])


def _last_adapter_decision(conn: sqlite3.Connection, seed: dict) -> Optional[dict]:
    d = autonomy.decisions(conn, target=seed_ref(seed["name"]), subject=ADAPTER_SUBJECT,
                           exact=True, limit=1)
    return d[0] if d else None


def _adapter_settled(conn: sqlite3.Connection, seed: dict, mode: autonomy.Settings) -> bool:
    """Whether review leaves the seed's adapter alone: someone reverted it
    to generic (for good), or it was tried since its feed was last probed —
    a failed trial only for `ADAPTER_RETRY_DAYS`, a supervised proposal only
    while the town is supervised."""
    d = _last_adapter_decision(conn, seed)
    if not d:
        return False
    if d["verdict"] == REVERT:
        return True
    if d["verdict"] == autonomy.AWAIT and mode.autonomous:
        return False
    if seed.get("feed_probed_at") and d["ts"] < seed["feed_probed_at"]:
        return False
    if d["reason"] == "adapter-trial-failed":
        retry = datetime.now(timezone.utc) - timedelta(days=ADAPTER_RETRY_DAYS)
        return d["ts"] >= retry.isoformat(timespec="seconds")
    return True


def assign_adapters(conn: sqlite3.Connection, mode: autonomy.Settings, *,
                    read_adapter: Callable[[dict], seed_adapters.Listing] = seed_adapters.read,
                    limit: int) -> dict:
    """Review's adapter step: a generic seed whose probed `feed_type` an
    adapter reads gets that adapter's dry run, judged by
    `autonomy.judge_adapter` — assigned (autonomous), proposed for the
    operator (supervised), or kept generic. Held seeds count only when held
    `awaiting-adapter`."""
    out = {"adapters_tried": 0, "adapters_assigned": 0, "adapters_proposed": 0}
    for seed in [dict(r) for r in conn.execute(
            "SELECT * FROM event_seeds WHERE adapter IS NULL AND feed_type IS NOT NULL "
            "  AND (status IN ('proposed', 'approved') "
            "       OR (status = 'held' AND hold_reason = 'awaiting-adapter')) "
            "ORDER BY status = 'held' DESC, seed_id")]:
        adapter = seed_adapters.for_feed(seed["feed_type"])
        if not adapter or _adapter_settled(conn, seed, mode):
            continue
        if out["adapters_tried"] >= limit:
            break
        out["adapters_tried"] += 1
        d = autonomy.judge_adapter(adapter_trial(
            seed, adapter, None, read_adapter,
            feed_type=seed["feed_type"], feed_url=seed.get("feed_url")), mode)
        if d.verdict == ASSIGN:
            _assign(conn, seed, d, mode)
            out["adapters_assigned"] += 1
            continue
        out["adapters_proposed"] += d.verdict == autonomy.AWAIT
        autonomy.record(conn, ADAPTER_SUBJECT, seed_ref(seed["name"]), d)
        conn.commit()
    return out


def adapter_proposals(conn: sqlite3.Connection) -> list[dict]:
    """Supervised review's open proposals — a seed still generic whose
    latest adapter decision is the proposal — for the nightly digest."""
    out = []
    for seed in [dict(r) for r in conn.execute(
            "SELECT seed_id, name, status FROM event_seeds WHERE adapter IS NULL "
            "AND status NOT IN ('rejected', 'retired') ORDER BY name COLLATE NOCASE")]:
        d = _last_adapter_decision(conn, seed)
        if d and d["verdict"] == autonomy.AWAIT:
            e = d["evidence"]
            out.append({**seed, "adapter": e.get("adapter"), "feed_type": e.get("feed_type"),
                        "trial_events": e.get("trial_events"),
                        "command": f"event-seed-discovery set-adapter --seed-id "
                                   f"{seed['seed_id']} --adapter {e.get('adapter')}"})
    return out


HOLD_REASONS = ("manual-only", "awaiting-adapter")
# Verdicts that change a seed's URL (review's relocation, `move`) — what a
# consumer of the decision log checks for "the seed reads another page now".
RELOCATE, MOVE = "relocate", "move"
URL_CHANGES = frozenset({RELOCATE, MOVE})


def set_status(conn: sqlite3.Connection, seed: dict, status: str, d: autonomy.Decision, *,
               note: Optional[str] = None, hold_reason: Optional[str] = None,
               commit: bool = True) -> None:
    """Move a seed to `status`, recording the decision behind it. The status
    note is the decision's reason, then `note`; leaving `held` clears the
    hold reason. `commit=False` leaves the transaction to the caller."""
    conn.execute("UPDATE event_seeds SET status = ?, status_note = ?, status_changed_at = ?, "
                 "  hold_reason = ? WHERE seed_id = ?",
                 (status, f"[autonomy] {d.reason}" + (f": {note}" if note else ""),
                  iso_utc_now(), hold_reason, seed["seed_id"]))
    autonomy.record(conn, "seed", seed_ref(seed["name"]), d)
    if commit:
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


def release(conn: sqlite3.Connection, seed: dict, mode: autonomy.Settings, *,
            commit: bool = True) -> str:
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
                            "to": status}), commit=commit)
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
    if not is_http_url(url):
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
          name: str, url: str, seed: Optional[dict] = None) -> autonomy.Decision:
    """Dry-run one seed URL: scrape, extract, judge what it lists. An
    adapter `seed` is read the way event-find will read it — through its
    adapter."""
    if seed and seed_adapters.adapter_of(seed) != seed_adapters.GENERIC:
        try:
            listing = tools.read_adapter(seed)
        except seed_adapters.AdapterError as e:
            return autonomy.Decision("reject", "trial-unreachable",
                                     {"url": url, "adapter": seed["adapter"],
                                      "error": str(e)[:200]})
        return _judge_listing(conn, profile, mode, tools, listing.events, listing.page, url)
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
    return _judge_listing(conn, profile, mode, tools,
                          event_provenance.listing_events(out, md, url), md, url)


def _judge_listing(conn: sqlite3.Connection, profile: dict, mode: autonomy.Settings,
                   tools: Tools, events: list, page: str, url: str) -> autonomy.Decision:
    """The trial's verdict on what a listing returned (`page`: the text the
    judgement quote-checks against)."""
    items = []
    for e in events:
        if not (isinstance(e, dict) and e.get("title") and e.get("date")):
            continue
        start, end = utc_times(e.get("date"), e.get("end_at"))
        items.append({"title": e["title"], "date": e["date"], "date_utc": start,
                      "end_at_utc": end, "venue": e.get("venue"), "location": e.get("location"),
                      "source_url": e.get("source_url")})
    if not items:
        return autonomy.judge_trial([], [], "empty-listing", mode)
    ctx = autonomy.judge_context(conn, profile, mode)
    d = autonomy.judge_trial(items, autonomy.judge_items(items, page, ctx, tools.llm), None, mode)
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
           trials: int, moves: int, adapters: int) -> dict:
    """One autonomous review run: assign up to `adapters` seeds the adapter
    their probed feed names (`assign_adapters` — a held `awaiting-adapter`
    seed is released by it), trial up to `trials` proposed seeds (those with
    a feed first), then re-judge every approved seed — role, trust, retire —
    looking for at most `moves` moved calendars."""
    out = {"trialed": 0, "approved": 0, "rejected": 0, "deferred": 0, "roles": 0,
           "retired": 0, "relocated": 0,
           **assign_adapters(conn, mode, read_adapter=tools.read_adapter, limit=adapters)}
    proposed = [dict(r) for r in conn.execute(
        "SELECT * FROM event_seeds WHERE status = 'proposed' "
        "ORDER BY feed_url IS NULL, trial_at IS NOT NULL, trial_at, seed_id LIMIT ?",
        (trials,))]
    for seed in proposed:
        out["trialed"] += 1
        d = trial(conn, profile, mode, tools, seed["name"], seed["url"], seed)
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
            autonomy.record(conn, ROLE_SUBJECT, seed_ref(seed["name"]), role)
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
