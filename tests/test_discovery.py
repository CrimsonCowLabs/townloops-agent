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

    @pytest.mark.parametrize("link", [
        "https://otherbiz.com/visitor-guide", "https://otherbiz.com/visitors-guide/2024",
        "https://otherbiz.com/travel-guide", "https://otherbiz.com/blog/best-hikes",
        "https://otherbiz.com/articles/tahoe",
    ])
    def test_an_editorial_page_is_dropped(self, link):
        """One editorial-path rule (own_site.ARTICLE_PATH) for the link filter
        and for verify."""
        from engine import discovery
        r = discovery.directory_link_block_reason("seed.com", link)
        assert r and "article" in r, r


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
        ("https://foxintheforest.net/south-lake-tahoe-hikes",
         "South Lake Tahoe Hikes | Fox in the Forest"),
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

    @pytest.mark.parametrize("kind", KINDS)
    @pytest.mark.parametrize("path", ["/member-application", "/jobs", "/hotdeals", "/hot-deals",
                                      "/careers", "/list", "/members/list", "/events/search",
                                      "/calendar"])
    def test_a_site_utility_page_rejects_even_named_after_its_host(self, monkeypatch, path, kind):
        from engine import discovery
        scraped = self._scrape(monkeypatch)
        v = discovery.verify_candidate(f"https://business.tahoechamber.org{path}",
                                       town_name="South Lake Tahoe", kind=kind, name="Tahoe Chamber")
        assert not v["verified"]
        assert any("not its own site" in r for r in v["reasons"]), v["reasons"]
        assert scraped == []

    @pytest.mark.parametrize("kind", ["community", "nonprofit"])
    def test_a_government_department_page_is_the_governments_own_site(self, monkeypatch, kind):
        from engine import discovery
        self._scrape(monkeypatch)
        v = discovery.verify_candidate("https://cityofslt.gov/parks-recreation",
                                       town_name="South Lake Tahoe", kind=kind,
                                       name="Parks & Recreation")
        assert v["verified"], v["reasons"]

    def test_a_deep_government_page_is_no_business_own_site(self, monkeypatch):
        from engine import discovery
        self._scrape(monkeypatch)
        v = discovery.verify_candidate("https://ca.gov/best-restaurants-south-lake-tahoe",
                                       town_name="South Lake Tahoe", name="Best Restaurants")
        assert not v["verified"]
        assert any("not its own site" in r for r in v["reasons"]), v["reasons"]


class TestRoundupReason:
    """An article or roundup *about* organisations — on any host (#17)."""

    @pytest.mark.parametrize("url, title, signal", [
        ("https://laketahoethisweek.com/blog/tahoes-art-scene", None, "/blog/"),
        ("https://insidetheupgrade.com/post/south-lake-tahoe-restaurants", None, "/post/"),
        ("https://townandtourist.com/articles/things-to-do", None, "/articles/"),
        ("https://example.com/article-12345", None, "/article"),
        ("https://tahoe.com/south-lake-tahoe/restaurants",
         "All 196+ South Lake Tahoe Restaurants", "opens with a count"),
        ("https://example.com/x", "The 25 Best Hikes", "opens with a count"),
        ("https://example.com/x", "12 Galleries Worth a Visit", "opens with a count"),
        ("https://example.com/x", "Best Restaurants and Bars in South Lake Tahoe", "'best'"),
        ("https://example.com/x", "Top Restaurants in Tahoe", "'top'"),
        ("https://example.com/x", "Things To Do in South Lake Tahoe", "'things to do'"),
        ("https://example.com/x", "South Lake Tahoe Bucket List", "'bucket list'"),
        ("https://example.com/x", "Places to Visit Near Tahoe", "'places to visit'"),
        ("https://example.com/x", "A Local’s Guide to South Lake Tahoe", "guide"),
        ("https://example.com/x", "Your Guide to Tahoe Galleries", "guide"),
        ("https://example.com/x", "Can't-Miss Spots in Tahoe", "can't-miss"),
        ("https://example.com/x", "15 Things To Do in Tahoe", "opens with a count"),
        ("https://example.com/x", "The Ultimate Guide to Tahoe", "guide"),
        ("https://example.com/x", "Guide to the Best Tahoe Beaches", "guide"),
        ("https://example.com/x", "Best Hikes", "'best'"),
        ("https://example.com/x", "Best Pizza in Tahoe", "'best'"),
        # A homepage's title is its name for a phrase — not for a count.
        ("https://some-blog.com/", "The 10 Best Things To Do in Tahoe", "opens with a count"),
        # Roundup words in the URL's own slug, whatever the title says.
        ("https://visitlaketahoe.com/food-and-wine/best-restaurants-and-bars-in-south-lake-tahoe",
         None, "slug"),
        ("https://visitlaketahoe.com/food-and-wine/best-restaurants-and-bars-in-south-lake-tahoe",
         "Food and Wine | Visit Lake Tahoe", "slug"),
        ("https://example.com/top-10-tahoe-restaurants", None, "slug"),
        ("https://example.com/24-best-south-lake-tahoe-restaurants", None, "slug"),
        ("https://example.com/things-to-do-in-tahoe", None, "slug"),
        ("https://example.com/tahoe-bucket-list", None, "slug"),
        ("https://example.com/places-to-visit-near-tahoe", None, "slug"),
    ])
    def test_a_signal_fires_and_is_named(self, url, title, signal):
        from engine import discovery
        why = discovery.roundup_reason(url, title)
        assert why and signal.lower() in why.lower(), why
        assert why.startswith("an article or roundup: "), why

    @pytest.mark.parametrize("url, title", [
        ("https://example.com/x", "Tahoe's Art Scene: Tour Local Galleries and…"),
        ("https://example.com/x", "Tahoe's Art Scene: Tour Local Galleries and ..."),
        # A homepage found by a cut-short search-result title.
        ("https://insidetheupgrade.com/", "Inside the Upgrade | South Lake Tahoe Restaur…"),
        ("https://insidetheupgrade.com/", "Inside the Upgrade…"),
    ])
    def test_a_truncated_search_result_title_fires(self, url, title):
        from engine import discovery
        why = discovery.roundup_reason(url, title, from_search=True)
        assert why and "truncated" in why, why

    def test_a_pages_own_title_ending_in_dots_is_not_truncation(self):
        """Only a search engine cuts a title short; a site may end its own
        <title> with "..."."""
        from engine import discovery
        assert discovery.roundup_reason("https://example.com/x", "Coming Soon...") is None

    @pytest.mark.parametrize("title", [
        "3 Sisters Bakery", "1 Tahoe Place", "7 Eleven",
        "Best Western Station House Inn", "Top Notch Plumbing", "Tahoe's Best Pizza",
        "Mike's Guide Service", "Tahoe Guide to Fishing Charters",
    ])
    def test_a_name_shaped_title_on_a_deep_page_carries_no_signal(self, title):
        """A count is a roundup only before a roundup word or a plural; 'best'
        and 'top' only with a plural or a place ("in Tahoe"); 'guide' only as
        someone's guide or a guide to somewhere."""
        from engine import discovery
        assert discovery.roundup_reason("https://example.com/menu", title) is None

    @pytest.mark.parametrize("url", [
        "https://tahoetoyshop.com/toys",
        "https://topofthetram.com/top-of-the-tram-tours",
        "https://tahoeoutdooradventures.com/kayak-tours",
        "https://example.com/classes",
    ])
    def test_an_ordinary_slug_carries_no_signal(self, url):
        from engine import discovery
        assert discovery.roundup_reason(url, None) is None

    @pytest.mark.parametrize("url, title", [
        # A homepage: the name is the organisation's, whatever words it carries.
        ("https://thebestoftahoe.com/", "The Best of Tahoe"),
        ("https://topofthetram.com/", "Top of the Tram"),
        ("https://tahoeflyfishing.com/", "Tahoe Fly Fishing Guide Service"),
        ("https://insidetheupgrade.com/", "Inside the Upgrade"),
        ("https://thebestoftahoe.com/", "The Best of Tahoe…"),  # a page's own title: not truncation
        # A deep page whose title is the name its own host carries.
        ("https://thebestoftahoe.com/about", "About Us | The Best of Tahoe"),
        ("https://tahoeflyfishingguideservice.com/trips", "Tahoe Fly Fishing Guide Service"),
        ("https://tahoeoutdooradventures.com/kayak-tours", "Tahoe Outdoor Adventures | Kayak Tours"),
        ("https://oncoursetahoe.com/", None),
        ("https://example.com/menu", "Menu | Blue Angel Cafe"),
    ])
    def test_an_organisations_own_page_carries_no_signal(self, url, title):
        from engine import discovery
        assert discovery.roundup_reason(url, title) is None


class TestVerifyRoundup:
    """An editorial page about organisations is rejected even where its host
    passes the own-site check (#17) — SLT's queue held visitlaketahoe.com and
    tahoe.com roundups that sit on the town's own allowed hosts."""

    PAGE = TestVerifyOwnSite.PAGE
    KINDS = TestVerifyOwnSite.KINDS

    def _scrape(self, monkeypatch, title=""):
        return TestVerifyOwnSite._scrape(self, monkeypatch, title=title)

    # (url, a name its host carries, the page's own title)
    ROUNDUPS = [
        ("https://visitlaketahoe.com/food-and-wine/best-restaurants-and-bars-in-south-lake-tahoe",
         "Visit Lake Tahoe", "Best Restaurants and Bars in South Lake Tahoe | Visit Lake Tahoe"),
        ("https://tahoe.com/south-lake-tahoe/restaurants",
         "Tahoe.com", "All 196+ South Lake Tahoe Restaurants | Tahoe.com"),
        ("https://thebestoftahoe.com/blog/post/the-5-best-art-galleries-around-lake-tahoe-region",
         "The Best of Tahoe", "The 5 Best Art Galleries Around Lake Tahoe Region - The Best of Tahoe"),
        ("https://laketahoethisweek.com/blog/tahoes-art-scene-tour-local-galleries-and-studios",
         "Lake Tahoe This Week", "Tahoe's Art Scene: Tour Local Galleries and Studios"),
        ("https://townandtourist.com/articles/things-to-do-in-south-lake-tahoe",
         "Town and Tourist", "25 Fun Things To Do in South Lake Tahoe - Town and Tourist"),
        ("https://insidetheupgrade.com/post/south-lake-tahoe-restaurants",
         "Inside the Upgrade", "Inside the Upgrade | 24 BEST South Lake Tahoe Restaurants…"),
    ]

    @pytest.mark.parametrize("kind", KINDS)
    @pytest.mark.parametrize("url, host_name, title", ROUNDUPS)
    def test_a_roundup_rejects_by_its_page_title(self, monkeypatch, url, host_name, title, kind):
        """Named after its host — the own-site check passes — the page's own
        title (or its path) still gives it away."""
        from engine import discovery
        self._scrape(monkeypatch, title=title)
        v = discovery.verify_candidate(url, town_name="South Lake Tahoe", kind=kind, name=host_name)
        assert not v["verified"]
        assert any("article or roundup" in r for r in v["reasons"]), v["reasons"]

    @pytest.mark.parametrize("kind", KINDS)
    @pytest.mark.parametrize("url, host_name, title", ROUNDUPS)
    def test_a_nameless_roundup_rejects(self, monkeypatch, url, host_name, title, kind):
        from engine import discovery
        self._scrape(monkeypatch, title=title)
        v = discovery.verify_candidate(url, town_name="South Lake Tahoe", kind=kind)
        assert not v["verified"]
        assert any("article or roundup" in r for r in v["reasons"]), v["reasons"]

    @pytest.mark.parametrize("kind", KINDS)
    @pytest.mark.parametrize("url, serp_name", [
        ("https://tahoe.com/south-lake-tahoe/restaurants", "All 196+ South Lake Tahoe Restaurants"),
        ("https://visitlaketahoe.com/food-and-wine/best-restaurants-and-bars-in-south-lake-tahoe",
         "Best Restaurants and Bars in South Lake Tahoe"),
        ("https://laketahoethisweek.com/blog/tahoes-art-scene-tour-local-galleries-and-studios",
         "Tahoe's Art Scene: Tour Local Galleries and…"),
    ])
    def test_a_roundup_search_title_rejects_unscraped(self, monkeypatch, url, serp_name, kind):
        from engine import discovery
        scraped = self._scrape(monkeypatch)
        v = discovery.verify_candidate(url, town_name="South Lake Tahoe", kind=kind, name=serp_name)
        assert not v["verified"]
        assert any("article or roundup" in r for r in v["reasons"]), v["reasons"]
        assert scraped == []

    @pytest.mark.parametrize("kind", KINDS)
    def test_a_homepage_with_a_truncated_search_title_rejects_unscraped(self, monkeypatch, kind):
        from engine import discovery
        scraped = self._scrape(monkeypatch)
        v = discovery.verify_candidate("https://insidetheupgrade.com/", town_name="South Lake Tahoe",
                                       kind=kind, name="Inside the Upgrade | South Lake Tahoe Restaur…")
        assert not v["verified"]
        assert any("truncated" in r for r in v["reasons"]), v["reasons"]
        assert scraped == []

    @pytest.mark.parametrize("kind", KINDS)
    def test_a_pages_own_title_ending_in_dots_still_verifies(self, monkeypatch, kind):
        from engine import discovery
        self._scrape(monkeypatch, title="Pacific Crest Gallery | Art of the Sierra...")
        v = discovery.verify_candidate("https://pacificcrestgallerytahoe.com/",
                                       town_name="South Lake Tahoe", kind=kind)
        assert v["verified"], v["reasons"]

    @pytest.mark.parametrize("kind", KINDS)
    @pytest.mark.parametrize("url, name, title", [
        ("https://thebestoftahoe.com/", "The Best of Tahoe", "The Best of Tahoe | Home"),
        ("https://thebestoftahoe.com/", None, "The Best of Tahoe | Home"),
        ("https://tahoeflyfishing.com/", "Tahoe Fly Fishing Guide Service",
         "Tahoe Fly Fishing Guide Service"),
        ("https://topofthetram.com/", "Top of the Tram", "Top of the Tram - Heavenly"),
        ("https://tahoeoutdooradventures.com/", "Tahoe Outdoor Adventures", "Home | Somewhere"),
        ("https://oncoursetahoe.com/", "OnCourse Tahoe", "OnCourse Tahoe"),
        ("https://pacificcrestgallerytahoe.com/", "Pacific Crest Gallery", "Pacific Crest Gallery"),
    ])
    def test_a_homepage_named_with_best_or_top_still_verifies(self, monkeypatch, url, name, title, kind):
        from engine import discovery
        self._scrape(monkeypatch, title=title)
        v = discovery.verify_candidate(url, town_name="South Lake Tahoe", kind=kind, name=name)
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
