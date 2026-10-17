"""Autonomy policy: the calls a local operator used to make, made by the agent.

A town runs in one of two modes, read from town-profile.yaml on every call
(nothing caches it across runs):

    operating_mode: supervised   # default — today's behaviour, every gate waits
    operating_mode: autonomous   # the agent decides; TownLoops moderation is the
                                 # only human gate

Every decision is a `Decision(verdict, reason, evidence)` and is written to
`agent_activity` (worker 'autonomy', action '<subject>:<verdict>', detail the
reason + evidence as JSON) by `record`, so any call can be explained later
(the `autonomy` skill's `why`) and summed up in the nightly digest. In
supervised mode the gate functions return `AWAIT` — "await operator".

Null beats wrong: an item the evidence can't settle is dropped
(`insufficient-evidence`), never guessed; an AI call that fails defers the
item to the next run instead.

Submissions (autonomous only) go through `GatedClient`: before a create it
checks the gate (pause flag, open circuit, cap on outstanding pending
submissions); after it, the create must come back `pending` — anything else
means the key can publish, so the circuit opens, the operator is alerted
and nothing else is submitted (fail closed). Every create is recorded in
`submissions`, which the moderation poll (engine/moderation.py) follows to
its outcome.

Entities (venues, hosts, people) are submitted pending too — TownLoops
gates entity creates like events (townloops ADR 0009). The town profile can
hold them (`autonomy.entity_moderation: false`); an autonomous event then
ships with its venue's name only and new entities wait.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Iterable, Optional

from engine.derive_helpers import (
    dict_rows as _rows, iso_utc_now, load_town_profile, normalize_name, parse_extra_json,
    parse_instant, seed_ref,
)


SUPERVISED, AUTONOMOUS = "supervised", "autonomous"
AWAIT = "await-operator"
WORKER = "autonomy"


# --------------------------------------------------------------------------
# Settings
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class Settings:
    mode: str = SUPERVISED
    paused: bool = False
    entity_moderation: bool = True
    max_pending: int = 100
    reject_rate: float = 0.5           # circuit breaker: rejected share of recent outcomes
    reject_window: int = 20            # … over this many recent outcomes
    reject_min_outcomes: int = 10      # … once at least this many are in
    horizon_days: int = 120
    seed_min_runs: int = 4             # successful runs before a seed is judged
    seed_dead_runs: int = 4            # consecutive zero/sharply-down runs → retire review
    seed_min_outcomes: int = 5         # decided submissions before trust demotes or retires
    # Seed trust (0..1; a new seed starts at 0.5 — see seed_trust) decides:
    seed_trust_approve: float = 0.2    # … a proposed seed's trial passes at this score
    seed_trust_syndicator: float = 0.45  # … below this an approved seed is a syndicator
    seed_trust_retire: float = 0.3     # … below this it is retired
    series_reject_streak: int = 3
    series_publish_streak: int = 3     # published in a row → a well-established Series
    stuck_retries: int = 2             # backoff retries before a stuck event is resolved
    verdict_max_age_days: int = 30     # an older discovery verify verdict is re-verified

    @property
    def autonomous(self) -> bool:
        return self.mode == AUTONOMOUS


def settings(profile: Optional[dict] = None) -> Settings:
    """The town's mode and autonomy knobs. Unknown or missing values keep
    the defaults; anything but `autonomous` is supervised."""
    profile = load_town_profile() if profile is None else (profile or {})
    a = profile.get("autonomy") or {}
    breaker = a.get("circuit_breaker") or {}
    seeds = a.get("seed_retirement") or {}
    trust = a.get("seed_trust") or {}
    d = Settings()

    def num(src: dict, key: str, default, cast=int):
        try:
            return cast(src[key]) if src.get(key) is not None else default
        except (TypeError, ValueError):
            return default

    return Settings(
        mode=AUTONOMOUS if profile.get("operating_mode") == AUTONOMOUS else SUPERVISED,
        paused=bool(a.get("paused")),
        entity_moderation=bool(a.get("entity_moderation", d.entity_moderation)),
        max_pending=num(a, "max_pending", d.max_pending),
        reject_rate=num(breaker, "reject_rate", d.reject_rate, float),
        reject_window=num(breaker, "window", d.reject_window),
        reject_min_outcomes=num(breaker, "min_outcomes", d.reject_min_outcomes),
        horizon_days=num(a, "horizon_days", d.horizon_days),
        seed_min_runs=num(seeds, "min_runs", d.seed_min_runs),
        seed_dead_runs=num(seeds, "dead_runs", d.seed_dead_runs),
        seed_min_outcomes=num(seeds, "min_outcomes", d.seed_min_outcomes),
        seed_trust_approve=num(trust, "approve", d.seed_trust_approve, float),
        seed_trust_syndicator=num(trust, "syndicator", d.seed_trust_syndicator, float),
        seed_trust_retire=num(trust, "retire", d.seed_trust_retire, float),
        series_reject_streak=num(a, "series_reject_streak", d.series_reject_streak),
        series_publish_streak=num(a, "series_publish_streak", d.series_publish_streak),
        stuck_retries=num(a, "stuck_retries", d.stuck_retries),
        verdict_max_age_days=num(a, "verdict_max_age_days", d.verdict_max_age_days),
    )


def is_autonomous() -> bool:
    return settings().autonomous


def inactive_message(skill: str) -> dict:
    """What a supervised-mode operator skill reports in autonomous mode."""
    return {"inactive": True, "skill": skill, "mode": AUTONOMOUS,
            "message": f"{skill} is a supervised-mode skill — this town runs in autonomous "
                       "mode, where the agent makes this call itself and TownLoops "
                       "moderation is the review step. Ask `autonomy why <id>` about a "
                       "decision instead."}


# --------------------------------------------------------------------------
# Decisions + the decision log
# --------------------------------------------------------------------------

@dataclass
class Decision:
    verdict: str
    reason: str
    evidence: dict = field(default_factory=dict)


def await_operator(why: str = "supervised mode", evidence: Optional[dict] = None) -> Decision:
    return Decision(AWAIT, "supervised", {"note": why, **(evidence or {})})


def record(conn: sqlite3.Connection, subject: str, target: str, d: Decision) -> None:
    """Write one decision to the activity log (caller commits)."""
    conn.execute(
        "INSERT INTO agent_activity (ts, worker, action, target, detail, ok) "
        "VALUES (?, ?, ?, ?, ?, 1)",
        (iso_utc_now(), WORKER, f"{subject}:{d.verdict}", str(target)[:200],
         json.dumps({"reason": d.reason, "evidence": d.evidence}, ensure_ascii=False,
                    default=str)))


def decisions(conn: sqlite3.Connection, *, target: Optional[str] = None,
              subject: Optional[str] = None, verdict: Optional[str] = None,
              since: Optional[str] = None, exact: bool = False,
              limit: int = 50) -> list[dict]:
    """Recorded decisions, newest first. `target` matches a prefix (an event
    uid prefix, a seed id, a slug), or with `exact` only itself."""
    where, args = ["worker = ?"], [WORKER]
    if target and exact:
        where.append("target = ?")
        args.append(str(target)[:200])
    elif target:
        where.append("target LIKE ?")
        args.append(target.replace("%", "") + "%")
    if subject and verdict:
        where.append("action = ?")
        args.append(f"{subject}:{verdict}")
    elif subject:
        where.append("action LIKE ?")
        args.append(subject + ":%")
    if since:
        where.append("ts >= ?")
        args.append(since)
    out = []
    for r in _rows(conn, f"SELECT ts, action, target, detail FROM agent_activity "
                         f"WHERE {' AND '.join(where)} ORDER BY id DESC LIMIT ?",
                   (*args, limit)):
        subj, _, verdict = r["action"].partition(":")
        try:
            detail = json.loads(r["detail"] or "{}")
        except json.JSONDecodeError:
            detail = {"reason": r["detail"]}
        out.append({"ts": r["ts"], "subject": subj, "verdict": verdict, "target": r["target"],
                    "reason": detail.get("reason"), "evidence": detail.get("evidence") or {}})
    return out


# --------------------------------------------------------------------------
# Circuit + submission gate
# --------------------------------------------------------------------------

def _state_get(conn: sqlite3.Connection, key: str) -> Optional[dict]:
    row = conn.execute("SELECT value FROM autonomy_state WHERE key = ?", (key,)).fetchone()
    if not row or not row[0]:
        return None
    try:
        return json.loads(row[0])
    except json.JSONDecodeError:
        return {"reason": row[0]}


def circuit(conn: sqlite3.Connection) -> Optional[dict]:
    """{reason, evidence, at} while submissions are stopped, else None."""
    return _state_get(conn, "circuit")


def open_circuit(conn: sqlite3.Connection, reason: str, evidence: dict,
                 alert: Callable[[str], None] | None = None) -> bool:
    """Stop all submissions for the town. True when this opened it (the
    operator is alerted once); False when it was already open."""
    if circuit(conn):
        return False
    conn.execute(
        "INSERT INTO autonomy_state (key, value, updated_at) VALUES ('circuit', ?, ?) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
        (json.dumps({"reason": reason, "evidence": evidence, "at": iso_utc_now()}, default=str),
         iso_utc_now()))
    record(conn, "circuit", "submissions", Decision("open", reason, evidence))
    conn.commit()
    (alert or _telegram)(
        f"[{_town()}] autonomous submissions STOPPED — {reason}. "
        f"{json.dumps(evidence, default=str)[:300]}\n"
        "Collecting and enriching continue. Fix the cause, then `autonomy resume`.")
    return True


def close_circuit(conn: sqlite3.Connection, by: str = "operator") -> bool:
    was = circuit(conn)
    conn.execute("DELETE FROM autonomy_state WHERE key = 'circuit'")
    if was:
        record(conn, "circuit", "submissions", Decision("closed", f"resumed by {by}",
                                                        {"was": was}))
    conn.commit()
    return bool(was)


def pending_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT COUNT(*) FROM submissions WHERE status = 'pending'").fetchone()[0]


def submission_gate(conn: sqlite3.Connection, s: Settings) -> Decision:
    """Whether a new submission may go to TownLoops now."""
    if not s.autonomous:
        return await_operator()
    if s.paused:
        return Decision("hold", "paused", {"setting": "autonomy.paused"})
    c = circuit(conn)
    if c:
        return Decision("hold", "circuit-open", c)
    n = pending_count(conn)
    if n >= s.max_pending:
        return Decision("hold", "pending-cap", {"pending": n, "max_pending": s.max_pending})
    return Decision("submit", "gate-open", {"pending": n})


def check_rejection_rate(conn: sqlite3.Connection, s: Settings,
                         alert: Callable[[str], None] | None = None) -> Optional[dict]:
    """Open the circuit when TownLoops rejected too much of what was
    recently decided. Returns the evidence when it opened."""
    rows = [r[0] for r in conn.execute(
        "SELECT status FROM submissions WHERE status IN ('published', 'rejected', 'merged') "
        "AND published_unreviewed = 0 ORDER BY decided_at DESC, id DESC LIMIT ?",
        (s.reject_window,)).fetchall()]
    if len(rows) < s.reject_min_outcomes:
        return None
    rejected = rows.count("rejected")
    rate = rejected / len(rows)
    if rate < s.reject_rate:
        return None
    evidence = {"rejected": rejected, "outcomes": len(rows), "rate": round(rate, 2),
                "threshold": s.reject_rate}
    return evidence if open_circuit(conn, "rejection-rate", evidence, alert) else None


class SubmissionsHeld(RuntimeError):
    """A create was refused before it reached TownLoops."""

    def __init__(self, reason: str, evidence: Optional[dict] = None):
        super().__init__(f"submissions held: {reason}")
        self.reason = reason
        self.evidence = evidence or {}


def record_submission(conn: sqlite3.Connection, resource: str, slug: str, *,
                      status: str = "pending", unreviewed: bool = False,
                      **context) -> None:
    if not slug:
        return
    cols = {k: v for k, v in context.items()
            if k in ("local_uid", "kind", "target_slug", "seed", "series_slug",
                     "discovery_source") and v is not None}
    now = iso_utc_now()
    conn.execute(
        "INSERT INTO submissions (resource, slug, status, published_unreviewed, submitted_at, "
        "  decided_at) VALUES (?, ?, ?, ?, ?, ?) ON CONFLICT (resource, slug) DO NOTHING",
        (resource, slug, status, 1 if unreviewed else 0, now, None if status == "pending" else now))
    if cols:
        conn.execute(f"UPDATE submissions SET {', '.join(f'{k} = ?' for k in cols)} "
                     "WHERE resource = ? AND slug = ?", (*cols.values(), resource, slug))


def attribute(conn: sqlite3.Connection, resource: str, slug: str, **context) -> None:
    """Fill in what a recorded submission is attributed to."""
    cols = {k: v for k, v in context.items() if v is not None}
    if cols:
        conn.execute(f"UPDATE submissions SET {', '.join(f'{k} = ?' for k in cols)} "
                     "WHERE resource = ? AND slug = ?", (*cols.values(), resource, slug))


class GatedClient:
    """A TownLoops client whose creates pass the submission gate and must
    come back `pending`. Everything else is the wrapped client's.

    `context` (kind, seed, series_slug, local_uid, target_slug,
    discovery_source) is recorded with each create; callers set it before
    creating."""

    def __init__(self, client, conn: sqlite3.Connection, s: Settings,
                 alert: Callable[[str], None] | None = None):
        self._client = client
        self._conn = conn
        self._settings = s
        self._alert = alert
        self.context: dict[str, Any] = {}

    def __getattr__(self, name):
        return getattr(self._client, name)

    def create(self, resource: str, payload: dict, *, idempotency_key: Optional[str] = None):
        if resource != "events" and not self._settings.entity_moderation:
            raise SubmissionsHeld("entity-moderation-off",
                                  {"resource": resource, "setting": "autonomy.entity_moderation"})
        gate = submission_gate(self._conn, self._settings)
        if gate.verdict != "submit":
            raise SubmissionsHeld(gate.reason, gate.evidence)
        result = self._client.create(resource, payload, idempotency_key=idempotency_key)
        row = result if isinstance(result, dict) else {}
        slug = row.get("slug") or payload.get("slug")
        status = row.get("status")
        unreviewed = status != "pending"
        ctx = dict(self.context)
        if resource != "events":
            ctx = {k: v for k, v in ctx.items() if k in ("discovery_source", "seed")}
        record_submission(self._conn, resource, slug,
                          status="published" if unreviewed else "pending",
                          unreviewed=unreviewed, **ctx)
        self._conn.commit()
        if unreviewed:
            open_circuit(self._conn, "published-without-review",
                         {"resource": resource, "slug": slug, "status": status,
                          "note": "the API key can publish — switch the town to an "
                                  "api_ingest key without publish permission"},
                         self._alert)
        return result


def gated(client, conn: sqlite3.Connection, s: Optional[Settings] = None):
    """`client` itself in supervised mode, else a GatedClient."""
    s = s or settings()
    return GatedClient(client, conn, s) if s.autonomous else client


# --------------------------------------------------------------------------
# Event quality judgement
# --------------------------------------------------------------------------

@dataclass
class JudgeContext:
    town: str
    aliases: tuple[str, ...] = ()
    zip_codes: tuple[str, ...] = ()
    known_venues: frozenset[str] = frozenset()   # normalized in-town venue names
    horizon_days: int = 120
    now: Optional[datetime] = None
    # event → slug of a live occurrence of its predicted Series on its date
    series_duplicate: Optional[Callable[[dict], Optional[str]]] = None
    # event → slug of its predicted Series when that Series is rejection-blocked
    series_blocked: Optional[Callable[[dict], Optional[str]]] = None
    # event → (slug, publish streak) of its predicted Series when well established
    series_established: Optional[Callable[[dict], Optional[tuple[str, int]]]] = None


def judge_context(conn: sqlite3.Connection, profile: dict, s: Settings, **kw) -> JudgeContext:
    names = [r[0] for r in conn.execute(
        "SELECT name FROM venues WHERE COALESCE(name, '') != '' "
        "AND (townloops_slug IS NOT NULL OR source_kind = 'live')")]
    return JudgeContext(
        town=profile.get("name") or profile.get("slug") or "",
        aliases=tuple(a for a in profile.get("name_aliases") or [] if a),
        zip_codes=tuple(str(z) for z in profile.get("zip_codes") or []),
        known_venues=frozenset(normalize_name(n) for n in names if normalize_name(n)),
        horizon_days=s.horizon_days, **kw)


# News, notices, opening hours and closures — listed on calendars, not attended.
_NON_EVENT = re.compile(
    r"\b(?:closed\b(?![- ]caption)|closures?\b|hours of operation|business hours|store hours"
    r"|holiday hours|office closed|no school|press release|announces?\b|announcement"
    r"|deadline|applications? (?:due|open)|registration (?:opens|closes))",
    re.IGNORECASE)
_ZIP = re.compile(r"\b(\d{5})(?:-\d{4})?\b")
_MIN_QUOTE = 8


def _place_names(ctx: JudgeContext) -> list[str]:
    return [n for n in (normalize_name(x) for x in (ctx.town, *ctx.aliases)) if n]


def _mentions(text: str, names: Iterable[str]) -> Optional[str]:
    t = f" {normalize_name(text)} "
    return next((n for n in names if f" {n} " in t), None)


def _has_time(item: dict) -> bool:
    from engine.event_extract import has_clock_time
    return has_clock_time(item.get("date")) or bool(item.get("end_at_utc"))


def _deterministic(item: dict, ctx: JudgeContext) -> tuple[Optional[Decision], dict]:
    """(final decision, or None + the facets it settled: is_event / in_town)."""
    now = ctx.now or datetime.now(timezone.utc)
    start = parse_instant(item.get("date_utc"))
    title = item.get("title") or ""
    if not start:
        return Decision("drop", "no-start-time", {"date": item.get("date")}), {}
    if start <= now:
        return Decision("drop", "past", {"start_at": item.get("date_utc")}), {}
    if start > now + timedelta(days=ctx.horizon_days):
        return Decision("drop", "beyond-horizon",
                        {"start_at": item.get("date_utc"), "horizon_days": ctx.horizon_days}), {}
    m = _NON_EVENT.search(title)
    if m:
        return Decision("drop", "not-an-event", {"title": title, "matched": m.group(0)}), {}
    if ctx.series_blocked:
        blocked = ctx.series_blocked(item)
        if blocked:
            return Decision("drop", "series-rejected-streak", {"series_slug": blocked}), {}
    if ctx.series_duplicate:
        live = ctx.series_duplicate(item)
        if live:
            return Decision("drop", "series-duplicate",
                            {"live_slug": live, "note": "its Series already has an "
                                                        "occurrence on this date"}), {}
    facets: dict = {}
    venue, location = item.get("venue") or "", item.get("location") or ""
    if _has_time(item) and (venue or location):
        facets["is_event"] = True
    if venue and normalize_name(venue) in ctx.known_venues:
        facets["in_town"], facets["town_evidence"] = True, f"known town venue {venue!r}"
    else:
        hit = _mentions(f"{venue} {location}", _place_names(ctx))
        if hit:
            facets["in_town"], facets["town_evidence"] = True, f"location names {hit!r}"
        elif ctx.zip_codes:
            zips = _ZIP.findall(location)
            if zips:
                facets["in_town"] = any(z in ctx.zip_codes for z in zips)
                facets["town_evidence"] = f"ZIP {zips[0]}"
    if facets.get("in_town") is False:
        return Decision("drop", "out-of-town", {"evidence": facets["town_evidence"]}), facets
    if facets.get("is_event") and facets.get("in_town"):
        return Decision("approve", "checks-passed",
                        {"town": facets["town_evidence"], "has_time": True}), facets
    # The moderator keeps publishing this Series: a well-established event,
    # approved on the checks above without the AI pass.
    established = ctx.series_established(item) if ctx.series_established else None
    if established:
        return Decision("approve", "series-established",
                        {"series_slug": established[0], "publish_streak": established[1]}), facets
    return None, facets


def _judge_prompt(items: list[tuple[int, dict]], page: str, ctx: JudgeContext) -> str:
    listing = "\n".join(
        f'{i}. "{it.get("title")}" — {it.get("date") or it.get("date_utc")} — '
        f'venue: {it.get("venue") or "?"} — location: {it.get("location") or "?"}'
        for i, it in items)
    aliases = ", ".join(ctx.aliases) or "(none)"
    return f"""You are checking listings extracted from a web page for the town of "{ctx.town}" (also known as: {aliases}).

For each numbered item, decide from the page text below ONLY — never from outside knowledge:
  "is_event": true if the page presents it as something people attend at a stated date and time; false if it is news, an announcement, opening hours, a closure or a deadline; null if the page does not say
  "event_quote": words copied exactly from the page about this item that show what it is, or null
  "in_town": true if the page places it in {ctx.town} or one of its aliases; false if the page places it somewhere else; null if the page does not say
  "town_quote": words copied exactly from the page that state where it takes place, or null

Return ONLY a JSON array of objects with keys "i", "is_event", "event_quote", "in_town", "town_quote" — no prose, no markdown, no code fence.

Items:
{listing}

Page text follows:
---
{page[:12000]}
---
"""


def _on_page(quote, page_norm: str) -> Optional[str]:
    from engine.page_extract import find_quote
    found = find_quote(quote, page_norm, _MIN_QUOTE)
    return found.text if found else None


def _ai_verdict(answer: dict, facets: dict, page_norm: str, ctx: JudgeContext) -> Decision:
    is_event, in_town = facets.get("is_event"), facets.get("in_town")
    evidence: dict = {}
    if is_event is None and isinstance(answer.get("is_event"), bool):
        q = _on_page(answer.get("event_quote"), page_norm)
        if q:
            is_event, evidence["event_quote"] = answer["is_event"], q
    if in_town is None and isinstance(answer.get("in_town"), bool):
        q = _on_page(answer.get("town_quote"), page_norm)
        names = _place_names(ctx)
        if q and answer["in_town"] == bool(_mentions(q, names)):
            in_town, evidence["town_quote"] = answer["in_town"], q
    if facets.get("town_evidence"):
        evidence["town"] = facets["town_evidence"]
    if is_event is False:
        return Decision("drop", "not-an-event", evidence)
    if in_town is False:
        return Decision("drop", "out-of-town", evidence)
    if is_event and in_town:
        return Decision("approve", "ai-verified", evidence)
    return Decision("drop", "insufficient-evidence",
                    {**evidence, "unsettled": [k for k, v in (("is_event", is_event),
                                                              ("in_town", in_town)) if v is None]})


def judge_items(items: list[dict], page: Optional[str], ctx: JudgeContext,
                llm: Optional[Callable[..., tuple[bool, str, str]]] = None
                ) -> list[Optional[Decision]]:
    """One decision per item: approve, or drop with a reason. Deterministic
    checks first; items they can't settle that are on `page` (item key
    `on_page`, default True when a page is given) go to one quote-checked
    LLM call — `llm(prompt) → (ok, stdout, err)`, as event_extract.codex_exec.
    None = deferred (the LLM call failed): leave the item for the next run."""
    from engine.event_extract import parse_json_array
    from engine.page_extract import normalize
    out: list[Optional[Decision]] = [None] * len(items)
    ask: list[tuple[int, dict]] = []
    facets_of: dict[int, dict] = {}
    for i, it in enumerate(items):
        d, facets = _deterministic(it, ctx)
        if d:
            out[i] = d
        elif page and it.get("on_page", True) and llm:
            ask.append((i, it))
            facets_of[i] = facets
        else:
            out[i] = Decision("drop", "insufficient-evidence",
                              {**({"town": facets["town_evidence"]} if facets.get("town_evidence")
                                  else {}),
                               "note": "not on this run's page" if page else "no page to check"})
    if not ask:
        return out
    ok, reply, err = llm(_judge_prompt([(n + 1, it) for n, (_, it) in enumerate(ask)], page, ctx))
    if not ok:
        return out   # deferred
    answers = {}
    for a in parse_json_array(reply):
        if isinstance(a, dict) and isinstance(a.get("i"), int):
            answers[a["i"]] = a
    page_norm = normalize(page)
    for n, (i, _) in enumerate(ask):
        out[i] = _ai_verdict(answers.get(n + 1) or {}, facets_of[i], page_norm, ctx)
    return out


def event_item(row: dict, on_page: bool = True) -> dict:
    """A local events row as a judgement item."""
    return {"event_uid": row.get("event_uid"), "title": row.get("title") or row.get("name"),
            "date": row.get("date"), "date_utc": row.get("date_utc"),
            "end_at_utc": row.get("end_at_utc"),
            "venue": row.get("venue_name") or row.get("venue_raw"),
            "location": row.get("location") or row.get("location_raw"),
            "source_url": row.get("source_url"), "on_page": on_page}


# --------------------------------------------------------------------------
# Stuck events
# --------------------------------------------------------------------------

# States from which an event can still ship with what it has.
_SHIPPABLE_FROM = ("approved", "detail-read", "venue-derived", "hosts-derived",
                   "people-derived")


def resolve_stuck(event: dict, s: Settings, now: Optional[datetime] = None) -> Decision:
    """What to do with a `stuck-<state>` event: `wait` (backoff not over),
    `retry` (re-enter the queue), `ship-as-is` (skip to ship with the fields
    it has — title and a future start are required) or `drop`."""
    if not s.autonomous:
        return await_operator()
    now = now or datetime.now(timezone.utc)
    state = (event.get("state") or "").removeprefix("stuck-")
    auto = parse_extra_json(event.get("extra_json")).get("autonomy") or {}
    retries = int(auto.get("retries") or 0)
    # A failed ship is TownLoops not answering, not the event: it keeps
    # retrying (backoff capped at 16h) until the event has started.
    start = parse_instant(event.get("date_utc"))
    keep_trying = state == "entities-enriched" and start is not None
    if retries < s.stuck_retries or keep_trying:
        if keep_trying and start <= now:
            return Decision("drop", "past", {"state": state, "retries": retries})
        since = parse_instant(event.get("state_updated_at")) or now
        backoff = timedelta(hours=2 ** min(retries, 4))
        if now - since < backoff:
            return Decision("wait", "backoff", {"retries": retries,
                                                "retry_after": (since + backoff).isoformat()})
        return Decision("retry", "backoff-elapsed", {"retries": retries + 1, "state": state})
    start = parse_instant(event.get("date_utc"))
    if not (event.get("title") or event.get("name")) or not start or start <= now:
        return Decision("drop", "missing-required-fields" if not start or not (
            event.get("title") or event.get("name")) else "past",
            {"state": state, "retries": retries})
    if state in _SHIPPABLE_FROM:
        return Decision("ship-as-is", "retries-exhausted", {"state": state, "retries": retries})
    return Decision("drop", "ship-failed", {"state": state, "retries": retries})


# --------------------------------------------------------------------------
# Cancellations
# --------------------------------------------------------------------------

_CANCELLED = re.compile(r"\b(?:cancell?ed|postponed|called off)\b", re.IGNORECASE)
# Policy wording, not a statement about this event.
_HYPOTHETICAL = re.compile(r"\b(?:if|in case|in the event|should|may be|might|refunds?|policy"
                           r"|not|no longer|unless)\b", re.IGNORECASE)
# A statement about the page's own event: a status line, or "this event has been …".
_STATUS_LINE = re.compile(
    r"^\W*(?:cancell?ed|postponed)\b|\b(?:this|the|today'?s|tonight'?s)\s+(?:event|show|concert"
    r"|performance|class|game|race|festival|meeting|program)\s+(?:has been|is|was|will be)\s+"
    r"(?:cancell?ed|postponed|called off)", re.IGNORECASE)


def cancellation_evidence(page: Optional[str], title: Optional[str],
                          own_page: bool = True) -> Decision:
    """Whether an event's own page explicitly says it is cancelled or
    postponed: a line of the page that says so (and, when it carries the
    title, names this event). A vanished listing alone never is. A page
    that isn't the event's own (`own_page=False`: a seed's listing of many
    events) counts only a line that names the event."""
    if not page:
        return Decision("hold", "no-page", {})
    tokens = {t for t in normalize_name(title or "").split() if len(t) > 3}
    for line in re.split(r"[\n.!]", page):
        if not _CANCELLED.search(line) or _HYPOTHETICAL.search(line):
            continue
        words = set(normalize_name(line).split())
        if (own_page and _STATUS_LINE.search(line)) or (tokens and tokens <= words):
            return Decision("submit", "page-states-cancelled", {"quote": line.strip()[:300]})
    return Decision("hold", "no-explicit-statement", {})


# --------------------------------------------------------------------------
# Series attribution
# --------------------------------------------------------------------------

def series_blocked(conn: sqlite3.Connection, series_slug: Optional[str], s: Settings) -> bool:
    """True when the moderator rejected this Series' last
    `series_reject_streak` decided submissions in a row — it is skipped until
    one is published (which ends the streak)."""
    if not series_slug or s.series_reject_streak <= 0:
        return False
    rows = [r[0] for r in conn.execute(
        "SELECT status FROM submissions WHERE resource = 'events' AND series_slug = ? "
        "AND status IN ('published', 'rejected', 'merged') ORDER BY decided_at DESC, id DESC "
        "LIMIT ?", (series_slug, s.series_reject_streak)).fetchall()]
    return len(rows) >= s.series_reject_streak and all(r == "rejected" for r in rows)


def series_established(conn: sqlite3.Connection, series_slug: Optional[str],
                       s: Settings) -> Optional[int]:
    """The publish streak when the moderator published (or merged) this
    Series' latest `series_publish_streak` or more decided submissions in a
    row — a well-established event, approved without the AI pass — else
    None. One rejection ends the streak."""
    n = s.series_publish_streak
    if not series_slug or n <= 0:
        return None
    streak = 0
    for (status,) in conn.execute(
            "SELECT status FROM submissions WHERE resource = 'events' AND series_slug = ? "
            "AND status IN ('published', 'rejected', 'merged') ORDER BY decided_at DESC, id DESC",
            (series_slug,)):
        if status == "rejected":
            break
        streak += 1
    return streak if streak >= n else None


# --------------------------------------------------------------------------
# Discovered organisations
# --------------------------------------------------------------------------

# A candidate held for one of these is re-verified and judged again
# (business_promote.auto_approve).
FOREIGN_VERDICT = "foreign-verdict"
STALE_VERDICT = "stale-verdict"
REVERIFY = (FOREIGN_VERDICT, STALE_VERDICT)


def judge_candidate(cand: dict, s: Settings, now: Optional[datetime] = None) -> Decision:
    """A verified business / community group / non-profit candidate: submit
    it (it lands pending for the moderator) once TownLoops moderates
    entities — on a verdict of the current verifier
    (engine/discovery.verify_candidate) no older than
    `verdict_max_age_days`. That verifier checked one page: that it names
    the town, shows no closure, is customer- or member-facing, isn't a
    configured seed directory, a listing host, a roundup or an article,
    and passes the own-site heuristic
    (engine/own_site — a host or platform profile carrying the name, not
    proof of ownership). Dedup already matched it against TownLoops
    (published, pending and rejected) and against remembered rejections.

    A verdict copied in from elsewhere (the CMO import's `imported_from`)
    is `foreign-verdict`; one older than the max age, or with no verify
    time, is `stale-verdict` — both held until re-verified."""
    if not s.autonomous:
        return await_operator()
    verify = cand.get("verify") or {}
    if cand.get("status") != "verified" or not verify.get("verified"):
        return Decision("hold", "not-verified", {"status": cand.get("status")})
    if not s.entity_moderation:
        return Decision("hold", "entity-moderation-off",
                        {"setting": "autonomy.entity_moderation"})
    if verify.get("imported_from"):
        return Decision("hold", FOREIGN_VERDICT,
                        {"imported_from": verify["imported_from"],
                         "source_signal": cand.get("source_signal")})
    at = parse_instant(verify.get("fetched_at"))
    now = now or datetime.now(timezone.utc)
    if at is None or now - at > timedelta(days=s.verdict_max_age_days):
        return Decision("hold", STALE_VERDICT,
                        {"verified_at": verify.get("fetched_at"),
                         "max_age_days": s.verdict_max_age_days})
    return Decision("submit", "verified-on-own-site",
                    {"url": cand.get("canonical_url"), "kind": cand.get("kind"),
                     **{k: verify[k] for k in ("address", "phone", "reasons") if verify.get(k)}})


# --------------------------------------------------------------------------
# Seeds: trust, role, retirement
# --------------------------------------------------------------------------

_HEALTH_FACTOR = {None: 1.0, "drop": 0.6, "zero": 0.3}


def seed_outcomes(conn: sqlite3.Connection, seed_name: str, since: str = "") -> dict:
    counts = dict(conn.execute(
        "SELECT status, COUNT(*) FROM submissions WHERE resource = 'events' AND seed = ? "
        "AND published_unreviewed = 0 AND COALESCE(decided_at, submitted_at) > ? "
        "GROUP BY status", (seed_name, since)).fetchall())
    return {"published": counts.get("published", 0) + counts.get("merged", 0),
            "rejected": counts.get("rejected", 0), "pending": counts.get("pending", 0)}


def dedup_record(conn: sqlite3.Connection, seed_name: str) -> dict:
    """How this seed's copies fared in cross-seed dedup: `lost` = its copy
    was retired for another seed's, `of` = its copies that met another."""
    rows = _rows(conn, "SELECT e.event_uid, e.status, e.canonical_event_uid FROM events e "
                       "WHERE e.source = ? AND e.canonical_event_uid IN (SELECT canonical_event_uid "
                       "  FROM events GROUP BY canonical_event_uid HAVING COUNT(DISTINCT source) > 1)",
                 (seed_ref(seed_name),))
    return {"lost": sum(r["status"] == "duplicate" for r in rows), "of": len(rows)}


def seed_trust(conn: sqlite3.Connection, seed: dict) -> float:
    """0..1: how far this seed's output is accepted — the moderator's
    published share of its submissions since its last status change
    (Laplace-smoothed, so a new or restored seed starts at 0.5), scaled down
    by a standing health alert and by how often its copy of an event loses
    dedup to another source's (not for a syndicator: losing to the
    organiser's copy is its role). Trial approval, demotion to syndicator
    and retirement are decided on it (judge_trial, classify_role,
    judge_seed)."""
    o = seed_outcomes(conn, seed["name"], seed.get("status_changed_at") or "")
    acceptance = (o["published"] + 1) / (o["published"] + o["rejected"] + 2)
    health = _HEALTH_FACTOR.get(seed.get("health_status"), 1.0)
    d = dedup_record(conn, seed["name"])
    dedup = 1 - 0.5 * (d["lost"] / d["of"]) \
        if d["of"] and seed.get("source_role") != "syndicator" else 1.0
    return round(acceptance * health * dedup, 3)


def _trust_evidence(conn: sqlite3.Connection, seed: dict) -> tuple[float, int, dict]:
    """(trust, decided outcomes, evidence) — the record a seed is judged on."""
    o = seed_outcomes(conn, seed["name"], seed.get("status_changed_at") or "")
    t = seed_trust(conn, seed)
    return t, o["published"] + o["rejected"], {**o, "trust": t}


def refresh_trust(conn: sqlite3.Connection, seed_names: Optional[Iterable[str]] = None) -> dict:
    """Recompute and store trust for these seeds (default: every seed). A
    proposed or rejected seed keeps the score its trial gave it."""
    seeds = _rows(conn, "SELECT * FROM event_seeds WHERE status NOT IN ('proposed', 'rejected')")
    wanted = set(seed_names) if seed_names is not None else None
    out = {}
    for seed in seeds:
        if wanted is not None and seed["name"] not in wanted:
            continue
        t = seed_trust(conn, seed)
        conn.execute("UPDATE event_seeds SET trust_score = ? WHERE seed_id = ?", (t, seed["seed_id"]))
        out[seed["name"]] = t
    return out


def classify_role(conn: sqlite3.Connection, seed: dict, s: Settings,
                  min_events: int = 5) -> Decision:
    """Primary or syndicator. A seed whose trust has fallen below
    `seed_trust_syndicator` (once `seed_min_outcomes` are decided) is
    demoted to syndicator, so another source's copy of an event wins.
    Otherwise from the seed's own events: a calendar whose events mostly
    link to other sites' pages republishes other organisers' events; one
    whose events link to its own site is the organiser's. Too few events
    with links → keep."""
    from urllib.parse import urlparse
    if not s.autonomous:
        return await_operator()
    trust, decided, record = _trust_evidence(conn, seed)
    if decided >= s.seed_min_outcomes and trust < s.seed_trust_syndicator:
        return Decision("syndicator", "low-trust",
                        {**record, "threshold": s.seed_trust_syndicator})

    def host(url: Optional[str]) -> str:
        h = (urlparse(url or "").hostname or "").lower()
        return h[4:] if h.startswith("www.") else h

    own = host(seed.get("url"))
    hosts = [host(r[0]) for r in conn.execute(
        "SELECT source_url FROM events WHERE source = ? AND COALESCE(source_url, '') != ''",
        (seed_ref(seed['name']),)).fetchall()]
    hosts = [h for h in hosts if h]
    if len(hosts) < min_events or not own:
        return Decision("keep", "too-few-events", {"with_links": len(hosts), "trust": trust})
    off = sum(1 for h in hosts if h != own and not h.endswith("." + own))
    share = off / len(hosts)
    evidence = {"events_with_links": len(hosts), "off_site_share": round(share, 2),
                "trust": trust}
    if share >= 0.6:
        return Decision("syndicator", "links-off-site", evidence)
    if share <= 0.4:
        return Decision("primary", "links-on-site", evidence)
    return Decision("keep", "mixed-links", evidence)


def _recent_runs(conn: sqlite3.Connection, seed: dict, n: int, since: str = "") -> list[dict]:
    return _rows(conn, "SELECT ok, fetched, error FROM source_runs WHERE source = ? "
                       "AND started_at > ? ORDER BY started_at DESC, id DESC LIMIT ?",
                 (seed_ref(seed['name']), since, n))


def judge_seed(conn: sqlite3.Connection, seed: dict, s: Settings) -> Decision:
    """An approved seed: `keep`, `retire` (its trust fell below
    `seed_trust_retire` once `seed_min_outcomes` were decided), or
    `check-moved` (its last `seed_dead_runs` runs failed or listed nothing,
    or it is low-trust under a standing health alert — look for a moved
    calendar before retiring it)."""
    if not s.autonomous:
        return await_operator()
    since = seed.get("status_changed_at") or ""   # a restored seed starts a fresh record
    trust, decided, record = _trust_evidence(conn, seed)
    if decided >= s.seed_min_outcomes and trust < s.seed_trust_retire:
        evidence = {**record, "threshold": s.seed_trust_retire}
        if seed.get("health_status"):
            # Part of the low score is the source going quiet: what it lists
            # now may not be its calendar any more.
            return Decision("check-moved", "low-trust",
                            {**evidence, "health": seed["health_status"]})
        return Decision("retire", "low-trust", evidence)
    runs = _recent_runs(conn, seed, max(s.seed_dead_runs, s.seed_min_runs), since)
    total = conn.execute("SELECT COUNT(*) FROM source_runs WHERE source = ? AND started_at > ?",
                         (seed_ref(seed['name']), since)).fetchone()[0]
    recent = runs[:s.seed_dead_runs]
    if (total >= s.seed_min_runs and len(recent) >= s.seed_dead_runs
            and all(not r["ok"] or not r["fetched"] for r in recent)):
        return Decision("check-moved", "dead-source",
                        {"runs": [{"ok": bool(r["ok"]), "fetched": r["fetched"]} for r in recent],
                         "health": seed.get("health_status"), "trust": trust})
    if seed.get("health_status") and seed.get("health_flagged_at"):
        since = conn.execute("SELECT COUNT(*) FROM source_runs WHERE source = ? AND started_at >= ?",
                             (seed_ref(seed['name']), seed["health_flagged_at"])).fetchone()[0]
        if since >= s.seed_dead_runs:
            return Decision("check-moved", "health-alert-persists",
                            {"health": seed["health_status"], "note": seed.get("health_note"),
                             "runs_since_alert": since, "trust": trust})
        return Decision("keep", "health-alert-watching", {"health": seed["health_status"],
                                                          "runs_since_alert": since,
                                                          "trust": trust})
    return Decision("keep", "healthy", record)


def judge_trial(items: list[dict], decisions_: list[Optional[Decision]],
                failure: Optional[str], s: Settings) -> Decision:
    """A proposed seed's dry run: approve it when the page isn't an error /
    challenge / empty page and its trial score — the Laplace-smoothed share
    of the listed events that passed the event judgement (upcoming, in town,
    a real event), the trust it starts with — reaches `seed_trust_approve`
    with at least one passing. Deferred items (the AI call failed) aren't
    counted; while any are, a seed short of the bar is deferred, not
    rejected."""
    if failure:
        return Decision("reject", f"trial-{failure}", {})
    passed = [it["title"] for it, d in zip(items, decisions_) if d and d.verdict == "approve"]
    judged = sum(1 for d in decisions_ if d)
    trust = round((len(passed) + 1) / (judged + 2), 3)
    evidence = {"events": len(items), "passed": passed[:5], "trust": trust,
                "threshold": s.seed_trust_approve}
    if passed and trust >= s.seed_trust_approve:
        return Decision("approve", "trial-yielded-events", evidence)
    if any(d is None for d in decisions_):
        return Decision("defer", "ai-unavailable", evidence)
    evidence["reasons"] = sorted({d.reason for d in decisions_ if d and d.verdict != "approve"})
    return Decision("reject", "trial-low-yield" if passed else "trial-no-usable-events", evidence)


def judge_adapter(trial: dict, s: Settings) -> Decision:
    """Review's call on a generic seed whose probed feed an adapter reads,
    from that adapter's dry run (`trial`: the evidence engine/seeds'
    `adapter_trial` gathered — `error` when the read failed, else the usable
    `trial_events`). Failed or empty keeps the seed generic; passed assigns
    the adapter — in supervised mode the operator does (`set-adapter`)."""
    if trial.get("error"):
        return Decision("keep-generic", "adapter-trial-failed", trial)
    if not trial.get("trial_events"):
        return Decision("keep-generic", "adapter-trial-empty", trial)
    if not s.autonomous:
        return await_operator("adapter passed a dry run — set-adapter on the operator's say-so",
                              trial)
    return Decision("assign", "feed-match", trial)


# --------------------------------------------------------------------------
# Provenance (#45)
# --------------------------------------------------------------------------

def judge_provenance(evidence: dict, s: Settings) -> Decision:
    """An event its source can't vouch for (engine/event_provenance's
    `evidence["disagreed"]`: what its listing card or detail page
    contradicts) is held for review — never shipped, never dropped. It waits
    pending for the operator in both modes: the autonomous judge skips it and
    the nightly digest lists it until the operator approves (accepts) or
    declines it. Nothing disagreed: ship."""
    if not evidence.get("disagreed"):
        return Decision("ship", "provenance-agrees", evidence)
    if not s.autonomous:
        return await_operator("source mismatch", evidence)
    return Decision("hold", "provenance-mismatch", evidence)


# --------------------------------------------------------------------------

def _town() -> str:
    import os
    return os.environ.get("TOWNLOOPS_TOWN_SLUG", "?")


def _telegram(text: str) -> None:
    try:
        from engine.orchestrator import _telegram_notify
    except Exception:  # noqa: BLE001 — alerts are best-effort
        return
    _telegram_notify(text)
