"""A whole town, in-process, for autonomous-mode tests.

The agent is a black box between scraped pages and TownLoops: tests drive
its real cron entry points (event-find, the workqueue drain, the moderation
poll, seed review, organisation discovery, cancellations) against
`tests.fake_townloops.FakeAPI`, with Firecrawl and Codex answered from
canned pages and replies at their usual monkeypatch points, and look only at
what reached TownLoops, what didn't, the recorded decisions and the digest.

The drain runs each workqueue skill's `main()` in-process (instead of a
subprocess) so it shares the fake API. `cli(skill, *argv)` runs any skill's
CLI the same way (what cron and the analyst run call); `run_day()` runs a
whole day of the deterministic schedule, ending in the daily report.
"""

from __future__ import annotations

import contextlib
import importlib.util
import inspect
import io
import json
import re
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

import yaml

from tests.fake_townloops import FakeAPI

ROOT = Path(__file__).resolve().parent.parent
SKILLS = ROOT / "skills" / "business"

# Fixture events carry one-line descriptions ("A race."): the thin-event
# hold (engine/event_quality) is off unless a test is about it
# (tests/test_event_quality.py turns it on).
PROFILE = {"slug": "slt", "name": "South Lake Tahoe", "region": "California",
           "name_aliases": ["SLT", "Stateline"], "operating_mode": "autonomous",
           "autonomy": {"hold_thin_events": False}}


def soon(days: int = 7, hour: int = 19) -> str:
    """A local wall-clock ISO time `days` from today (naive, as extracted)."""
    d = datetime.now(timezone.utc).date() + timedelta(days=days)
    return f"{d.isoformat()}T{hour:02d}:00:00"


def page_text(*lines: str) -> str:
    """Seed page markdown — padded past the empty-page check."""
    return "\n\n".join(lines) + "\n\n" + ("Upcoming events in our community. " * 8)


def as_listed(value) -> str:
    """An extracted ISO date/time as a listing prints it — "2027-03-20 ·
    8:00 PM": a page states a clock time in words, not as an ISO timestamp."""
    try:
        dt = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return str(value)
    if "T" not in value:
        return value
    return f"{dt.date().isoformat()} · {dt.hour % 12 or 12}:{dt.minute:02d} " \
           f"{'PM' if dt.hour >= 12 else 'AM'}"


def _answer(text: str) -> dict:
    """A CLI's JSON answer: the whole output, else from its first `{`, else
    its last JSON line; a list comes back as {"items": [...]}."""
    candidates = [text.strip()]
    if "{" in text:
        candidates.append(text[text.index("{"):].strip())
    candidates += [line for line in reversed(text.splitlines()) if line.startswith(("{", "["))]
    for c in candidates:
        try:
            doc = json.loads(c)
        except ValueError:
            continue
        return doc if isinstance(doc, dict) else {"items": doc}
    return {"output": text}


def load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class Town:
    def __init__(self, tmp_state_dir: Path, monkeypatch, conn: sqlite3.Connection,
                 api: Optional[FakeAPI] = None, profile: Optional[dict] = None):
        self.state = tmp_state_dir
        self.mp = monkeypatch
        self.conn = conn
        self.api = api or FakeAPI(event_status="pending", entity_status="pending")
        self.pages: dict[str, str] = {}
        self.extractions: dict[str, list[dict]] = {}
        self.judge: Callable[[str], str] = lambda prompt: "[]"
        self.details: dict[str, dict] = {}
        self.page_html: dict[str, str] = {}   # an event page's HTML (og tags, JSON-LD)
        self.telegrams: list[str] = []
        self.search: dict[str, list[dict]] = {}
        self.redirects: dict[str, str] = {}
        self.web: dict[str, list[dict]] = {}    # web search results by query prefix
        self.sites: dict[str, dict] = {}        # site pages {markdown, links} for discovery
        self.searched: list[str] = []
        self.commands: list[tuple[str, ...]] = []   # every `cli` call, as (skill, *argv)
        self._skills: dict[str, object] = {}
        db = tmp_state_dir / "townloops.db"
        self.profile_path = tmp_state_dir.parent / "town-profile.yaml"
        self.set_profile(profile or PROFILE)
        for k, v in (("TOWNLOOPS_API_URL", "https://tl.example"), ("TOWNLOOPS_API_KEY", "k"),
                     ("TOWNLOOPS_TOWN_SLUG", "slt"), ("TOWN_PROFILE_PATH", str(self.profile_path)),
                     ("TOWNLOOPS_ENRICH_MAX_LOOKUPS", "0")):
            monkeypatch.setenv(k, v)
        for target in ("engine.derive_helpers._DB_PATH", "engine.inspection._DB_PATH"):
            monkeypatch.setattr(target, db)
        monkeypatch.setattr("engine.db.DEFAULT_DB_PATH", str(db))
        import engine.townloops_api as tl
        monkeypatch.setattr(tl, "Client", lambda *a, **k: self.api)
        monkeypatch.setattr("engine.orchestrator._telegram_notify", self.telegrams.append)
        monkeypatch.setattr("engine.notify.telegram", self.telegrams.append)
        monkeypatch.setattr("engine.event_detail.firecrawl_page", self._fetch_detail)
        monkeypatch.setattr("engine.event_detail.codex_exec", self._codex)
        monkeypatch.setattr("engine.event_people.codex_exec", self._codex)
        # Enrichment's page fields (entity_classify.codex_llm) resolve it lazily.
        monkeypatch.setattr("engine.event_extract.codex_exec", self._codex)
        self.people: dict[str, dict] = {}   # event title → host/people AI answer
        monkeypatch.setattr("engine.orchestrator._invoke_skill", self._invoke)
        (tmp_state_dir / "categories.json").write_text(json.dumps(
            [{"slug": "music", "name": "Music"}, {"slug": "community", "name": "Community"}]))

    # --- setup ---------------------------------------------------------------

    def set_profile(self, profile: dict, **autonomy) -> None:
        doc = json.loads(json.dumps(profile))
        doc.setdefault("autonomy", {}).update(autonomy)
        self.profile_path.write_text(yaml.safe_dump(doc))
        self.profile = doc

    def seed(self, name: str, url: str, status: str = "approved", kind: str = "venue",
             **cols) -> int:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        data = {"name": name, "url": url, "kind": kind, "status": status,
                "first_seen_at": now, "last_seen_at": now, **cols}
        cur = self.conn.execute(
            f"INSERT INTO event_seeds ({', '.join(data)}) VALUES ({', '.join('?' * len(data))})",
            tuple(data.values()))
        self.conn.commit()
        return cur.lastrowid

    def listing(self, url: str, events: list[dict], *lines: str) -> None:
        """What a seed page shows and what extraction finds on it."""
        self.pages[url] = page_text(*lines, *(
            (f"[{e['title']}]({e['source_url']})" if e.get("source_url") else e["title"])
            + f" — {as_listed(e.get('date'))}"
            f"{' – ' + as_listed(e['end_at']) if e.get('end_at') else ''} "
            f"— {e.get('venue') or ''} {e.get('location') or ''}"
            for e in events))
        self.extractions[url] = events

    # --- fakes ---------------------------------------------------------------

    def _fetch(self, url: str, timeout_s: int = 0):
        md = self.pages.get(url)
        return (True, md, "") if md else (False, "", "firecrawl http 404")

    def _fetch_detail(self, url: str, timeout_s: int = 0):
        """An event's own page: canned, else a plain page naming the event
        (engine/event_provenance) that states nothing else."""
        titles = [e["title"] for events in self.extractions.values() for e in events
                  if e.get("source_url") == url]
        return True, self.pages.get(url) or page_text(*(f"# {t}" for t in titles[:1]),
                                                      "Event details."), "", \
            {"html": self.page_html.get(url), "metadata": {}}

    def judge_by_title(self, answers: dict[str, dict]) -> None:
        """Answer the judgement call per item title (items come numbered)."""
        def judge(prompt: str) -> str:
            out = []
            for m in re.finditer(r'^(\d+)\. "(.*?)" — ', prompt, re.MULTILINE):
                if m.group(2) in answers:
                    out.append({"i": int(m.group(1)), **answers[m.group(2)]})
            return json.dumps(out)
        self.judge = judge

    def _codex(self, prompt: str, timeout_s: int = 0):
        if prompt.startswith("You are extracting upcoming events"):
            url = re.search(r'scraped markdown of ".*?" \((\S+)\)', prompt).group(1)
            return True, json.dumps(self.extractions.get(url, [])), ""
        if prompt.startswith("You are checking listings"):
            return True, self.judge(prompt), ""
        if prompt.startswith("Below is the text about the event"):
            title = prompt.split('"', 2)[1]
            return True, json.dumps(self.people.get(title, {})), ""
        if "detail page (" in prompt:
            url = prompt.split("detail page (", 1)[1].split(")", 1)[0]
            return True, json.dumps(self.details.get(url, {})), ""
        return True, "{}", ""

    def _skill(self, skill: str, entry: str):
        if skill not in self._skills:
            self._skills[skill] = load(SKILLS / skill / entry, f"town_{skill.replace('-', '_')}")
        return self._skills[skill]

    def _invoke(self, skill: str, entry: str, event_uid: str, timeout_s: int = 120):
        mod = self._skill(skill, entry)
        err, out = io.StringIO(), io.StringIO()
        self.mp.setattr(sys, "argv", [entry, "--event-uid", event_uid])
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
            try:
                rc = mod.main()
            except SystemExit as e:
                rc = e.code or 0
        return rc, out.getvalue()[-2000:], err.getvalue()[-2000:]

    # --- entry points --------------------------------------------------------

    def find(self, *argv: str) -> dict:
        mod = load(SKILLS / "event-find" / "find.py", "town_event_find")
        self.mp.setattr(mod, "_DB_PATH", self.state / "townloops.db")
        self.mp.setattr(mod, "_firecrawl_listing", self._fetch)
        self.mp.setattr(mod, "_codex_exec", self._codex)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            assert mod.main(list(argv)) == 0
        lines = [json.loads(line) for line in out.getvalue().splitlines() if line.startswith("{")]
        return next((x["summary"] for x in lines if "summary" in x), {})

    def drain(self, times: int = 1) -> dict:
        from engine import orchestrator
        out = {}
        with contextlib.redirect_stdout(io.StringIO()):
            for _ in range(times):
                out = orchestrator.drain_workqueue(self.conn)
        return out

    def poll(self) -> dict:
        mod = load(SKILLS / "moderation-feedback" / "poll.py", "town_moderation_feedback")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            assert mod.main([]) == 0
        text = out.getvalue()
        return json.loads(text[text.index("{"):]) if "{" in text else {}

    def seed_review(self, *argv: str) -> dict:
        mod = load(SKILLS / "event-seed-discovery" / "discover.py", "town_seed_discovery")
        self.mp.setattr(mod, "_DB_PATH", self.state / "townloops.db")
        self.mp.setattr(mod, "_trial_fetch", self._fetch)
        self.mp.setattr(mod, "_codex_exec", self._codex)
        self.mp.setattr(mod, "_scrape_page", self._scrape)
        self.mp.setattr(mod, "_site_search", lambda q: self.search.get(q, []))
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            assert mod.main(["review", *argv]) == 0
        return json.loads(out.getvalue().strip().splitlines()[-1])

    def live_sync(self) -> dict:
        """entity-live-sync's nightly run (entity sync, release pass, Series)."""
        mod = load(SKILLS / "entity-live-sync" / "sync.py", "town_entity_live_sync")
        self.mp.setattr(sys, "argv", ["sync.py", "run"])
        self.conn.commit()   # it opens its own connection
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            assert mod.main() == 0
        return json.loads(out.getvalue().strip().splitlines()[-1])

    def _scrape(self, url: str):
        """event-seed-discovery's rawHtml scrape: (ok, code, html, final url)."""
        final = self.redirects.get(url, url)
        return (final in self.pages or url in self.redirects), 200, "<html></html>", final

    def digest(self) -> tuple[dict, str, str]:
        from engine import orchestrator
        with contextlib.redirect_stdout(io.StringIO()):
            out = orchestrator.summary(self.conn, hours=24)
        return out, Path(out["report_path"]).read_text(), self.telegrams[-1]

    def report(self, *argv: str) -> dict:
        """town-report's CLI (the daily report, the analyst-run budget):
        its exit code and its JSON answer."""
        mod = load(SKILLS / "town-report" / "report.py", "town_report_cli")
        self.conn.commit()   # it opens its own connection
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = mod.main(list(argv))
        text = out.getvalue()
        return {"rc": rc, **(json.loads(text[text.index("{"):]) if "{" in text else {})}

    def heartbeat(self) -> None:
        """The 10-minute heartbeat wrapper's own work — migrate, then the
        standing-alert tick; its detached drain is not started here."""
        import subprocess

        class NoDrain:
            pid = 0

            def __init__(self, *a, **k):
                pass
        mod = load(ROOT / "scripts" / "workqueue_heartbeat.py", "town_heartbeat")
        self.mp.setattr(subprocess, "Popen", NoDrain)
        self.conn.commit()
        with contextlib.redirect_stdout(io.StringIO()):
            assert mod.main([]) == 0

    def seed_row(self, name: str) -> dict:
        cur = self.conn.cursor()
        cur.row_factory = sqlite3.Row
        return dict(cur.execute("SELECT * FROM event_seeds WHERE name = ?", (name,)).fetchone())

    def drain_all(self, limit: int = 15) -> None:
        """Drain until a tick claims nothing."""
        for _ in range(limit):
            out = self.drain()
            if not any(v.get("claimed") for v in out.get("per_action", {}).values()):
                return

    def run(self) -> None:
        """A find, then drains until the approved events are through."""
        self.find()
        self.drain_all()

    # --- any skill's CLI, and a whole day ------------------------------------

    def cli(self, skill: str, *argv: str) -> dict:
        """A skill's CLI as cron or the analyst run calls it (`python
        <skill>/<entry>.py ARGV`), in-process with the town's fakes: its exit
        code and its (last) JSON answer."""
        [entry] = [p for p in (SKILLS / skill).glob("*.py")]
        mod = load(entry, f"town_cli_{skill.replace('-', '_')}")
        self._fake_network(skill, mod)
        self.conn.commit()   # the CLI opens its own connection
        self.mp.setattr(sys, "argv", [entry.name, *argv])
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            try:
                rc = (mod.main(list(argv)) if inspect.signature(mod.main).parameters
                      else mod.main())
            except SystemExit as e:
                rc = e.code if isinstance(e.code, int) else (0 if e.code is None else 1)
        self.commands.append((skill, *argv))
        return {"rc": rc or 0, **_answer(out.getvalue())}

    def _fake_network(self, skill: str, mod) -> None:
        if skill == "event-seed-discovery":
            for name, fake in (("_DB_PATH", self.state / "townloops.db"),
                               ("_trial_fetch", self._fetch), ("_codex_exec", self._codex),
                               ("_scrape_page", self._verify), ("_hunt_search", self._web_search),
                               ("_site_scrape", self._site_scrape),
                               ("_site_search", lambda q: self.search.get(q, []))):
                self.mp.setattr(mod, name, fake)
        elif skill == "event-find":
            self.mp.setattr(mod, "_DB_PATH", self.state / "townloops.db")
            self.mp.setattr(mod, "_firecrawl_listing", self._fetch)
            self.mp.setattr(mod, "_codex_exec", self._codex)
        elif skill in ("business-discovery", "performer-research", "entity-live-sync"):
            self.mp.setattr("engine.discovery.firecrawl_search",
                            lambda q, limit=5, raise_on_error=False: self._web_search(q))
            self.mp.setattr("engine.discovery.firecrawl_scrape",
                            lambda url, raise_on_error=False: self._site_scrape(url))
            self.mp.setattr("engine.discovery.BETWEEN_QUERIES_S", 0)
            self.mp.setattr("engine.research.fetch_html", self._site_html)
            self.mp.setattr("engine.research.firecrawl_fetch", lambda url: (None, None))

    def _web_search(self, query: str) -> list[dict]:
        """Web search (Firecrawl/SearXNG): results by query prefix."""
        self.searched.append(query)
        return next((v for k, v in self.web.items() if query.startswith(k)), [])

    def _site_scrape(self, url: str) -> dict:
        return self.sites.get(url.rstrip("/")) or self.sites.get(url) or {}

    def _site_html(self, url: str, timeout: float = 20.0):
        md = self._site_scrape(url).get("markdown")
        return (f"<html><body><p>{md}</p></body></html>", url) if md else (None, None)

    def _verify(self, url: str):
        """A seed URL's rawHtml verify: a canned page or site answers 200."""
        final = self.redirects.get(url, url)
        ok = final in self.pages or url.rstrip("/") in self.sites or url in self.redirects
        return ok, 200 if ok else 404, "<html></html>", final

    def advance(self, minutes: int = 10) -> None:
        """Move the workqueue `minutes` into the past: its hourly rate windows
        (engine/workqueue) see the claims as that much older."""
        self.conn.execute(
            "UPDATE event_workqueue SET claimed_at = "
            "strftime('%Y-%m-%dT%H:%M:%S+00:00', datetime(claimed_at, ?)) "
            "WHERE claimed_at IS NOT NULL", (f"-{minutes} minutes",))
        self.conn.commit()

    def queue_open(self) -> int:
        from engine.workqueue import MAX_ATTEMPTS
        return self.conn.execute("SELECT COUNT(*) FROM event_workqueue WHERE resolved_at IS NULL "
                                 "AND attempts < ?", (MAX_ATTEMPTS,)).fetchone()[0]

    def run_day(self, *, ticks: int = 144,
                moderate: Optional[Callable[["Town"], None]] = None) -> dict:
        """One day of the town's deterministic schedule (cron/jobs.json), in
        its order: organisation harvest, entity live sync, seed hunt and
        review, the morning event-find, then 10-minute ticks — a drain each
        (time moves on 10 minutes, `advance`), and hourly the moderator's
        turn (`moderate`), the moderation poll, business promote and the
        heartbeat's alert tick — until the queue is empty at an hour's end;
        then the daily report (`town-report build --send`). Returns each
        step's answer and the ticks it took."""
        out = {"harvest": self.cli("business-discovery", "harvest"),
               "live_sync": self.cli("entity-live-sync", "run"),
               "hunt": self.cli("event-seed-discovery", "hunt"),
               "review": self.cli("event-seed-discovery", "review", "--trials", "8"),
               "find": self.find()}
        tick = 0
        for tick in range(1, ticks + 1):
            if self.queue_open():
                self.drain()
            if tick % 6 == 0:
                if moderate:
                    moderate(self)
                out["poll"] = self.cli("moderation-feedback")
                out["promote"] = self.cli("business-discovery", "promote", "--limit", "3")
                self.heartbeat()
                if not self.queue_open():
                    break
            self.advance(10)
        out["ticks"] = tick
        out["report"] = self.report("build", "--send")
        return out

    def publish_all(self) -> None:
        """The moderator publishes everything pending."""
        for resource, slug in self.api.created:
            row = self.api.tables.get(resource, {}).get(slug)
            if row and row.get("status") == "pending":
                self.api.approve(resource, slug)

    # --- outcomes ------------------------------------------------------------

    def submitted_events(self) -> list[dict]:
        return [c[2] for c in self.api.calls if c[:2] == ("POST", "events")]

    def submitted_titles(self) -> list[str]:
        return sorted(p["title"] for p in self.submitted_events())

    def decisions(self, subject: Optional[str] = None, target: Optional[str] = None):
        from engine import autonomy
        return autonomy.decisions(self.conn, subject=subject, target=target, limit=500)

    def reasons(self, subject: str = "event") -> dict[str, str]:
        """{title: reason} of the latest decision per event."""
        out = {}
        for d in reversed(self.decisions(subject)):
            row = self.conn.execute("SELECT COALESCE(title, name) FROM events WHERE event_uid = ?",
                                    (d["target"],)).fetchone()
            out[row[0] if row else d["target"]] = d["reason"]
        return out
