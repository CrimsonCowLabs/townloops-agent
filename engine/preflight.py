"""Preflight — one read-only verdict on whether this town will work.

`run()` returns a list of `Finding`s; `town-init preflight` prints them and
exits non-zero on any error, and `town-init setup` runs it last. Each finding
is a severity (`error`: the agent cannot work; `warning`: it works with a
piece missing; `info`: something preflight cannot check), a stable code, the
file + key it concerns, and the consequence in one line.

Read-only and cheap: no writes (the state DB is opened `mode=ro`, never
created or migrated), no probe-by-create, no pagination — `healthz`, one
`list(kind, limit=1)`, `verify_town()`'s category read, and one more category
read only when a profile slug is missing from the snapshot. TownLoops reports
neither a Town's own fields nor a key's Role, so `name`/`region` are listed as
unverifiable and a publish-capable key shows up only as the open circuit
`autonomy.GatedClient` records after the first ship.

Network and clients are injectable (`client`, `http_get`, `listmonk`) so the
tests drive it in-process with `tests/fake_townloops.FakeAPI`.
"""

from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Optional

from engine import autonomy, db, firecrawl_env, policy
from engine.derive_helpers import load_town_profile

ERROR, WARNING, INFO = "error", "warning", "info"
PROFILE = "town-profile.yaml"
ENV = ".env"
# The compose profile bin/townloops-agent-add's autonomous override parks the
# dashboard behind — the same marker its `--rerender` check greps for.
DASHBOARD_PROFILE = "supervised-dashboard"
FALLBACK_TZ = "America/Los_Angeles"   # engine/ingest_core.DEFAULT_TZ's default
PLACEHOLDER = "PLACEHOLDER"


@dataclass(frozen=True)
class Finding:
    severity: str
    code: str
    where: str      # "<file>: <key>"
    message: str    # the consequence, one line
    detail: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


def has_errors(findings: list[Finding]) -> bool:
    return any(f.severity == ERROR for f in findings)


def run(client=None, *, profile: Optional[dict] = None,
        http_get: Optional[Callable] = None,
        listmonk: Optional[Callable] = None) -> list[Finding]:
    """Every check, in the order an operator would fix them."""
    profile = load_town_profile() if profile is None else profile
    out: list[Finding] = []
    client = _credential(client, out)
    _profile(profile, client, out)
    s = autonomy.settings(profile or {})
    _operating_mode(s, out)
    _circuit(out)
    _firecrawl(http_get or _http_get, out)
    _codex(out)
    _timezone(out)
    _listmonk(listmonk, out)
    if s.autonomous and not (_env("TELEGRAM_BOT_TOKEN") and _env("TELEGRAM_CHAT_ID")):
        out.append(Finding(WARNING, "telegram-missing", f"{ENV}: TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID",
                           "no push alerts — an open circuit or a dead source is seen only by "
                           "running preflight"))
    return out


# --------------------------------------------------------------------------
# Checks
# --------------------------------------------------------------------------

def _credential(client, out: list[Finding]):
    """The client when the key authenticates and is scoped to this town, else None."""
    from engine.townloops_api import Client
    where = f"{ENV}: TOWNLOOPS_API_URL, TOWNLOOPS_API_KEY, TOWNLOOPS_TOWN_SLUG"
    if client is None:
        try:
            client = Client()
        except Exception as e:
            out.append(Finding(ERROR, "credential-missing", where,
                               f"can't reach TownLoops at all — not set: {e}"))
            return None
    try:
        client.healthz()
    except Exception as e:
        out.append(Finding(ERROR, "api-unreachable", f"{ENV}: TOWNLOOPS_API_URL",
                           f"TownLoops /healthz failed, nothing can ship: {e}"))
        return None
    try:
        client.list("venues", limit=1)
    except Exception as e:
        out.append(Finding(ERROR, "credential-rejected", f"{ENV}: TOWNLOOPS_API_KEY",
                           f"the key can't read town {client.town_slug!r}, nothing can ship: {e}",
                           {"status": getattr(e, "status", None)}))
        return None
    try:
        client.verify_town(str(policy.categories_snapshot_path()))
    except Exception as e:
        out.append(Finding(ERROR, "town-scope", where,
                           f"the cross-town guard refuses every write: {e}"))
        return None
    return client


def _profile(profile: dict, client, out: list[Finding]) -> None:
    if not profile:
        out.append(Finding(ERROR, "profile-missing", PROFILE,
                           "no town profile (TOWN_PROFILE_PATH / $HERMES_HOME/town-profile.yaml) — "
                           "discovery has no town name, terms or seeds"))
        return
    for key in ("name", "region"):
        value = str(profile.get(key) or "")
        if not value.strip() or PLACEHOLDER in value:
            out.append(Finding(ERROR if key == "name" else WARNING, "profile-placeholder",
                               f"{PROFILE}: {key}",
                               f"{key} is unset or still the template placeholder — "
                               + ("every search query and locality check uses it"
                                  if key == "name" else "searches lose the region qualifier")))
    out.append(Finding(INFO, "profile-unverifiable", f"{PROFILE}: name, region",
                       "TownLoops has no Town read, so these are on your honour — not checked"))
    _categories(profile, client, out)


def _categories(profile: dict, client, out: list[Finding]) -> None:
    groups = (((profile.get("discovery") or {}).get("community") or {}).get("query_groups")) or []
    wanted = [(i, g.get("category_slug")) for i, g in enumerate(groups)
              if isinstance(g, dict) and g.get("category_slug")]
    if not wanted:
        return
    snap = policy.categories_snapshot_path()
    known = policy.load_category_slugs(snap)
    if not known:
        out.append(Finding(ERROR, "categories-snapshot-missing", str(snap),
                           "no categories snapshot — run `town-init setup`; until then every "
                           "community query group enqueues nothing"))
        return
    unknown = [(i, slug) for i, slug in wanted if not policy.has_category(slug, snap)]
    live: set[str] = set()
    if unknown and client is not None:
        try:
            live = {c.get("slug") for c in client.list("categories", limit=200) or []
                    if isinstance(c, dict)}
        except Exception:
            live = set()
    for i, slug in unknown:
        where = f"{PROFILE}: discovery.community.query_groups[{i}].category_slug"
        if slug in live:
            out.append(Finding(ERROR, "category-snapshot-stale", where,
                               f"{slug!r} exists on TownLoops but not in the snapshot — re-run "
                               "`town-init setup` (the profile is right)", {"slug": slug}))
        else:
            out.append(Finding(ERROR, "category-unknown", where,
                               f"{slug!r} is not a category of this town — that query group "
                               f"enqueues nothing. Known: {', '.join(sorted(known))}",
                               {"slug": slug, "known": sorted(known)}))


def _operating_mode(s: autonomy.Settings, out: list[Finding]) -> None:
    if not s.autonomous:
        if not _env("DASHBOARD_PASSWORD"):
            out.append(Finding(ERROR, "dashboard-password-missing", f"{ENV}: DASHBOARD_PASSWORD",
                               "supervised mode reviews in the dashboard, and every login fails "
                               "without a password hash"))
        return
    town_dir = _town_dir()
    if town_dir is None:
        out.append(Finding(WARNING, "dashboard-unverified", "docker-compose.override.yml",
                           "can't see the town folder from here (set TOWNLOOPS_TOWN_DIR or run "
                           "on the host) — the dashboard being off is unchecked"))
        return
    override = town_dir / "docker-compose.override.yml"
    try:
        text = override.read_text()
    except OSError:
        text = ""
    if DASHBOARD_PROFILE not in text:
        out.append(Finding(ERROR, "dashboard-enabled-autonomous", f"{override}: services.dashboard.profiles",
                           f"autonomous mode but the dashboard isn't parked behind the "
                           f"{DASHBOARD_PROFILE!r} profile — add "
                           f"`services: {{dashboard: {{profiles: [\"{DASHBOARD_PROFILE}\"]}}}}`"))


def _circuit(out: list[Finding]) -> None:
    path = Path(db._db_path_from_env())
    if not path.exists():
        out.append(Finding(WARNING, "state-db-missing", str(path),
                           "no state DB yet (run `town-init setup`) — the autonomy circuit is unchecked"))
        return
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            c = autonomy.circuit(conn)
        finally:
            conn.close()
    except sqlite3.Error as e:
        out.append(Finding(WARNING, "state-db-missing", str(path),
                           f"state DB unreadable or not migrated ({e}) — the autonomy circuit is unchecked"))
        return
    if c:
        out.append(Finding(ERROR, "circuit-open", f"{path}: autonomy_state.circuit",
                           f"all submissions stopped since {c.get('at')}: {c.get('reason')} — "
                           f"{json.dumps(c.get('evidence'), default=str)}. Fix it, then "
                           "`autonomy resume`",
                           {"reason": c.get("reason"), "evidence": c.get("evidence"),
                            "at": c.get("at")}))


def _firecrawl(http_get: Callable, out: list[Finding]) -> None:
    where = f"{ENV}: FIRECRAWL_API_URL (or TOWNLOOPS_FIRECRAWL_URL)"
    try:
        base = firecrawl_env.base_url()
    except firecrawl_env.FirecrawlNotConfigured as e:
        out.append(Finding(ERROR, "firecrawl-not-configured", where,
                           f"no event page can be read: {e}"))
        return
    try:
        http_get(base + "/", timeout=5)
    except Exception as e:
        out.append(Finding(ERROR, "firecrawl-unreachable", where,
                           f"local Firecrawl at {base} didn't answer — no event page can be "
                           f"read, and the town looks empty: {e}"))


def _codex(out: list[Finding]) -> None:
    home = Path(_env("CODEX_HOME") or Path.home() / ".codex")
    if not (home / "auth.json").is_file():
        out.append(Finding(ERROR, "codex-login-missing", f"{home}/auth.json (CODEX_HOME)",
                           "no Codex CLI login — every LLM step (event finding, judgement) "
                           "no-ops; run `codex login` on the host"))


def _timezone(out: list[Finding]) -> None:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    name = _env("AGENT_TZ")
    if not name:
        out.append(Finding(WARNING, "tz-fallback", f"{ENV}: AGENT_TZ",
                           f"unset, so every date is computed in {FALLBACK_TZ} — set it if "
                           "the town isn't on Pacific time"))
        return
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        out.append(Finding(ERROR, "tz-invalid", f"{ENV}: AGENT_TZ",
                           f"{name!r} is not a time zone — every event date would be wrong"))


def _listmonk(factory: Optional[Callable], out: list[Finding]) -> None:
    where = f"{ENV}: LISTMONK_URL, LISTMONK_USER, LISTMONK_TOKEN"
    if not all(_env(k) for k in ("LISTMONK_URL", "LISTMONK_USER", "LISTMONK_TOKEN")):
        out.append(Finding(WARNING, "listmonk-unconfigured", where,
                           "the weekly newsletter is off (ingest is unaffected)"))
        return
    try:
        if factory is None:
            from engine.listmonk_api import Client as factory
        factory().lists()
    except Exception as e:
        out.append(Finding(WARNING, "listmonk-unreachable", where,
                           f"configured but unreachable — Sunday's newsletter draft will fail: {e}"))


# --------------------------------------------------------------------------
# Helpers + output
# --------------------------------------------------------------------------

def _env(key: str) -> str:
    return (os.environ.get(key) or "").strip()


def _town_dir() -> Optional[Path]:
    """The scaffolded town folder holding docker-compose.override.yml:
    $TOWNLOOPS_TOWN_DIR, else $TOWNLOOPS_AGENTS_ROOT (default ~/townloops-agents)/<slug>
    — the layout bin/townloops-agent-add writes. None when not visible here."""
    explicit = _env("TOWNLOOPS_TOWN_DIR")
    if explicit:
        p = Path(explicit)
        return p if p.is_dir() else None
    root = Path(_env("TOWNLOOPS_AGENTS_ROOT") or Path.home() / "townloops-agents")
    p = root / _env("TOWNLOOPS_TOWN_SLUG")
    return p if _env("TOWNLOOPS_TOWN_SLUG") and p.is_dir() else None


def _http_get(url: str, timeout: float):
    import requests
    return requests.get(url, timeout=timeout)


def format_human(findings: list[Finding]) -> str:
    errors = [f for f in findings if f.severity == ERROR]
    warnings = [f for f in findings if f.severity == WARNING]
    infos = [f for f in findings if f.severity == INFO]
    lines = []
    for label, group in (("ERROR", errors), ("WARN", warnings), ("INFO", infos)):
        for f in group:
            lines.append(f"{label:5} {f.code}  [{f.where}]\n      {f.message}")
    verdict = (f"FAIL — {len(errors)} error(s), {len(warnings)} warning(s): the agent cannot work "
               "until the errors are fixed" if errors else
               f"OK — no errors, {len(warnings)} warning(s)")
    return "\n".join(lines + [verdict])
