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

    def test_listing_hosts_blocked(self):
        from engine import discovery
        assert discovery.host_blocked(
            "https://www.restaurantji.com/ca/south-lake-tahoe/cascade-kitchens-food-hall-and-bar-/")

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

    def test_verdict_carries_the_page_title(self, monkeypatch):
        from engine import discovery
        self._mock_scrape(monkeypatch, "Bakery in Town. Menu.", title="Home | Town Bakery")
        assert discovery.verify_candidate("https://x.co", town_name="Town")["title"] == "Home | Town Bakery"
        self._mock_scrape(monkeypatch, "Bakery in Elsewhere.", title="Elsewhere Bakery")
        assert discovery.verify_candidate("https://x.co", town_name="Town")["title"] == "Elsewhere Bakery"


class TestVerifyOwnSite:
    """A page about an organisation — a travel blog's listicle, a city
    directory — is not its own site, however well it names the town (#16:
    25 listicles reached SLT's business queue this way)."""

    # Passes the town, closure and customer/member-facing checks for every kind.
    PAGE = "Eat and play in South Lake Tahoe. Contact us. About us. Join. Menu."
    KINDS = ("business", "community", "nonprofit")

    def _scrape(self, monkeypatch, title=""):
        from engine import discovery
        scraped = []

        def scrape(url, *, raise_on_error=False):
            scraped.append(url)
            return {"markdown": self.PAGE, "links": [], "title": title}

        monkeypatch.setattr(discovery, "firecrawl_scrape", scrape)
        return scraped

    @pytest.mark.parametrize("kind", KINDS)
    @pytest.mark.parametrize("url, name", [
        ("https://insidetheupgrade.com/post/south-lake-tahoe-restaurants",
         "The 24 BEST South Lake Tahoe Restaurants"),
        ("https://foxintheforest.net/best-south-lake-tahoe-hikes", "15 Best South Lake Tahoe Hikes"),
        ("https://gowanderly.com/south-lake-tahoe-bucket-list", "South Lake Tahoe Bucket List"),
        ("https://sf.eater.com/maps/best-restaurants-bars-beach-south-lake-tahoe",
         "The Best Restaurants in South Lake Tahoe"),
        ("https://localgovs.com/local-california/south-lake-tahoe", "South Lake Tahoe"),
        ("https://citydirectory.us/city-south-lake-tahoe.html", "South Lake Tahoe City Directory"),
        ("https://business.tahoechamber.org/member-application", "Member Application"),
        ("https://business.tahoechamber.org/jobs", "Jobs"),
        ("https://business.tahoechamber.org/hotdeals", "Hot Deals"),
    ])
    def test_a_page_about_organisations_rejects(self, monkeypatch, url, name, kind):
        from engine import discovery
        scraped = self._scrape(monkeypatch)
        v = discovery.verify_candidate(url, town_name="South Lake Tahoe", kind=kind, name=name)
        assert not v["verified"]
        assert any("not its own site" in r for r in v["reasons"]), v["reasons"]
        assert scraped == []          # decided from the URL — no Firecrawl spent

    @pytest.mark.parametrize("kind", KINDS)
    @pytest.mark.parametrize("url, title", [
        ("https://business.tahoechamber.org/member-application",
         "Member Application | Lake Tahoe South Shore Chamber of Commerce"),
        ("https://business.tahoechamber.org/jobs", "Job Board - Lake Tahoe South Shore Chamber"),
        ("https://business.tahoechamber.org/hotdeals", "Hot Deals | Tahoe Chamber"),
        ("https://insidetheupgrade.com/post/south-lake-tahoe-restaurants",
         "The 24 BEST South Lake Tahoe Restaurants | Inside the Upgrade"),
    ])
    def test_a_nameless_candidate_is_judged_by_its_page_title_name(self, monkeypatch, url, title, kind):
        from engine import discovery
        self._scrape(monkeypatch, title=title)
        v = discovery.verify_candidate(url, town_name="South Lake Tahoe", kind=kind)
        assert not v["verified"]
        assert any("not its own site" in r for r in v["reasons"]), v["reasons"]

    @pytest.mark.parametrize("kind", KINDS)
    @pytest.mark.parametrize("url, name", [
        ("https://tahoeoutdooradventures.com/", "Tahoe Outdoor Adventures"),
        ("https://oncoursetahoe.com/", "OnCourse Tahoe"),
        ("https://pacificcrestgallerytahoe.com/", "Pacific Crest Gallery"),
        ("https://pacificcrestgallerytahoe.com/", None),
        ("https://tahoeoutdooradventures.com/kayak-tours", "Tahoe Outdoor Adventures"),
        ("https://www.facebook.com/tahoeyogacollective", "Tahoe Yoga Collective"),
    ])
    def test_an_organisations_own_site_still_verifies(self, monkeypatch, url, name, kind):
        from engine import discovery
        self._scrape(monkeypatch, title="Home | Somewhere")
        v = discovery.verify_candidate(url, town_name="South Lake Tahoe", kind=kind, name=name)
        assert v["verified"], v["reasons"]

    def test_a_nameless_deep_page_on_its_own_host_verifies_by_title(self, monkeypatch):
        from engine import discovery
        self._scrape(monkeypatch, title="Tahoe Outdoor Adventures | Kayak Tours")
        v = discovery.verify_candidate("https://tahoeoutdooradventures.com/kayak-tours",
                                       town_name="South Lake Tahoe")
        assert v["verified"], v["reasons"]

    def test_a_government_department_page_is_the_governments_own_site(self, monkeypatch):
        from engine import discovery
        self._scrape(monkeypatch)
        v = discovery.verify_candidate("https://cityofslt.gov/parks-recreation",
                                       town_name="South Lake Tahoe", kind="community",
                                       name="Parks & Recreation")
        assert v["verified"], v["reasons"]


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
