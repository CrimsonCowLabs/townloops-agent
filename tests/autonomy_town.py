"""A whole town, in-process, for autonomous-mode tests.

The agent is a black box between scraped pages and TownLoops: tests drive
its real cron entry points (event-find, the workqueue drain, the moderation
poll, seed review, organisation discovery, cancellations) against
`tests.fake_townloops.FakeAPI`, with Firecrawl and Codex answered from
canned pages and replies at their usual monkeypatch points, and look only at
what reached TownLoops, what didn't, the recorded decisions and the digest.

The drain runs each workqueue skill's `main()` in-process (instead of a
subprocess) so it shares the fake API.
"""

from __future__ import annotations

import contextlib
import importlib.util
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

PROFILE = {"slug": "slt", "name": "South Lake Tahoe", "region": "California",
           "name_aliases": ["SLT", "Stateline"], "operating_mode": "autonomous",
           "autonomy": {}}


def soon(days: int = 7, hour: int = 19) -> str:
    """A local wall-clock ISO time `days` from today (naive, as extracted)."""
    d = datetime.now(timezone.utc).date() + timedelta(days=days)
    return f"{d.isoformat()}T{hour:02d}:00:00"


def page_text(*lines: str) -> str:
    """Seed page markdown — padded past the empty-page check."""
    return "\n\n".join(lines) + "\n\n" + ("Upcoming events in our community. " * 8)


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
        self.telegrams: list[str] = []
        self.search: dict[str, list[dict]] = {}
        self.redirects: dict[str, str] = {}
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
        monkeypatch.setattr("engine.event_detail.firecrawl_markdown", self._fetch_detail)
        monkeypatch.setattr("engine.event_detail.codex_exec", self._codex)
        monkeypatch.setattr("engine.event_people.codex_exec", self._codex)
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
            f"{e['title']} — {e.get('date')} — {e.get('venue') or ''} {e.get('location') or ''}"
            for e in events))
        self.extractions[url] = events

    # --- fakes ---------------------------------------------------------------

    def _fetch(self, url: str, timeout_s: int = 0):
        md = self.pages.get(url)
        return (True, md, "") if md else (False, "", "firecrawl http 404")

    def _fetch_detail(self, url: str, timeout_s: int = 0):
        """An event's own page: canned, else a plain page that states nothing."""
        return True, self.pages.get(url) or page_text("Event details."), ""

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
        self.mp.setattr(mod, "_firecrawl_markdown", self._fetch)
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

    def _scrape(self, url: str):
        """event-seed-discovery's rawHtml scrape: (ok, code, html, final url)."""
        final = self.redirects.get(url, url)
        return (final in self.pages or url in self.redirects), 200, "<html></html>", final

    def digest(self) -> tuple[dict, str, str]:
        from engine import orchestrator
        with contextlib.redirect_stdout(io.StringIO()):
            out = orchestrator.summary(self.conn, hours=24)
        return out, Path(out["report_path"]).read_text(), self.telegrams[-1]

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
