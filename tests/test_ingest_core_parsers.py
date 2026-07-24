"""Parser tests — pure functions over HTML/JSON-LD/iCal fixtures.

All parsers here operate on pre-fetched strings, so we don't need HTTP mocks —
just realistic input snippets and a `source` dict shaped like sources.yaml.
"""

from __future__ import annotations

import pytest


# =====================================================================
# _walk_jsonld
# =====================================================================


class TestWalkJsonld:
    def test_flat_dict(self):
        from engine.ingest_core import _walk_jsonld
        assert list(_walk_jsonld({"@type": "X", "name": "foo"})) == [
            {"@type": "X", "name": "foo"}
        ]

    def test_top_level_list(self):
        from engine.ingest_core import _walk_jsonld
        blob = [{"@type": "A"}, {"@type": "B"}]
        items = list(_walk_jsonld(blob))
        # Order is preserved.
        assert items[0]["@type"] == "A"
        assert items[1]["@type"] == "B"

    def test_graph_traversed(self):
        from engine.ingest_core import _walk_jsonld
        blob = {"@type": "Root", "@graph": [{"@type": "A"}, {"@type": "B"}]}
        types = [item.get("@type") for item in _walk_jsonld(blob)]
        assert "Root" in types
        assert "A" in types
        assert "B" in types

    def test_nested_dict_values_traversed(self):
        from engine.ingest_core import _walk_jsonld
        blob = {"@type": "Outer", "inner": {"@type": "Inner"}}
        types = [item.get("@type") for item in _walk_jsonld(blob)]
        assert "Outer" in types
        assert "Inner" in types


# =====================================================================
# _jsonld_to_event
# =====================================================================


class TestJsonldToEvent:
    @pytest.fixture
    def source(self):
        return {
            "name": "test-src",
            "hosting_group": "Test Host",
            "default_location": "South Lake Tahoe, CA",
            "default_category_slug": "live-music",
        }

    def test_minimal_event(self, source):
        from engine.ingest_core import _jsonld_to_event
        item = {
            "@type": "Event",
            "name": "Jazz Night",
            "startDate": "2026-07-15T20:00:00-07:00",
        }
        ev = _jsonld_to_event(item, source, "https://example.com/list")
        assert ev is not None
        assert ev.name == "Jazz Night"
        assert ev.category_slug == "live-music"
        # No location on the item → falls back to source's default_location.
        assert "South Lake Tahoe" in ev.location

    def test_missing_name_returns_none(self, source):
        from engine.ingest_core import _jsonld_to_event
        assert _jsonld_to_event(
            {"@type": "Event", "startDate": "2026-07-15T20:00:00"},
            source, "https://x",
        ) is None

    def test_missing_start_returns_none(self, source):
        from engine.ingest_core import _jsonld_to_event
        assert _jsonld_to_event(
            {"@type": "Event", "name": "Jazz Night"},
            source, "https://x",
        ) is None

    def test_dict_location(self, source):
        from engine.ingest_core import _jsonld_to_event
        item = {
            "@type": "Event",
            "name": "Jazz Night",
            "startDate": "2026-07-15",
            "location": {
                "name": "Riva Grill",
                "address": {
                    "streetAddress": "3411 Lake Tahoe Blvd",
                    "addressLocality": "South Lake Tahoe",
                    "addressRegion": "CA",
                },
            },
        }
        ev = _jsonld_to_event(item, source, "https://x")
        assert ev.venue_name == "Riva Grill"
        assert "3411 Lake Tahoe Blvd" in ev.location
        assert "South Lake Tahoe" in ev.location
        assert "CA" in ev.location

    def test_list_location(self, source):
        from engine.ingest_core import _jsonld_to_event
        item = {
            "@type": "Event",
            "name": "X",
            "startDate": "2026-07-15",
            "location": [{"name": "First Venue"}, {"name": "Ignored"}],
        }
        ev = _jsonld_to_event(item, source, "https://x")
        assert ev.venue_name == "First Venue"

    def test_str_location(self, source):
        from engine.ingest_core import _jsonld_to_event
        item = {
            "@type": "Event",
            "name": "X",
            "startDate": "2026-07-15",
            "location": "Some Place, SLT",
        }
        ev = _jsonld_to_event(item, source, "https://x")
        assert ev.location == "Some Place, SLT"

    def test_organizer_dict(self, source):
        from engine.ingest_core import _jsonld_to_event
        item = {
            "@type": "Event", "name": "X", "startDate": "2026-07-15",
            "organizer": {
                "@type": "Organization",
                "name": "Tahoe Music Society",
                "url": "https://tahoemusic.example",
            },
        }
        ev = _jsonld_to_event(item, source, "https://x")
        assert ev.hosting_group == "Tahoe Music Society"
        assert "organizer" in ev.mining_extras
        assert ev.mining_extras["organizer"]["name"] == "Tahoe Music Society"
        assert ev.mining_extras["organizer"]["url"] == "https://tahoemusic.example"

    def test_organizer_fallback_to_source_hosting_group(self, source):
        from engine.ingest_core import _jsonld_to_event
        item = {"@type": "Event", "name": "X", "startDate": "2026-07-15"}
        ev = _jsonld_to_event(item, source, "https://x")
        assert ev.hosting_group == "Test Host"

    def test_performers_extracted_to_mining_extras(self, source):
        from engine.ingest_core import _jsonld_to_event
        item = {
            "@type": "Event", "name": "X", "startDate": "2026-07-15",
            "performer": [
                {"@type": "MusicGroup", "name": "Band A", "sameAs": "https://sameas"},
                {"@type": "Person", "name": "DJ Soloist", "url": "https://dj"},
            ],
        }
        ev = _jsonld_to_event(item, source, "https://x")
        assert "performers" in ev.mining_extras
        assert len(ev.mining_extras["performers"]) == 2
        assert ev.mining_extras["performers"][0]["name"] == "Band A"
        # sameAs falls through to url when url is absent
        assert ev.mining_extras["performers"][0]["url"] == "https://sameas"

    def test_creators_extracted_to_mining_extras(self, source):
        from engine.ingest_core import _jsonld_to_event
        item = {
            "@type": "Event", "name": "X", "startDate": "2026-07-15",
            "creator": {"@type": "Person", "name": "Alice"},
        }
        ev = _jsonld_to_event(item, source, "https://x")
        assert "creators" in ev.mining_extras
        assert ev.mining_extras["creators"][0]["name"] == "Alice"

    def test_venue_from_description_when_generic(self, source):
        """When location.name is the town (as often happens with aggregators)
        the venue is mined from the description text via the 'at X in <town>' regex."""
        from engine.ingest_core import _jsonld_to_event
        item = {
            "@type": "Event", "name": "Jazz", "startDate": "2026-07-15",
            "location": {"name": "South Lake Tahoe"},
            "description": "Live music at Riva Grill in South Lake Tahoe.",
        }
        ev = _jsonld_to_event(item, source, "https://x")
        assert ev.venue_name == "Riva Grill"

    def test_url_overrides_page_url(self, source):
        from engine.ingest_core import _jsonld_to_event
        item = {
            "@type": "Event", "name": "X", "startDate": "2026-07-15",
            "url": "https://direct-event-url.example/e",
        }
        ev = _jsonld_to_event(item, source, "https://listing-page")
        assert ev.source_url == "https://direct-event-url.example/e"

    def test_source_url_falls_back_to_page_url(self, source):
        from engine.ingest_core import _jsonld_to_event
        item = {"@type": "Event", "name": "X", "startDate": "2026-07-15"}
        ev = _jsonld_to_event(item, source, "https://listing")
        assert ev.source_url == "https://listing"


# =====================================================================
# _extract_jsonld_from_html
# =====================================================================


class TestExtractJsonldFromHtml:
    def test_wraps_walker_and_filters_to_event_type(self):
        from engine.ingest_core import _extract_jsonld_from_html
        html = """
        <script type="application/ld+json">
        {"@type": "Organization", "name": "Not an event"}
        </script>
        <script type="application/ld+json">
        {"@type": "Event", "name": "Yes", "startDate": "2026-07-15"}
        </script>
        """
        source = {"name": "t", "hosting_group": "h", "default_location": ""}
        events = _extract_jsonld_from_html(html, source, "https://x")
        assert len(events) == 1
        assert events[0].name == "Yes"

    def test_graph_of_events(self):
        from engine.ingest_core import _extract_jsonld_from_html
        html = """
        <script type="application/ld+json">
        {"@graph": [
            {"@type": "Event", "name": "A", "startDate": "2026-07-15"},
            {"@type": "Event", "name": "B", "startDate": "2026-07-16"}
        ]}
        </script>
        """
        source = {"name": "t", "hosting_group": "h", "default_location": ""}
        events = _extract_jsonld_from_html(html, source, "https://x")
        names = sorted(e.name for e in events)
        assert names == ["A", "B"]

    def test_invalid_json_is_skipped(self):
        from engine.ingest_core import _extract_jsonld_from_html
        html = """
        <script type="application/ld+json">
        NOT VALID JSON
        </script>
        <script type="application/ld+json">
        {"@type": "Event", "name": "Ok", "startDate": "2026-07-15"}
        </script>
        """
        source = {"name": "t", "hosting_group": "h", "default_location": ""}
        events = _extract_jsonld_from_html(html, source, "https://x")
        # First block is ignored, second one produces an event.
        assert len(events) == 1

    def test_type_as_list(self):
        """Some pages have @type: ['Event', 'MusicEvent'] as an array."""
        from engine.ingest_core import _extract_jsonld_from_html
        html = """
        <script type="application/ld+json">
        {"@type": ["Event", "MusicEvent"], "name": "X", "startDate": "2026-07-15"}
        </script>
        """
        source = {"name": "t", "hosting_group": "h", "default_location": ""}
        assert len(_extract_jsonld_from_html(html, source, "https://x")) == 1


# =====================================================================
# CSS-selector extraction
# =====================================================================


class TestExtractSelectorsFromHtml:
    @pytest.fixture
    def source(self):
        return {
            "name": "sel-src",
            "hosting_group": "Sel Host",
            "default_location": "SLT",
            "default_category_slug": "arts",
            "selectors": {
                "container": ".ev",
                "name": "h3",
                "date": "time",
                "date_attr": "datetime",
                "location": ".loc",
                "description": ".desc",
            },
        }

    def test_produces_events_per_container(self, source):
        from engine.ingest_core import _extract_selectors_from_html
        html = """
        <ul>
          <li class="ev"><h3>A</h3><time datetime="2026-08-01">Aug 1</time>
              <p class="loc">Venue A</p><p class="desc">Desc A</p></li>
          <li class="ev"><h3>B</h3><time datetime="2026-08-02">Aug 2</time>
              <p class="loc">Venue B</p><p class="desc">Desc B</p></li>
        </ul>
        """
        events = _extract_selectors_from_html(html, source, "https://x")
        assert len(events) == 2
        assert [e.name for e in events] == ["A", "B"]
        assert events[0].date == "2026-08-01"
        assert events[0].description == "Desc A"

    def test_skips_missing_name(self, source):
        from engine.ingest_core import _extract_selectors_from_html
        html = """
        <li class="ev"><time datetime="2026-08-01">Aug 1</time></li>
        <li class="ev"><h3>Real</h3><time datetime="2026-08-02">Aug 2</time></li>
        """
        events = _extract_selectors_from_html(html, source, "https://x")
        assert len(events) == 1
        assert events[0].name == "Real"

    def test_name_exclude_filter(self, source):
        source["selectors"]["name_exclude"] = ["Skip This"]
        from engine.ingest_core import _extract_selectors_from_html
        html = """
        <li class="ev"><h3>Skip This</h3><time datetime="2026-08-01">a</time></li>
        <li class="ev"><h3>Keep</h3><time datetime="2026-08-02">a</time></li>
        """
        events = _extract_selectors_from_html(html, source, "https://x")
        assert [e.name for e in events] == ["Keep"]

    def test_name_prefix(self, source):
        source["selectors"]["name_prefix"] = "SLT: "
        from engine.ingest_core import _extract_selectors_from_html
        html = '<li class="ev"><h3>Concert</h3><time datetime="2026-08-01">a</time></li>'
        events = _extract_selectors_from_html(html, source, "https://x")
        assert events[0].name == "SLT: Concert"

    def test_date_regex_extracts_from_text(self):
        source = {
            "name": "s", "hosting_group": "h", "default_location": "",
            "selectors": {"container": "li", "name": "h3",
                          "date_regex": r"(\w+ \d+, \d+)"},
        }
        from engine.ingest_core import _extract_selectors_from_html
        html = '<li><h3>Show</h3>Runs July 4, 2026 through August.</li>'
        events = _extract_selectors_from_html(html, source, "https://x")
        assert len(events) == 1
        assert events[0].date == "July 4, 2026"

    def test_date_regex_all_emits_one_per_match(self):
        source = {
            "name": "s", "hosting_group": "h", "default_location": "",
            "selectors": {"container": "li", "name": "h3",
                          "date_regex": r"(\d{4}-\d{2}-\d{2})",
                          "date_regex_all": True},
        }
        from engine.ingest_core import _extract_selectors_from_html
        html = '<li><h3>Recurring</h3>Dates: 2026-07-01 and 2026-07-08 and 2026-07-15.</li>'
        events = _extract_selectors_from_html(html, source, "https://x")
        assert len(events) == 3
        assert [e.date for e in events] == ["2026-07-01", "2026-07-08", "2026-07-15"]

    def test_venue_override(self, source):
        source["selectors"]["venue_name"] = ".loc"
        source["venue_name_overrides"] = {"Ambiguous Name": "Canonical Venue Name"}
        from engine.ingest_core import _extract_selectors_from_html
        html = '<li class="ev"><h3>X</h3><time datetime="2026-08-01">a</time><p class="loc">Ambiguous Name</p></li>'
        events = _extract_selectors_from_html(html, source, "https://x")
        assert events[0].venue_name == "Canonical Venue Name"

    def test_relative_link_resolves(self, source):
        source["selectors"]["link"] = "a"
        source["selectors"]["link_attr"] = "href"
        from engine.ingest_core import _extract_selectors_from_html
        html = '<li class="ev"><h3>X</h3><time datetime="2026-08-01">a</time><a href="/detail/1">go</a></li>'
        events = _extract_selectors_from_html(html, source, "https://example.com/list")
        assert events[0].source_url == "https://example.com/detail/1"

    def test_no_container_returns_empty(self):
        from engine.ingest_core import _extract_selectors_from_html
        source = {"name": "s", "hosting_group": "h", "default_location": "",
                   "selectors": {}}
        assert _extract_selectors_from_html("<div/>", source, "https://x") == []


# =====================================================================
# _detail_links_from_html + _text/_attr_or_text
# =====================================================================


class TestDetailLinksFromHtml:
    def test_extracts_unique_absolute_urls(self):
        from engine.ingest_core import _detail_links_from_html
        html = '<a class="e" href="/a">A</a><a class="e" href="/b">B</a><a class="e" href="/a">dup</a>'
        links = _detail_links_from_html(html, "https://example.com/list",
                                          ".e", cap=10)
        assert links == ["https://example.com/a", "https://example.com/b"]

    def test_caps_at_limit(self):
        from engine.ingest_core import _detail_links_from_html
        html = "".join(f'<a class="e" href="/{i}">x</a>' for i in range(20))
        links = _detail_links_from_html(html, "https://x.com/", ".e", cap=5)
        assert len(links) == 5

    def test_skips_missing_href(self):
        from engine.ingest_core import _detail_links_from_html
        html = '<a class="e">no href</a><a class="e" href="/ok">ok</a>'
        links = _detail_links_from_html(html, "https://x.com/", ".e", cap=10)
        assert links == ["https://x.com/ok"]


class TestTextAndAttrOrText:
    def test_text_missing_returns_empty(self):
        from engine.ingest_core import _text
        from bs4 import BeautifulSoup
        soup = BeautifulSoup("<div/>", "lxml")
        assert _text(soup, ".missing") == ""

    def test_text_empty_selector(self):
        from engine.ingest_core import _text
        from bs4 import BeautifulSoup
        soup = BeautifulSoup("<div>hi</div>", "lxml")
        assert _text(soup, "") == ""
        assert _text(soup, None) == ""

    def test_attr_or_text_attr_wins_when_specified(self):
        from engine.ingest_core import _attr_or_text
        from bs4 import BeautifulSoup
        soup = BeautifulSoup('<a href="/x">click</a>', "lxml")
        assert _attr_or_text(soup, "a", "href") == "/x"
        assert _attr_or_text(soup, "a", None) == "click"

    def test_attr_or_text_missing_attr_returns_empty(self):
        from engine.ingest_core import _attr_or_text
        from bs4 import BeautifulSoup
        soup = BeautifulSoup('<a href="/x">click</a>', "lxml")
        assert _attr_or_text(soup, "a", "nonexistent") == ""


# =====================================================================
# Enrichment extraction
# =====================================================================


class TestExtractEnrichmentField:
    def test_string_spec_takes_first_match(self):
        from engine.ingest_core import _extract_enrichment_field
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(
            '<div class="d">First</div><div class="d">Second</div>', "lxml"
        )
        assert _extract_enrichment_field(soup, ".d") == "First"

    def test_string_spec_no_match(self):
        from engine.ingest_core import _extract_enrichment_field
        from bs4 import BeautifulSoup
        soup = BeautifulSoup("<div/>", "lxml")
        assert _extract_enrichment_field(soup, ".missing") == ""

    def test_dict_spec_length_filter(self):
        from engine.ingest_core import _extract_enrichment_field
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(
            '<div class="d">short</div>'
            '<div class="d">this one is exactly the length we want, definitely.</div>'
            '<div class="d">' + ("x" * 500) + '</div>',
            "lxml",
        )
        result = _extract_enrichment_field(
            soup, {"css": ".d", "min_length": 40, "max_length": 100}
        )
        assert "definitely" in result

    def test_dict_spec_nth_selects_within_filter(self):
        from engine.ingest_core import _extract_enrichment_field
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(
            '<div class="d">short</div>'   # filtered out
            '<div class="d">first candidate that is definitely long enough for the filter</div>'
            '<div class="d">second candidate that is definitely long enough for the filter</div>',
            "lxml",
        )
        assert _extract_enrichment_field(
            soup, {"css": ".d", "min_length": 40, "nth": 1}
        ).startswith("second")

    def test_no_css_returns_empty(self):
        from engine.ingest_core import _extract_enrichment_field
        from bs4 import BeautifulSoup
        soup = BeautifulSoup("<div>hi</div>", "lxml")
        assert _extract_enrichment_field(soup, {}) == ""

    def test_non_string_non_dict_returns_empty(self):
        from engine.ingest_core import _extract_enrichment_field
        from bs4 import BeautifulSoup
        soup = BeautifulSoup("<div>hi</div>", "lxml")
        assert _extract_enrichment_field(soup, 123) == ""


# =====================================================================
# _apply_enrichments — end-to-end without HTTP (using string fetch)
# =====================================================================


class TestApplyEnrichments:
    def test_overlays_description_on_matching_events(self, monkeypatch, sample_event):
        from engine import ingest_core as core

        # Substitute a fake fetch that returns HTML with a matchable field.
        def fake_fetch(url, mode):
            return '<div class="d">This is the enriched description text.</div>'

        monkeypatch.setattr(core, "_fetch_for_enrichment", fake_fetch)

        # Two events with the same name — both should be enriched.
        e1 = core.Event(**{**sample_event.__dict__, "description": "", "mining_extras": {}})
        e2 = core.Event(**{**sample_event.__dict__, "description": "",
                            "date": "2026-07-16T20:00:00-07:00", "mining_extras": {}})
        source = {
            "enrichment_urls": [{
                "url": "https://x",
                "matches_name": "Jazz Night",
                "selectors": {"description": ".d"},
            }],
        }
        core._apply_enrichments([e1, e2], source)
        assert e1.description == "This is the enriched description text."
        assert e2.description == "This is the enriched description text."

    def test_non_matching_events_untouched(self, monkeypatch, sample_event):
        from engine import ingest_core as core
        monkeypatch.setattr(
            core, "_fetch_for_enrichment",
            lambda url, mode: '<div class="d">enriched</div>',
        )
        e = core.Event(**{**sample_event.__dict__, "name": "Different Show",
                          "description": "original", "mining_extras": {}})
        source = {"enrichment_urls": [{
            "url": "https://x", "matches_name": "Jazz Night",
            "selectors": {"description": ".d"},
        }]}
        core._apply_enrichments([e], source)
        assert e.description == "original"

    def test_no_overwrite_flag_respected(self, monkeypatch, sample_event):
        from engine import ingest_core as core
        monkeypatch.setattr(
            core, "_fetch_for_enrichment",
            lambda url, mode: '<div class="d">enriched</div>',
        )
        e = core.Event(**{**sample_event.__dict__,
                           "description": "existing", "mining_extras": {}})
        source = {"enrichment_urls": [{
            "url": "https://x", "matches_name": "Jazz Night",
            "overwrite": False,
            "selectors": {"description": ".d"},
        }]}
        core._apply_enrichments([e], source)
        # overwrite=False + existing non-empty → left alone.
        assert e.description == "existing"

    def test_fetch_error_does_not_abort(self, monkeypatch, sample_event):
        """A single enrichment URL failing must not stop other enrichments
        (there could be many per source)."""
        from engine import ingest_core as core

        def fake_fetch(url, mode):
            if "boom" in url:
                raise RuntimeError("intentional")
            return '<div class="d">worked</div>'

        monkeypatch.setattr(core, "_fetch_for_enrichment", fake_fetch)
        e = core.Event(**{**sample_event.__dict__,
                           "description": "", "mining_extras": {}})
        source = {"enrichment_urls": [
            {"url": "https://boom", "matches_name": "Jazz Night",
              "selectors": {"description": ".d"}},
            {"url": "https://ok", "matches_name": "Jazz Night",
              "selectors": {"description": ".d"}},
        ]}
        core._apply_enrichments([e], source)
        assert e.description == "worked"

    def test_no_enrichments_no_op(self, sample_event):
        from engine.ingest_core import _apply_enrichments
        # Doesn't blow up and leaves events untouched.
        _apply_enrichments([sample_event], {})
        _apply_enrichments([sample_event], {"enrichment_urls": []})
