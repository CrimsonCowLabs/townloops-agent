"""Seed adapters: a deterministic reader per seed, chosen by the seed record.

`event_seeds.adapter` names how event-find reads a seed — NULL/'generic' is
the Firecrawl markdown → LLM path (event-find's own); any other key is an
adapter of `registry()`, configured by `event_seeds.adapter_config` (a JSON
object). No URL or hostname conditionals: a seed is moved onto an adapter by
setting its key (`event-seed-discovery set-adapter`, or the autonomous
`review` when an adapter reads the seed's probed `feed_type`). Each adapter
is an `Adapter`: its reader, the settings its config may hold
(`check_config` refuses any other key or a bad value before anything is
stored) and the feed types it reads (`for_feed`).

`registry()` is the shared `ADAPTERS` plus the town's own: a town agent
writes `$HERMES_HOME/adapters/<name>.py` exposing `ADAPTER`
(`town_adapters`, loaded at lookup and again whenever the file changes). A
shared adapter of the same name wins and the town copy is reported
shadowed — how a promoted adapter takes over; a file that fails to import or
has no valid `ADAPTER` is skipped and named, never breaking another seed.
Every adapter name is resolved through `registry()`.

`read(seed)` returns a `Listing`: events normalized to what the extraction
prompt returns (title, local-wall-clock `date`/`end_at`, venue, location,
description, categories, tags, source_url) — so they go through event-find's
one upsert / cancellation / vanish / source-health / judgement path — plus
`page`, the text those quote-check against. Anything short of the whole
listing raises `AdapterError`, as does a reader that raises anything else or
returns no event list: a partial read would count every unread event as
missing, and a failed read must be a failed run, never a zero.

`run_fixture` / `run_case` read a captured response through an adapter with
fetching stubbed (`FixtureFeed`): `event-seed-discovery test-adapter` in a
town, tests/test_adapter_fixtures.py here.
"""

from __future__ import annotations

import difflib
import html as _html
import importlib.util
import json
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Optional, Union
from urllib.parse import parse_qsl, urljoin, urlsplit

import requests
from dateutil import tz as _tz

from engine import blocked_page
from engine.derive_helpers import is_http_url
from engine.event_extract import town_tz_name
from engine.research import UA
from engine.feed_probe import SHOWARE_LIST_PATH, TRIBE_REST_PATH
from engine.log import get_logger

_log = get_logger("seed-adapters")

GENERIC = "generic"
TIMEOUT_S = 30

GetJson = Callable[[str, dict], dict]


class AdapterError(Exception):
    """The adapter could not read the seed's whole listing."""


@dataclass(frozen=True)
class Listing:
    events: list[dict]
    page: str


def adapter_of(seed: dict) -> str:
    return seed.get("adapter") or GENERIC


def read(seed: dict, get_json: Optional[GetJson] = None) -> Listing:
    key = adapter_of(seed)
    adapter = registry().get(key)
    if adapter is None:
        raise AdapterError(why_unknown(key))
    config = _config(seed)
    try:
        events = adapter.read(seed, config, get_json or fetch_json)
    except AdapterError:
        raise
    except Exception as e:  # noqa: BLE001 — a reader's bug is a failed read, never a crash
        raise AdapterError(f"{type(e).__name__}: {e}") from e
    if not isinstance(events, list) or not all(isinstance(e, dict) for e in events):
        raise AdapterError("the reader did not return a list of event objects")
    return Listing(events, "\n\n".join(_page_entry(e) for e in events))


def _config(seed: dict) -> dict:
    raw = seed.get("adapter_config")
    if not raw:
        return {}
    try:
        cfg = json.loads(raw)
    except (TypeError, ValueError) as e:
        raise AdapterError(f"adapter_config is not JSON: {e}") from None
    if not isinstance(cfg, dict):
        raise AdapterError("adapter_config is not a JSON object")
    return cfg


def fetch_json(url: str, params: dict) -> dict:
    try:
        r = requests.get(url, params=params, timeout=TIMEOUT_S,
                         headers={"Accept": "application/json", "User-Agent": UA})
    except requests.RequestException as e:
        raise AdapterError(f"{type(e).__name__}: {e}") from None
    if r.ok:
        try:
            return r.json()
        except ValueError:
            pass
    # An error status, or a challenge / `no action` answered 200, is named.
    raise AdapterError(blocked_page.reason(r.text, r.status_code) or "response is not JSON")


def _page_entry(e: dict) -> str:
    where = ", ".join(x for x in (e.get("venue"), e.get("location")) if x)
    return "\n".join(x for x in (f"{e.get('title') or ''} — {e.get('date') or ''}", where,
                                 e.get("description"), e.get("source_url")) if x)


_BREAK_RE = re.compile(r"<br\s*/?>|</(?:p|div|li|h\d)>", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")


def html_text(value) -> Optional[str]:
    """HTML → plain text: one line per paragraph, entities decoded."""
    if not isinstance(value, str):
        return None
    text = _html.unescape(_TAG_RE.sub("", _BREAK_RE.sub("\n", value)))
    lines = (" ".join(line.split()) for line in text.splitlines())
    return "\n".join(line for line in lines if line) or None


def local_time(utc_value, local_value) -> Optional[str]:
    """A feed time as the extraction's form: town-local wall clock, no offset."""
    if isinstance(utc_value, str) and utc_value.strip():
        try:
            dt = datetime.fromisoformat(utc_value.strip().replace(" ", "T"))
        except ValueError:
            dt = None
        if dt is not None:
            return (dt.replace(tzinfo=timezone.utc).astimezone(_tz.gettz(town_tz_name()))
                    .replace(tzinfo=None).isoformat(timespec="seconds"))
    if isinstance(local_value, str) and local_value.strip():
        return local_value.strip().replace(" ", "T")
    return None


# ---------------------------------------------------------------------------
# WordPress The Events Calendar REST (Tribe)
# ---------------------------------------------------------------------------

TRIBE_PER_PAGE = 50          # the API caps per_page (50 seen), whatever is asked
TRIBE_MAX_PAGES = 100


def _tribe_endpoint(seed: dict, config: dict) -> str:
    """config `url`, else the feed event-seed-discovery probed
    (engine/feed_probe), else the site's default WordPress REST root."""
    if config.get("url"):
        return config["url"]
    if seed.get("feed_type") == "tribe" and seed.get("feed_url"):
        return seed["feed_url"]
    return urljoin(seed["url"], "/wp-json/" + TRIBE_REST_PATH)


def location(address, city, state, zip_code) -> Optional[str]:
    """"address, city, state zip" from whichever parts the feed gives."""
    zip_part = " ".join(x for x in (state, zip_code) if x)
    location = ", ".join(x for x in (address, city, zip_part) if x)
    return _html.unescape(location) or None


def _tribe_event(row: dict) -> dict:
    """A row without a title or start stays in (as None): event-find counts
    it as malformed rather than it vanishing here."""
    venue = row.get("venue") if isinstance(row.get("venue"), dict) else {}
    start = local_time(row.get("utc_start_date"), row.get("start_date"))
    end = local_time(row.get("utc_end_date"), row.get("end_date"))
    if row.get("all_day") is True and start:   # 00:00–23:59:59 is no published time
        start, end = start[:10], None
    return {
        "title": html_text(row.get("title")),
        "date": start,
        "end_at": end,
        "venue": html_text(venue.get("venue")),
        "location": location(venue.get("address"), venue.get("city"), venue.get("state"),
                              venue.get("zip")),
        "description": html_text(row.get("description")),
        "categories": [c["name"] for c in row.get("categories") or []
                       if isinstance(c, dict) and c.get("name")],
        "tags": [t["name"] for t in row.get("tags") or [] if isinstance(t, dict) and t.get("name")],
        "source_url": row.get("url") or None,
    }


def tribe(seed: dict, config: dict, get_json: GetJson) -> list[dict]:
    """Every page of the feed (`total_pages`, as each answer gives it), from
    today — through `days_ahead` when configured. A recurring event's
    occurrences are separate rows; one repeated across a page boundary is
    kept once (event id + start)."""
    url = _tribe_endpoint(seed, config)
    max_pages = int(config.get("max_pages") or TRIBE_MAX_PAGES)
    today = datetime.now(_tz.gettz(town_tz_name())).date()
    params = {"per_page": TRIBE_PER_PAGE, "start_date": today.isoformat()}
    if config.get("days_ahead"):
        params["end_date"] = (today + timedelta(days=int(config["days_ahead"]))).isoformat()
    events: list[dict] = []
    seen: set = set()
    page, total_pages = 1, 1
    while page <= total_pages:
        try:
            body = get_json(url, {**params, "page": page})
        except AdapterError as e:
            raise AdapterError(f"page {page}: {e}") from None
        rows = body.get("events") if isinstance(body, dict) else None
        if not isinstance(rows, list):
            raise AdapterError(f"page {page}: answer has no events list")
        # Every answer's count: a feed that grows while it is paged is read to its end.
        total_pages = body.get("total_pages")
        if not isinstance(total_pages, int):
            raise AdapterError(f"page {page}: answer has no total_pages")
        if total_pages > max_pages:
            raise AdapterError(f"{total_pages} pages, more than max_pages {max_pages}")
        for row in rows:
            if not isinstance(row, dict):
                continue
            key = (row.get("id") or row.get("url"), row.get("start_date"))
            if key in seen:
                continue
            seen.add(key)
            events.append(_tribe_event(row))
        page += 1
    return events


# ---------------------------------------------------------------------------
# Showare box office performance list
# ---------------------------------------------------------------------------

SHOWARE_PAGE_SIZE = 100
SHOWARE_MAX_PERFORMANCES = 1000
_SHOWARE_TIME = "%A, %B %d, %Y %I:%M:%S %p"      # "Thursday, October 22, 2026 7:30:00 PM"


def _showare_endpoint(seed: dict, config: dict) -> tuple[str, dict]:
    """config `url`, else the feed event-seed-discovery probed
    (engine/feed_probe), else the seed's own box office — split from its
    query, whose settings are kept and paging replaced (a repeated `page`
    reads as "1, 2")."""
    if config.get("url"):
        url = config["url"]
    elif seed.get("feed_type") == "showare" and seed.get("feed_url"):
        url = seed["feed_url"]
    elif urlsplit(seed["url"]).path.lower() == SHOWARE_LIST_PATH:
        url = seed["url"]
    else:
        url = urljoin(seed["url"], SHOWARE_LIST_PATH)
    parts = urlsplit(url)
    return parts._replace(query="", fragment="").geturl(), dict(parse_qsl(parts.query))


def _showare_time(value) -> Optional[str]:
    """A Showare time as the extraction's form. It is the box office's wall
    clock: the `TimeZone`/`VenuetGMTOffset` beside it stay "PST"/-8 all year."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        return datetime.strptime(" ".join(value.split()), _SHOWARE_TIME).isoformat(
            timespec="seconds")
    except ValueError:
        return None


def _undoubled(text: str) -> str:
    """The feed's address line can be its two parts, both the same: kept once."""
    words = text.split()
    half = len(words) // 2
    return " ".join(words[:half]) if half and words[:half] == words[half:] else " ".join(words)


def _showare_event(row: dict, box_office: str, shows_end: bool) -> dict:
    """A row without a title or start stays in (as None): event-find counts
    it as malformed rather than it vanishing here."""
    start = _showare_time(row.get("PerformanceDateTime"))
    end = _showare_time(row.get("PerformanceEndDateTime")) if shows_end else None
    if (row.get("PerformanceStartTimeAlias") or "").strip() and start:
        start, end = start[:10], None       # "TBA" in place of the clock: no published time
    event_id = row.get("EventID")
    # An event the box office sends elsewhere (`RedirectionEnabled`) is read there.
    redirect = row.get("RedirectURL") if row.get("RedirectionEnabled") else None
    if is_http_url(redirect):
        source_url = redirect
    elif event_id is not None:
        source_url = f"{box_office}/eventperformances.asp?evt={event_id}"
    else:
        source_url = None
    return {
        "title": html_text(row.get("Event")),
        "date": start,
        "end_at": end,
        "venue": html_text(row.get("Venue")),
        "location": location(_undoubled(row.get("VenueAddress") or ""), row.get("VenueCity"),
                              row.get("VenueState"), row.get("VenueZipCode")),
        "description": html_text(row.get("Description")),
        "categories": [],
        "tags": [],
        "source_url": source_url,
    }


def showare(seed: dict, config: dict, get_json: GetJson) -> list[dict]:
    """Every page of the performance list (`perfCount`, as each answer
    gives it, over SHOWARE_PAGE_SIZE rows a page). Each performance is its
    own occurrence (event id + performance id); fewer distinct performances
    than the count — a short page, a list that shifted while paged — is an
    error. The list holds upcoming performances only. The end time is kept
    only when the box office shows it (`settings.ShowEndTime`)."""
    url, query = _showare_endpoint(seed, config)
    max_perfs = int(config.get("max_performances") or SHOWARE_MAX_PERFORMANCES)
    parts = urlsplit(url)
    box_office = f"{parts.scheme}://{parts.netloc}"
    # `listMaxSize` caps `perfCount`: one over the cap tells a longer list apart.
    params = {**query, "action": query.get("action") or "perf",
              "listPageSize": SHOWARE_PAGE_SIZE, "listMaxSize": max_perfs + 1}
    events: list[dict] = []
    seen: set = set()
    page, total_pages, count = 1, 1, 0
    while page <= total_pages:
        try:
            body = get_json(url, {**params, "page": page})
        except AdapterError as e:
            raise AdapterError(f"page {page}: {e}") from None
        if not isinstance(body, dict):
            raise AdapterError(f"page {page}: answer has no performance list")
        count = body.get("perfCount")
        rows = [] if body.get("performance") is None and count == 0 else body.get("performance")
        if not isinstance(rows, list):
            raise AdapterError(f"page {page}: answer has no performance list")
        # Every answer's count: a list that grows while it is paged is read to its end.
        if not isinstance(count, int) or isinstance(count, bool):
            raise AdapterError(f"page {page}: answer has no perfCount")
        if count > max_perfs:
            raise AdapterError(f"more than max_performances {max_perfs} performances")
        settings = body.get("settings") if isinstance(body.get("settings"), dict) else {}
        shows_end = bool(settings.get("ShowEndTime"))
        total_pages = -(-count // SHOWARE_PAGE_SIZE)
        for row in rows:
            if not isinstance(row, dict):
                continue
            key = (row.get("EventID"), row.get("PerformanceID"))
            if key in seen:
                continue
            seen.add(key)
            events.append(_showare_event(row, box_office, shows_end))
        page += 1
    if len(seen) < count:
        raise AdapterError(f"read {len(seen)} of {count} performances")
    return events


# ---------------------------------------------------------------------------
# The registry: one entry per adapter
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Param:
    help: str
    check: Callable[[Any], Optional[str]]    # value → what's wrong with it, None if fine


def _http_url(v) -> Optional[str]:
    return None if is_http_url(v) else "must be an http(s) URL"


def _positive_int(v) -> Optional[str]:
    ok = isinstance(v, int) and not isinstance(v, bool) and v > 0
    return None if ok else "must be a whole number above 0"


@dataclass(frozen=True)
class Adapter:
    """`read(seed, config, get_json)` → the whole listing's events;
    `params`, the settings its config may hold; `feed_types`, the probed
    `event_seeds.feed_type`s (engine/feed_probe) it reads as they are."""
    read: Callable[[dict, dict, GetJson], list[dict]]
    params: dict[str, Param]
    feed_types: tuple[str, ...] = ()


ADAPTERS: dict[str, Adapter] = {
    "tribe": Adapter(tribe, {
        "url": Param("the events endpoint (default: the probed feed, else /wp-json/…)", _http_url),
        "max_pages": Param(f"refuse a feed longer than this (default {TRIBE_MAX_PAGES})",
                           _positive_int),
        "days_ahead": Param("read events through this many days from today", _positive_int),
    }, feed_types=("tribe",)),
    "showare": Adapter(showare, {
        "url": Param("the performancelist.asp endpoint (default: the probed feed, else the "
                     "seed's box office)", _http_url),
        "max_performances": Param("refuse a list longer than this "
                                  f"(default {SHOWARE_MAX_PERFORMANCES})", _positive_int),
    }, feed_types=("showare",)),
}


# ---------------------------------------------------------------------------
# Town adapters: $HERMES_HOME/adapters/<name>.py, each exposing ADAPTER
# ---------------------------------------------------------------------------

TOWN_ADAPTERS_DIR = "adapters"
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
_SHADOWED = object()
# file path → ((mtime_ns, size), Adapter | _SHADOWED | why it was skipped)
_town_cache: dict[str, tuple[tuple, Any]] = {}


@dataclass(frozen=True)
class TownAdapters:
    """`adapters`: the town's own in use; `shadowed`: names a shared adapter
    takes; `errors`: file name → why it was skipped."""
    adapters: dict[str, Adapter] = field(default_factory=dict)
    shadowed: list[str] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)


def town_adapters_dir() -> Path:
    """$HERMES_HOME/adapters — the town agent's own, persistent; read fresh."""
    return Path(os.environ.get("HERMES_HOME", "/home/agent/.hermes")) / TOWN_ADAPTERS_DIR


def _invalid(obj) -> Optional[str]:
    if not isinstance(obj, Adapter):
        return "ADAPTER is not an engine.seed_adapters.Adapter"
    if not callable(obj.read):
        return "ADAPTER.read is not callable"
    if not isinstance(obj.params, dict) or not all(
            isinstance(k, str) and isinstance(v, Param) for k, v in obj.params.items()):
        return "ADAPTER.params must map setting names to Param"
    if not isinstance(obj.feed_types, (tuple, list)) or not all(
            isinstance(t, str) for t in obj.feed_types):
        return "ADAPTER.feed_types must be a tuple of feed type names"
    return None


def _import(path: Path, name: str) -> Any:
    """The file's ADAPTER, or why it can't be used."""
    if name == GENERIC:
        return f"{GENERIC!r} is the Firecrawl → LLM path, not an adapter name"
    if not _NAME_RE.match(name):
        return "the file name names the adapter: a-z, 0-9, _ or -, up to 40, lowercase first"
    mod_name = f"town_adapters.{name}"
    spec = importlib.util.spec_from_file_location(mod_name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[mod_name] = mod       # a dataclass in the file looks its module up
    try:
        spec.loader.exec_module(mod)
    except (Exception, SystemExit) as e:  # noqa: BLE001 — any import failure skips the file
        sys.modules.pop(mod_name, None)
        return f"failed to import — {type(e).__name__}: {e}"
    if not hasattr(mod, "ADAPTER"):
        return "no ADAPTER defined"
    return _invalid(mod.ADAPTER) or mod.ADAPTER


def _load(path: Path) -> Any:
    """Cached per file version, so loading (or skipping) is logged once."""
    try:
        st = path.stat()
    except OSError as e:
        return f"unreadable — {e}"
    stamp = (st.st_mtime_ns, st.st_size)
    cached = _town_cache.get(str(path))
    if cached and cached[0] == stamp:
        return cached[1]
    name = path.stem
    if name in ADAPTERS:
        got = _SHADOWED
        _log.warning(f"town adapter {path} is shadowed by the shared {name!r} adapter",
                     extra={"adapter": name, "path": str(path)})
    else:
        got = _import(path, name)
        if isinstance(got, str):
            _log.warning(f"town adapter {path} skipped: {got}",
                         extra={"adapter": name, "path": str(path)})
    _town_cache[str(path)] = (stamp, got)
    return got


def town_adapters() -> TownAdapters:
    """The town's adapter files (`_`-prefixed ones are helpers, not adapters)."""
    folder = town_adapters_dir()
    if not folder.is_dir():
        return TownAdapters()
    out = TownAdapters()
    for path in sorted(p for p in folder.glob("*.py") if not p.name.startswith("_")):
        got = _load(path)
        if got is _SHADOWED:
            out.shadowed.append(path.stem)
        elif isinstance(got, str):
            out.errors[path.name] = got
        else:
            out.adapters[path.stem] = got
    return out


def registry() -> dict[str, Adapter]:
    """Every adapter a seed can name: the shared ones first, then the town's."""
    return {**ADAPTERS, **town_adapters().adapters}


def why_unknown(name: str) -> str:
    """Why `name` isn't an adapter — a town file that didn't load says so."""
    why = town_adapters().errors.get(f"{name}.py")
    return f"town adapter {name}.py did not load: {why}" if why \
        else f"unknown adapter {name!r}"


def for_feed(feed_type: Optional[str]) -> Optional[str]:
    """The adapter that reads a seed's probed feed, None when none does."""
    return next((k for k, a in registry().items() if feed_type and feed_type in a.feed_types),
                None)


def check_config(adapter: str, config: dict) -> None:
    """ValueError for an unknown adapter, or naming the first setting
    `adapter` doesn't declare, or whose value its check refuses."""
    known = registry()
    if adapter not in known:
        raise ValueError(why_unknown(adapter))
    params = known[adapter].params
    for key, value in config.items():
        if key not in params:
            raise ValueError(f"{adapter} has no setting {key!r} "
                             f"(settings: {', '.join(sorted(params)) or 'none'})")
        why = params[key].check(value)
        if why:
            raise ValueError(f"{adapter} setting {key!r} {why} (got {value!r})")


# ---------------------------------------------------------------------------
# Fixture runs: a captured response through an adapter, no network
# ---------------------------------------------------------------------------

FIXTURE_SEED_URL = "https://fixture.invalid/"


class FixtureFeed:
    """A captured source answering an adapter's `get_json`: a JSON file
    answers every request; a directory answers each by its `page` param
    from `page-<N>.json` (no `page` is page 1). `requests` is what the
    adapter asked for."""

    def __init__(self, path: Union[str, Path]):
        self.path = Path(path)
        self.requests: list[dict] = []

    def __call__(self, url: str, params: dict) -> dict:
        self.requests.append({"url": url, "params": dict(params)})
        file = (self.path / f"page-{params.get('page', 1)}.json" if self.path.is_dir()
                else self.path)
        try:
            text = file.read_text()
        except OSError:
            raise AdapterError(f"fixture has no {file.name}") from None
        try:
            return json.loads(text)
        except ValueError as e:
            raise AdapterError(f"fixture {file.name} is not JSON: {e}") from None


@dataclass(frozen=True)
class FixtureRun:
    """`diff` is empty when the events are `expected` (or none was given)."""
    events: list[dict]
    requests: list[dict]
    expected: Optional[list] = None
    diff: str = ""


def _dump(value) -> list[str]:
    return json.dumps(value, indent=1, sort_keys=True, ensure_ascii=False).splitlines()


def run_fixture(adapter: str, fixture: Union[str, Path], *, seed: Optional[dict] = None,
                config: Optional[dict] = None, expected: Optional[list] = None) -> FixtureRun:
    """Read `fixture` through `adapter` as it would read `seed` (only what the
    adapter reads matters — url, feed_type, feed_url). ValueError for an
    unknown adapter or a config its params refuse; AdapterError when the
    read fails."""
    check_config(adapter, config or {})
    feed = FixtureFeed(fixture)
    events = read({"seed_id": 0, "name": "fixture", "url": FIXTURE_SEED_URL, "feed_type": None,
                   "feed_url": None, **(seed or {}), "adapter": adapter,
                   "adapter_config": json.dumps(config) if config else None},
                  get_json=feed).events
    diff = "" if expected is None or expected == events else "\n".join(difflib.unified_diff(
        _dump(expected), _dump(events), "expected", "read", lineterm=""))
    return FixtureRun(events, feed.requests, expected, diff)


def run_case(adapter: str, case: Union[str, Path], *, seed: Optional[dict] = None,
             config: Optional[dict] = None, expected: Optional[list] = None) -> FixtureRun:
    """A case directory — tests/fixtures/adapters/<name>/, and what a
    promotion proposal carries: `fixture.json` or `fixture/page-<N>.json`
    (the capture), `expected.json` (the events it must read as) and,
    optionally, `seed.json` (the seed's url/feed_type/feed_url and a
    `config`). `seed`, `config` and `expected` given here win over the
    case's own."""
    case = Path(case)
    fixture = case / "fixture" if (case / "fixture").is_dir() else case / "fixture.json"
    if not fixture.exists():
        raise ValueError(f"{case} has no fixture.json or fixture/ directory")
    try:
        stored = (json.loads((case / "seed.json").read_text())
                  if (case / "seed.json").exists() else {})
        if expected is None and (case / "expected.json").exists():
            expected = json.loads((case / "expected.json").read_text())
    except ValueError as e:
        raise ValueError(f"{case}: {e}") from None
    stored_config = stored.pop("config", None)
    return run_fixture(adapter, fixture, seed={**stored, **(seed or {})},
                       config=config if config is not None else stored_config,
                       expected=expected)
