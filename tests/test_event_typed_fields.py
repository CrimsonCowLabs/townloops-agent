"""The pure rules behind an event's image and typed fields (#64): which
picture a page offers with an alt that describes it (engine/images), and
which stated prices / age sentences map onto TownLoops' typed fields
(engine/event_tickets). Whole-flow behaviour is in tests/test_event_images.py."""

from __future__ import annotations

import json

import pytest

from engine import images
from engine.event_tickets import age_restriction, typed_price


class TestTypedPrice:
    @pytest.mark.parametrize("price, typed", [
        ("Free", {"price_kind": "free"}),
        ("Free admission; parking $5", {"price_kind": "free"}),
        ("$0", {"price_kind": "free"}),
        ("Tickets $15", {"price_kind": "paid", "price_min_cents": 1500}),
        ("$12.50 per person", {"price_kind": "paid", "price_min_cents": 1250}),
        ("$10-$20", {"price_kind": "paid", "price_min_cents": 1000, "price_max_cents": 2000}),
        ("Tickets $10 – $20", {"price_kind": "paid", "price_min_cents": 1000,
                               "price_max_cents": 2000}),
        ("$10 to 20", {"price_kind": "paid", "price_min_cents": 1000, "price_max_cents": 2000}),
        ("$15-$15", {"price_kind": "paid", "price_min_cents": 1500}),
    ])
    def test_unambiguous_wording_maps(self, price, typed):
        assert typed_price(price) == typed

    @pytest.mark.parametrize("price", [
        None, "", "Free for kids, $15 adults", "$15 advance, $20 at the door",
        "$5 suggested donation", "Pay what you can", "Sold out", "Waitlist only",
        "$10 members, $15 public, $20 at the door", "Free with museum admission",
    ])
    def test_anything_else_sends_nothing(self, price):
        assert typed_price(price) == {}


class TestAgeRestriction:
    @pytest.mark.parametrize("text", ["21+ show", "Ages 18 and over", "18+ only",
                                      "You must be 21 to attend.", "Adults only.",
                                      "No one under 21 admitted."])
    def test_adults_only(self, text):
        assert age_restriction(text) == "adults_only"

    @pytest.mark.parametrize("text", ["All ages welcome!", "This is an all-ages show."])
    def test_all_ages(self, text):
        assert age_restriction(text) == "all_ages"

    @pytest.mark.parametrize("text", [None, "Ages 5-12", "Family friendly fun",
                                      "All ages until 9pm, 21+ after", "Tickets $21"])
    def test_anything_else_is_none(self, text):
        assert age_restriction(text) is None


class TestUsableAlt:
    @pytest.mark.parametrize("alt", [None, "", "  ", "Image", "Event image", "featured photo",
                                     "IMG_1234", "jazz-night-poster.jpg", "Jazz Night",
                                     "Company logo"])
    def test_rejected(self, alt):
        assert images.usable_alt(alt, "Jazz Night") is None

    def test_a_description_is_kept_whitespace_collapsed(self):
        assert images.usable_alt("  Trio on the  lounge stage &amp; crowd ", "Jazz Night") \
            == "Trio on the lounge stage & crowd"

    def test_cut_to_the_api_limit_on_a_word(self):
        alt = images.usable_alt("word " * 100, None)
        assert len(alt) <= images.ALT_MAX and alt.endswith("…")


def _html(*parts):
    return "<html><head>" + "".join(parts) + "</head><body></body></html>"


def _ld(obj):
    return f'<script type="application/ld+json">{json.dumps(obj)}</script>'


class TestPageImage:
    URL = "https://arena.example/events/jazz"

    def test_json_ld_event_image_object_with_a_caption(self):
        html = _html(_ld({"@type": "MusicEvent", "name": "Jazz Night",
                          "image": {"@type": "ImageObject", "url": "/img/trio.jpg",
                                    "caption": "The trio on the lounge stage"}}))
        assert images.page_image(html, {}, self.URL, "Jazz Night") == images.Image(
            "https://arena.example/img/trio.jpg", "The trio on the lounge stage")

    def test_og_image_with_its_alt(self):
        html = _html('<meta property="og:image" content="https://cdn.example/trio.jpg">',
                     '<meta property="og:image:alt" content="The trio on stage">')
        assert images.page_image(html, {}, self.URL, "Jazz Night").url == \
            "https://cdn.example/trio.jpg"

    def test_a_bare_ld_url_borrows_og_alt_only_for_the_same_picture(self):
        html = _html(_ld({"@type": "Event", "image": "https://cdn.example/a.jpg"}),
                     '<meta property="og:image" content="https://cdn.example/b.jpg">',
                     '<meta property="og:image:alt" content="Dancers on the plaza">')
        assert images.page_image(html, {}, self.URL, "Jazz Night") == images.Image(
            "https://cdn.example/b.jpg", "Dancers on the plaza")

    def test_firecrawl_metadata_when_there_is_no_html(self):
        meta = {"ogImage": "https://cdn.example/trio.jpg", "og:image:alt": "The trio on stage"}
        assert images.page_image(None, meta, self.URL, "Jazz Night").alt == "The trio on stage"

    @pytest.mark.parametrize("head", [
        '<meta property="og:image" content="https://cdn.example/trio.jpg">',   # no alt
        '<meta property="og:image" content="https://cdn.example/site-logo.png">'
        '<meta property="og:image:alt" content="Arena at night">',             # a logo
        '<meta property="og:image" content="http://cdn.example/trio.jpg">'
        '<meta property="og:image:alt" content="The trio on stage">',          # not https
    ])
    def test_no_candidate(self, head):
        assert images.page_image(_html(head), {}, self.URL, "Jazz Night") is None

    def test_a_non_event_node_image_is_not_the_events(self):
        html = _html(_ld({"@type": "Organization", "image": {
            "url": "https://cdn.example/building.jpg", "caption": "The arena building"}}))
        assert images.page_image(html, {}, self.URL, "Jazz Night") is None


class TestMarkdownImage:
    def test_first_usable(self):
        md = "![logo](https://x.example/logo.png)\n![The trio on stage](https://x.example/t.jpg)"
        assert images.markdown_image(md, "Jazz Night") == images.Image(
            "https://x.example/t.jpg", "The trio on stage")
