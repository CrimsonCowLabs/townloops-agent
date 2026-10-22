"""Feed probing: a seed's page is checked for a machine-readable event feed
(iCal, Google Calendar, Tribe REST, Showare, EventCalendarApp, POINT, JSON-LD, RSS,
a structured HTML calendar) — from the page alone, no extra fetches."""

from __future__ import annotations

from pathlib import Path

import pytest

from engine import feed_probe

PAGES = Path(__file__).parent / "fixtures" / "feeds"


def _detect(name: str, url: str = "https://site.example/events"):
    return feed_probe.detect((PAGES / name).read_text(), url)


def test_ical_link_is_resolved_against_the_page():
    got = _detect("ical_link.html", "https://library.example/events/")
    assert (got["type"], got["url"]) == ("ical", "https://library.example/events/feed.ics")


def test_google_calendar_embed_becomes_its_public_ical_feed():
    got = _detect("google_calendar.html")
    assert got["type"] == "google-calendar"
    assert got["url"] == ("https://calendar.google.com/calendar/ical/"
                          "townhall%40example.org/public/basic.ics")


def test_wix_calendar_wrapper_is_flagged_as_hiding_its_feed():
    # The Wix filesusr.com wrapper only postMessages the Google Calendar src —
    # there's no feed to record, and the visible widget isn't dated evidence.
    got = _detect("wix_wrapper.html")
    assert got["type"] == "wix-calendar-wrapper"
    assert got["url"] is None
    assert "hidden" in got["feeds"][0]["note"]


def test_tribe_events_calendar_points_at_its_rest_feed():
    got = _detect("tribe.html", "https://shops.example/events/")
    assert got["type"] == "tribe"
    assert got["url"] == "https://shops.example/wp-json/tribe/events/v1/events"
    # The blog RSS on the same WordPress site isn't an events feed.
    assert "rss" not in [f["type"] for f in got["feeds"]]


def test_showare_widget_points_at_its_performance_list():
    # The widget script is protocol-relative and served by the box office's own host.
    got = _detect("showare.html", "https://valhallatahoe.showare.com/")
    assert got["type"] == "showare"
    assert got["url"] == ("https://valhallatahoe.showare.com"
                          "/include/widgets/events/performancelist.asp")
    assert [f["type"] for f in got["feeds"]] == ["showare"]      # two widgets, one feed


def test_eventcalendarapp_widget_becomes_its_api_feed():
    got = _detect("eventcalendarapp.html")
    assert got["type"] == "eventcalendarapp"
    assert got["url"] == ("https://api.eventcalendarapp.com/events?id=12316"
                          "&widgetUuid=629c1363-e303-4634-8c29-f91e28fb12d1")


def test_point_widget_becomes_its_shared_events_feed():
    got = _detect("point.html")
    assert got["type"] == "point"
    assert got["url"] == ("https://pointapp-8f268.appspot.com/api/v1/shared/frames/events"
                          "?token=abc123XYZ")


def test_jsonld_events_are_counted_including_subtypes():
    got = _detect("jsonld.html", "https://riva.example/events")
    assert (got["type"], got["url"]) == ("jsonld", "https://riva.example/events")
    assert got["feeds"][0]["events"] == 2


def test_only_an_events_rss_feed_counts():
    got = _detect("rss_events.html", "https://chamber.example/")
    assert (got["type"], got["url"]) == ("rss", "https://chamber.example/events/rss")
    assert len(got["feeds"]) == 1


def test_structured_html_calendar_is_the_page_itself():
    got = _detect("html_calendar.html", "https://parks.example/calendar")
    assert (got["type"], got["url"]) == ("html-calendar", "https://parks.example/calendar")


def test_page_without_a_feed_detects_nothing():
    got = _detect("plain.html")
    assert got == {"type": None, "url": None, "feeds": []}
    assert feed_probe.detect("", "https://x.example") == {"type": None, "url": None, "feeds": []}


def test_a_real_feed_beats_the_hidden_wix_calendar():
    html = (PAGES / "wix_wrapper.html").read_text() + (PAGES / "jsonld.html").read_text()
    got = feed_probe.detect(html, "https://mcps.example/live-music")
    assert got["type"] == "jsonld"
    assert [f["type"] for f in got["feeds"]] == ["jsonld", "wix-calendar-wrapper"]


def test_tribe_plugin_assets_alone_are_not_a_calendar():
    # The plugin enqueues its CSS on every page of the site.
    html = ('<html><head><link rel="stylesheet" id="tribe-events-widgets-v2-events-list-skeleton-css" '
            'href="https://shops.example/wp-content/plugins/the-events-calendar/x.css"></head>'
            '<body><h1>About</h1></body></html>')
    assert feed_probe.detect(html, "https://shops.example/about")["type"] is None
