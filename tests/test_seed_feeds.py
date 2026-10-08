"""event-seed-discovery records the feed it detects on each seed's page —
for new seeds while verifying them, and for existing seeds via `probe`."""

from __future__ import annotations

import argparse
import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

PAGES = Path(__file__).parent / "fixtures" / "feeds"
SKILL = Path(__file__).resolve().parent.parent / "skills/business/event-seed-discovery/discover.py"


@pytest.fixture
def conn(fresh_db):
    fresh_db.row_factory = sqlite3.Row
    fresh_db.commit()
    return fresh_db


@pytest.fixture
def skill(conn, tmp_state_dir, monkeypatch):
    """The discover.py module against the test DB; `pages` maps a URL to
    (fixture page, final URL after redirects)."""
    spec = importlib.util.spec_from_file_location("seed_discovery_feeds", SKILL)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "_DB_PATH", tmp_state_dir / "townloops.db")
    monkeypatch.setattr(mod, "_load_town_profile", lambda: {"name": "Test Town"})
    mod.pages = {}

    def scrape(url):
        if url not in mod.pages:
            return (False, 404, "", url)
        page, final = mod.pages[url]
        return (True, 200, (PAGES / page).read_text(), final)

    monkeypatch.setattr(mod, "_scrape_page", scrape)
    return mod


def _seed(conn, url):
    return dict(conn.execute("SELECT * FROM event_seeds WHERE url = ?", (url,)).fetchone())


def _add_seed(conn, name, url, status="approved"):
    conn.execute("INSERT INTO event_seeds (name, url, kind, status, first_seen_at, last_seen_at) "
                 "VALUES (?, ?, 'venue', ?, 'x', 'x')", (name, url, status))
    conn.commit()


def test_discover_records_the_feed_of_each_new_seed(skill, conn, monkeypatch):
    skill.pages = {"https://library.example/events": ("ical_link.html", "https://library.example/events"),
                   "https://about.example/": ("plain.html", "https://about.example/")}
    proposals = [{"name": "Library", "url": "https://library.example/events", "kind": "library"},
                 {"name": "About", "url": "https://about.example/", "kind": "other"}]
    monkeypatch.setattr(skill, "_codex_exec", lambda prompt: (True, json.dumps(proposals), ""))
    assert skill.cmd_discover(argparse.Namespace(limit=15, dry_run=False)) == 0

    lib = _seed(conn, "https://library.example/events")
    assert (lib["feed_type"], lib["feed_url"]) == ("ical", "https://library.example/events/feed.ics")
    assert lib["feed_probed_at"]
    about = _seed(conn, "https://about.example/")
    assert about["feed_type"] is None and about["feed_probed_at"]


def test_probe_fills_seeds_never_probed_and_follows_redirects(skill, conn):
    _add_seed(conn, "Shops", "https://bit.example/shops")
    _add_seed(conn, "Rejected", "https://nope.example/", status="rejected")
    skill.pages = {"https://bit.example/shops": ("tribe.html", "https://shops.example/events/")}
    assert skill.cmd_probe(argparse.Namespace(seed_id=None, all=False, limit=None)) == 0

    shops = _seed(conn, "https://bit.example/shops")
    assert shops["feed_type"] == "tribe"
    assert shops["feed_url"] == "https://shops.example/wp-json/tribe/events/v1/events"
    assert json.loads(shops["feed_detail"])["resolved_url"] == "https://shops.example/events/"
    assert _seed(conn, "https://nope.example/")["feed_probed_at"] is None


def test_probe_skips_probed_seeds_unless_asked(skill, conn):
    _add_seed(conn, "Riva", "https://riva.example/events")
    skill.pages = {"https://riva.example/events": ("plain.html", "https://riva.example/events")}
    skill.cmd_probe(argparse.Namespace(seed_id=None, all=False, limit=None))
    skill.pages = {"https://riva.example/events": ("jsonld.html", "https://riva.example/events")}
    skill.cmd_probe(argparse.Namespace(seed_id=None, all=False, limit=None))
    assert _seed(conn, "https://riva.example/events")["feed_type"] is None
    skill.cmd_probe(argparse.Namespace(seed_id=None, all=True, limit=None))
    assert _seed(conn, "https://riva.example/events")["feed_type"] == "jsonld"


def test_unreachable_seed_stays_unprobed_behind_untried_ones(skill, conn):
    # A capped probe run (cron: --limit) tries never-attempted seeds before
    # ones that were unreachable last time, so a dead seed can't hog it.
    _add_seed(conn, "Down", "https://down.example/")
    skill.cmd_probe(argparse.Namespace(seed_id=None, all=False, limit=1))
    down = _seed(conn, "https://down.example/")
    assert down["feed_probed_at"] is None and "unreachable" in down["feed_detail"]

    _add_seed(conn, "Riva", "https://riva.example/events")
    skill.pages = {"https://riva.example/events": ("jsonld.html", "https://riva.example/events")}
    skill.cmd_probe(argparse.Namespace(seed_id=None, all=False, limit=1))
    assert _seed(conn, "https://riva.example/events")["feed_type"] == "jsonld"
