"""engine/discovery.py — kernel functions.

All Firecrawl network I/O is monkey-patched — these tests never actually
hit the wire.
"""

from __future__ import annotations

import pytest


class TestCanonicalizeUrl:
    def test_strips_tracking_params(self):
        from engine import discovery
        u = discovery.canonicalize_url("https://Example.com/foo?utm_source=x&keep=1&fbclid=y")
        assert u == "https://example.com/foo?keep=1"

    def test_strips_www(self):
        from engine import discovery
        assert discovery.canonicalize_url("http://www.example.com/") == "http://example.com/"

    def test_adds_scheme(self):
        from engine import discovery
        assert discovery.canonicalize_url("example.com/foo") == "https://example.com/foo"

    def test_preserves_root_slash(self):
        from engine import discovery
        assert discovery.canonicalize_url("https://example.com/") == "https://example.com/"


class TestHostBlocked:
    def test_yelp_blocked(self):
        from engine import discovery
        assert discovery.host_blocked("https://www.yelp.com/biz/foo")

    def test_gov_blocked_by_default(self):
        from engine import discovery
        assert discovery.host_blocked("https://cityof.gov")

    def test_gov_allowed_when_opted_in(self):
        from engine import discovery
        assert not discovery.host_blocked("https://cityof.gov", allow_gov=True)

    def test_clean_host_ok(self):
        from engine import discovery
        assert not discovery.host_blocked("https://cafe.example.com")


class TestDirectoryLinkBlockReason:
    def test_same_host_as_seed(self):
        from engine import discovery
        assert discovery.directory_link_block_reason(
            "visitfoo.com", "https://visitfoo.com/other-page"
        ) == "same host as seed"

    def test_file_extension(self):
        from engine import discovery
        r = discovery.directory_link_block_reason(
            "seed.com", "https://otherbiz.com/brochure.pdf"
        )
        assert r and "file" in r

    def test_generic_section(self):
        from engine import discovery
        r = discovery.directory_link_block_reason(
            "seed.com", "https://otherbiz.com/dining/things-to-do"
        )
        assert r and "generic" in r

    def test_clean_link_none(self):
        from engine import discovery
        assert discovery.directory_link_block_reason(
            "seed.com", "https://realbiz.com/menu"
        ) is None


class TestVerifyCandidate:
    def _mock_scrape(self, monkeypatch, md, title=""):
        from engine import discovery
        monkeypatch.setattr(
            discovery, "firecrawl_scrape",
            lambda url, *, raise_on_error=False: {"markdown": md, "links": [], "title": title},
        )

    def test_scrape_failure(self, monkeypatch):
        from engine import discovery
        monkeypatch.setattr(discovery, "firecrawl_scrape",
                            lambda url, *, raise_on_error=False: {})
        v = discovery.verify_candidate("https://x.co", town_name="Town")
        assert not v["verified"]
        assert any("unreachable" in r for r in v["reasons"])

    def test_empty_markdown(self, monkeypatch):
        from engine import discovery
        self._mock_scrape(monkeypatch, "")
        v = discovery.verify_candidate("https://x.co", town_name="Town")
        assert not v["verified"]

    def test_town_missing(self, monkeypatch):
        from engine import discovery
        self._mock_scrape(monkeypatch, "restaurant in New York")
        v = discovery.verify_candidate("https://x.co", town_name="South Lake Tahoe")
        assert not v["verified"]
        assert any("no mention" in r for r in v["reasons"])

    def test_closure_marker(self, monkeypatch):
        from engine import discovery
        self._mock_scrape(monkeypatch,
                          "South Lake Tahoe cafe. Permanently closed as of 2025. Menu.")
        v = discovery.verify_candidate("https://x.co", town_name="South Lake Tahoe")
        assert not v["verified"]
        assert any("closure" in r for r in v["reasons"])

    def test_no_customer_facing(self, monkeypatch):
        from engine import discovery
        self._mock_scrape(monkeypatch, "South Lake Tahoe holdings corp. About the company.")
        v = discovery.verify_candidate("https://x.co", town_name="South Lake Tahoe")
        assert not v["verified"]

    def test_happy_path_extracts_signals(self, monkeypatch):
        from engine import discovery
        self._mock_scrape(monkeypatch,
            "Welcome to Blue Angel Cafe in South Lake Tahoe! Menu, book online. "
            "Address: 1132 Ski Run Blvd. Phone: (530) 555-1212. "
            "Hours: Mon-Fri 8am-4pm.")
        v = discovery.verify_candidate("https://x.co", town_name="South Lake Tahoe")
        assert v["verified"], v
        assert v["address"] and "Ski Run" in v["address"]
        assert v["phone"] and "555" in v["phone"]
        assert v["hours"]


class TestSweepFunctions:
    def test_sweep_web_search_dedupes(self, monkeypatch):
        from engine import discovery
        # Return the same URL every time — should collapse to one candidate.
        monkeypatch.setattr(
            discovery, "firecrawl_search",
            lambda q, *, limit=10, raise_on_error=False: [
                {"url": "https://only-one.com/", "title": "Only One", "description": ""}
            ],
        )
        # Also skip the QPS bucket wait.
        monkeypatch.setattr(discovery.firecrawl_bucket, "wait", lambda **_: None)
        out = discovery.sweep_web_search("Town", "CA", ["restaurants in Town", "cafes in Town"])
        assert len(out) == 1
        assert out[0].canonical_url == "https://only-one.com/"

    def test_sweep_directories_filters_blocked(self, monkeypatch):
        from engine import discovery
        monkeypatch.setattr(
            discovery, "firecrawl_scrape",
            lambda url, *, raise_on_error=False: {
                "markdown": "seed page",
                "links": [
                    "https://visitfoo.com/self-link",  # same-host: blocked
                    "https://real-biz.com/menu",       # ok
                    "https://otherbiz.com/asset.pdf",  # file ext: blocked
                    "not-a-url",                        # blocked
                ],
                "title": "",
            },
        )
        monkeypatch.setattr(discovery.firecrawl_bucket, "wait", lambda **_: None)
        out = discovery.sweep_directories(["https://visitfoo.com/list"])
        assert len(out) == 1
        assert out[0].canonical_url == "https://real-biz.com/menu"

    def test_sweep_facebook_filters_system_paths(self, monkeypatch):
        from engine import discovery
        monkeypatch.setattr(
            discovery, "firecrawl_search",
            lambda q, *, limit=10, raise_on_error=False: [
                {"url": "https://facebook.com/BizPage", "title": "BizPage | Facebook", "description": ""},
                {"url": "https://facebook.com/events/12345", "title": "Some Event", "description": ""},
                {"url": "https://other.com/x", "title": "Not FB", "description": ""},
            ],
        )
        monkeypatch.setattr(discovery.firecrawl_bucket, "wait", lambda **_: None)
        out = discovery.sweep_facebook("Town", ["cafe"])
        # BizPage kept; /events/ filtered; non-FB URL filtered.
        assert len(out) == 1
        assert out[0].canonical_url == "https://facebook.com/BizPage"
