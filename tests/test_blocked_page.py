"""Challenge and error pages: what a fetch got back that is not the page
(Incapsula / Cloudflare challenges, a platform's 404, Showare's `no action`,
a near-empty body, an HTTP error status) is a failed run with a named
reason — never LLM-extracted, never a zero, never a vanish miss."""

from __future__ import annotations

import argparse
import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

from engine import blocked_page, seed_adapters
from engine import source_health as sh
from engine.event_extract import event_uid, firecrawl_listing, firecrawl_markdown

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "blocked"


def _fx(name: str) -> str:
    return (FIXTURES / name).read_text()


# fixture, Firecrawl metadata, expected reason
BLOCKED = [
    ("incapsula.md", {"statusCode": 200}, "incapsula challenge"),
    ("cloudflare.md", {"statusCode": 403, "title": "Just a moment..."}, "cloudflare challenge"),
    ("growthzone-404.md", {"statusCode": 200, "title": "Page Not Found"}, "platform 404 page"),
    ("showare.txt", {"statusCode": 200}, "showare no action"),
    ("near-empty.md", {"statusCode": 200}, "near-empty body"),
]


class TestReason:
    @pytest.mark.parametrize("name,meta,want", BLOCKED)
    def test_each_fixture_is_named(self, name, meta, want):
        assert blocked_page.reason(_fx(name), meta.get("statusCode"), meta.get("title")) == want

    @pytest.mark.parametrize("name", ["normal.md", "quiet.md"])
    def test_real_pages_pass(self, name):
        # A small page that lists nothing is still the page: an explicit zero.
        assert blocked_page.reason(_fx(name), 200, "Events") is None
        assert blocked_page.reason(_fx(name)) is None

    @pytest.mark.parametrize("status", [403, 404, 410, 429, 500, 503])
    def test_error_status_is_a_failure(self, status):
        assert blocked_page.reason(_fx("normal.md"), status) == f"http {status}"

    def test_platform_404_with_its_status(self):
        assert blocked_page.reason(_fx("growthzone-404.md"), 404) == "http 404"

    def test_empty_body(self):
        assert blocked_page.reason("", 200) == "near-empty body"
        assert blocked_page.reason("   \n", None) == "near-empty body"

    def test_cloudflare_challenge_as_raw_html(self):
        html = ("<!DOCTYPE html><html><head><title>Just a moment...</title>"
                "<script src=\"/cdn-cgi/challenge-platform/h/b/orchestrate/chl_page/v1\"></script>"
                "</head><body><noscript>Enable JavaScript and cookies to continue</noscript>"
                "</body></html>")
        assert blocked_page.reason(html, 403) == "cloudflare challenge"

    def test_incapsula_resource_script(self):
        html = ('<html><head><META NAME="robots" CONTENT="noindex,nofollow">'
                '<script src="/_Incapsula_Resource?SWJIYLWA=5074a744e2e3d891814e9a2dace20bd4">'
                '</script></head><body></body></html>')
        assert blocked_page.reason(html, 200) == "incapsula challenge"

    def test_markers_on_a_big_page_are_content(self):
        # An article that mentions a challenge is not one.
        body = _fx("normal.md") + ("\n\nOur ticketing partner sometimes shows a "
                                   "'Just a moment...' Cloudflare check. " * 120)
        assert blocked_page.reason(body, 200) is None

    @pytest.mark.parametrize("line", ["404 Lounge — Comedy Night", "404 Main St Block Party",
                                      "Page 404 of our archive"])
    def test_a_name_starting_with_404_is_content(self, line):
        body = f"# {line}\n\n" + _fx("normal.md")
        assert blocked_page.reason(body, 200, line) is None

    @pytest.mark.parametrize("line", ["404", "404 — Page not found", "404: Not Found",
                                      "Error 404", "404 Error", "Not Found"])
    def test_a_not_found_heading_or_first_line(self, line):
        padded = "\n\nSorry about that. Try the menu above or search our site. " * 3
        assert blocked_page.reason(f"# {line}" + padded) == blocked_page.NOT_FOUND
        assert blocked_page.reason(line + padded) == blocked_page.NOT_FOUND
        assert blocked_page.reason(padded, None, line) == blocked_page.NOT_FOUND

    @pytest.mark.parametrize("sentence", [
        "Sorry, the page you requested could not be found.",
        "This page no longer exists. It may have moved.",
        "The page cannot be found.",
    ])
    def test_a_not_found_sentence_near_the_top(self, sentence):
        body = f"# Oops!\n\n{sentence}\n\n" + "Try the menu above or search our site. " * 3
        assert blocked_page.reason(body) == blocked_page.NOT_FOUND

    @pytest.mark.parametrize("body,want", [
        ("Please complete the CAPTCHA to continue to tahoe.example. " * 2, "captcha challenge"),
        ("Press & Hold to confirm you are a human (and not a bot). Reference ID 12ab" * 2,
         "captcha challenge"),
        ("Access Denied. You don't have permission to access \"http://x.example/events\" on "
         "this server. Reference #18.5c1f2e17", "access denied page"),
        ("Just a moment...", "cloudflare challenge"),
    ])
    def test_other_bot_walls(self, body, want):
        assert blocked_page.reason(body) == want
        assert want in blocked_page.CHALLENGES

    @pytest.mark.parametrize("body", [
        "This form is protected by reCAPTCHA and the Google Privacy Policy applies.",
        "Access denied? Members log in at the top right to see the private calendar.",
        "Our cf-chlorine-free pool reopens in May.",
    ])
    def test_loose_words_on_a_real_page_are_content(self, body):
        assert blocked_page.reason(_fx("normal.md") + "\n\n" + body) is None

    def test_odd_metadata_types_are_ignored(self):
        assert blocked_page.reason(_fx("normal.md"), "404", ["Page Not Found"]) is None

    def test_not_found_heading_deep_in_a_big_page_is_content(self):
        body = (_fx("normal.md") + "\n\nSome text about the venue. " * 300
                + "\n\n# Page not found? Call us\n")
        assert blocked_page.reason(body, 200) is None


class _Resp:
    def __init__(self, status=200, body=None, text=""):
        self.status_code, self._body, self.text = status, body, text
        self.ok = 200 <= status < 300

    def json(self):
        if self._body is None:
            raise ValueError("not JSON")
        return self._body


@pytest.fixture
def firecrawl(monkeypatch):
    """Firecrawl's /v1/scrape answers 200 whatever the target returned; the
    target's status rides in data.metadata.statusCode."""
    monkeypatch.setenv("TOWNLOOPS_FIRECRAWL_URL", "http://firecrawl.invalid:3002")
    pages: dict[str, tuple[str, dict]] = {}

    def post(url, headers=None, json=None, timeout=None):
        md, meta = pages[json["url"]]
        return _Resp(200, {"success": True, "data": {"markdown": md, "metadata": meta}})
    monkeypatch.setattr("engine.event_extract.requests.post", post)
    return pages


class TestFirecrawlListing:
    @pytest.mark.parametrize("name,meta,want", BLOCKED)
    def test_blocked_pages_fail_with_their_reason(self, firecrawl, name, meta, want):
        firecrawl["https://x.example/events"] = (_fx(name), meta)
        assert firecrawl_listing("https://x.example/events") == (False, "", want)

    def test_normal_page_passes_through(self, firecrawl):
        firecrawl["https://x.example/events"] = (_fx("normal.md"), {"statusCode": 200})
        assert firecrawl_listing("https://x.example/events") == (True, _fx("normal.md"), "")

    def test_firecrawl_markdown_is_unchanged_for_its_other_callers(self, firecrawl):
        firecrawl["https://x.example/events"] = (_fx("showare.txt"), {"statusCode": 200})
        assert firecrawl_markdown("https://x.example/events") == (True, "no action", "")


class TestAdapterFetch:
    def _get(self, monkeypatch, resp):
        monkeypatch.setattr("engine.seed_adapters.requests.get", lambda *a, **k: resp)
        with pytest.raises(seed_adapters.AdapterError) as e:
            seed_adapters.fetch_json("https://x.example/wp-json/tribe/events/v1/events", {})
        return str(e.value)

    def test_challenge_status_names_the_challenge(self, monkeypatch):
        assert self._get(monkeypatch, _Resp(403, text=_fx("cloudflare.md"))) == "cloudflare challenge"

    def test_plain_error_status(self, monkeypatch):
        assert self._get(monkeypatch, _Resp(500, text="<h1>Internal Server Error</h1>"
                                                     + " details " * 50)) == "http 500"

    @pytest.mark.parametrize("name,want", [("showare.txt", "showare no action"),
                                           ("incapsula.md", "incapsula challenge"),
                                           ("near-empty.md", "near-empty body")])
    def test_200_that_is_not_json_names_why(self, monkeypatch, name, want):
        assert self._get(monkeypatch, _Resp(200, text=_fx(name))) == want

    def test_200_html_page_is_not_json(self, monkeypatch):
        assert self._get(monkeypatch, _Resp(200, text=_fx("normal.md"))) == "response is not JSON"


# ---------------------------------------------------------------------------
# event-find: a blocked read is a failed run — no LLM, no zero, no vanish miss
# ---------------------------------------------------------------------------

SEED, URL = "Riva Grill", "https://riva.example/events"
SETTINGS = {"window": 4, "min_runs": 3, "drop_pct": 50, "min_baseline": 1}
JAZZ = {"title": "Jazz Night", "date": "2027-03-20T20:00:00", "venue": "Riva Grill"}
OPEN_MIC = {"title": "Open Mic", "date": "2027-03-25T19:00:00", "venue": "Riva Grill"}


@pytest.fixture
def conn(fresh_db):
    fresh_db.row_factory = sqlite3.Row
    fresh_db.isolation_level = None
    fresh_db.execute(
        "INSERT INTO event_seeds (name, url, kind, status, first_seen_at, last_seen_at) "
        "VALUES (?, ?, 'venue', 'approved', 'x', 'x')", (SEED, URL))
    return fresh_db


@pytest.fixture
def find(tmp_state_dir, monkeypatch, firecrawl):
    """event-find over the real Firecrawl listing path (HTTP stubbed); the
    LLM extraction records every call and answers the normal page's events."""
    path = Path(__file__).resolve().parent.parent / "skills/business/event-find/find.py"
    spec = importlib.util.spec_from_file_location("event_find_blocked", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    calls: list[str] = []

    def codex(prompt):
        calls.append(prompt)
        return True, json.dumps(run.answer), ""

    monkeypatch.setattr(mod, "_DB_PATH", tmp_state_dir / "townloops.db")
    monkeypatch.setattr(mod, "_load_town_profile", lambda: {
        "source_health": SETTINGS, "cancellation_miss_threshold": 1})
    monkeypatch.setattr(mod, "_build_extraction_prompt", lambda p, name, url, md: md)
    monkeypatch.setattr(mod, "_codex_exec", codex)
    monkeypatch.setattr(mod, "_telegram", lambda text: None)
    monkeypatch.setattr("engine.inspection._DB_PATH", tmp_state_dir / "townloops.db")

    def run():
        return mod.cmd_find(argparse.Namespace(seed_id=None, limit=None, dry_run=False))
    run.calls, run.pages, run.answer = calls, firecrawl, [JAZZ, OPEN_MIC]
    return run


def _runs(conn):
    return [dict(r) for r in conn.execute("SELECT ok, fetched, error FROM source_runs ORDER BY id")]


def _established(conn, find):
    """Three normal runs — source-health has a baseline — and Jazz Night live."""
    find.pages[URL] = (_fx("normal.md"), {"statusCode": 200, "title": "Events"})
    for _ in range(3):
        assert find() == 0
    jazz = event_uid(SEED, JAZZ["title"], JAZZ["date"])
    conn.execute("UPDATE events SET townloops_slug = 'jazz-night', pushed_at = 'x', "
                 "state = 'shipped', approval_status = 'approved' WHERE event_uid = ?", (jazz,))
    find.calls.clear()


class TestEventFind:
    def test_normal_page_still_extracts(self, conn, find):
        find.pages[URL] = (_fx("normal.md"), {"statusCode": 200, "title": "Events"})
        assert find() == 0
        assert find.calls == [_fx("normal.md")]
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 2
        assert _runs(conn) == [{"ok": 1, "fetched": 2, "error": None}]

    @pytest.mark.parametrize("name,meta,want", BLOCKED)
    def test_blocked_page_is_a_failed_run(self, conn, find, name, meta, want):
        _established(conn, find)
        find.pages[URL] = (_fx(name), meta)
        assert find() == 0
        assert find.calls == []                               # never reached the LLM
        assert _runs(conn)[-1] == {"ok": 0, "fetched": 0, "error": want}
        assert sh.flagged(conn) == []                         # not a zero, not a drop
        rows = conn.execute("SELECT missed_finds, cancel_status FROM events").fetchall()
        assert [tuple(r) for r in rows] == [(0, None), (0, None)]   # not a vanish miss

    def test_failed_runs_leave_the_baseline_a_real_zero_is_judged_against(self, conn, find):
        # Blocked runs between the history and a real explicit zero neither
        # flag the seed nor move the average the zero is judged against.
        _established(conn, find)                              # 2, 2, 2 events
        for name, meta, _ in BLOCKED:
            find.pages[URL] = (_fx(name), meta)
            find()
        assert sh.flagged(conn) == []
        find.pages[URL] = (_fx("quiet.md"), {"statusCode": 200, "title": "Events"})
        find.answer = []
        find()
        assert find.calls == [_fx("quiet.md")]               # a small real page reaches the LLM
        assert _runs(conn)[-1] == {"ok": 1, "fetched": 0, "error": None}
        [seed] = sh.flagged(conn)
        assert (seed["health_status"], seed["health_baseline"]) == ("zero", 2.0)
