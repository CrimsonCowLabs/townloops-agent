"""engine/own_site.py — the one own-site rule (performer research's presence
test and discovery's verify both call it)."""

from __future__ import annotations

import pytest

from engine import own_site


@pytest.mark.parametrize("name, url, homepage_counts, reason", [
    ("Jane Doe", "https://www.yelp.com/biz/jane-doe", True, "listing host"),
    ("Jane Doe", "https://www.instagram.com/p/Cxyz123/", True, "not a profile"),
    ("Jane Doe", "https://janedoemusic.example/events/1", True, "an event page"),
    ("Some Blog Post", "https://blog.example/post/x", True, "doesn't carry the name"),
    ("Jane Doe", "https://other.example/", False, "doesn't carry the name"),
    (None, "https://blog.example/post/x", True, "doesn't carry the name"),
])
def test_reasons_name_the_rule_that_fired(name, url, homepage_counts, reason):
    assert reason in own_site.not_own_reason(name, url, homepage_counts=homepage_counts)


@pytest.mark.parametrize("name, url, homepage_counts", [
    ("Jane Doe", "https://www.instagram.com/janedoe/", False),
    ("Jane Doe", "https://janedoemusic.example/about", False),
    (None, "https://anything.example/", True),
])
def test_own(name, url, homepage_counts):
    assert own_site.is_own(name, url, homepage_counts=homepage_counts)


def test_excluded_host():
    assert own_site.not_own_reason("Jane Doe", "https://janedoe.example/", homepage_counts=True,
                                   exclude_hosts={"janedoe.example"}) == "janedoe.example is excluded"


@pytest.mark.parametrize("name, url, matches", [
    ("Jane Doe", "https://janedoe.bandcamp.com/", True),
    ("Jane Doe", "https://venue.example/artists/jane-doe", True),
    ("Moe", "https://venue.example/artists/moe", False),      # under 5 letters: too loose
    ("Jane Doe", "https://venue.example/artists/john", False),
])
def test_url_carries_name(name, url, matches):
    assert own_site.url_carries_name(name, url) is matches


@pytest.mark.parametrize("url, utility", [
    ("https://business.tahoechamber.org/member-application", True),
    ("https://business.tahoechamber.org/jobs", True),
    ("https://business.tahoechamber.org/hotdeals", True),
    ("https://business.tahoechamber.org/list", True),
    ("https://example.org/careers/", True),
    ("https://tahoeoutdooradventures.com/kayak-tours", False),
    ("https://example.org/", False),
])
def test_utility_page(url, utility):
    assert own_site.is_utility_page(url) is utility


@pytest.mark.parametrize("url, article", [
    ("https://laketahoethisweek.com/blog/tahoes-art-scene", True),
    ("https://example.com/blogs/news", True),
    ("https://insidetheupgrade.com/post/x", True),
    ("https://example.com/article-12345", True),
    ("https://example.com/articles/", True),
    ("https://example.com/visitor-guide", True),
    ("https://example.com/visitors-guide/2024", True),
    ("https://example.com/travel-guide", True),
    ("https://example.com/blogger-tools", False),
    ("https://example.com/posters", False),
    ("https://example.com/", False),
])
def test_article_page(url, article):
    assert own_site.is_article_page(url) is article


@pytest.mark.parametrize("url, homepage", [
    ("https://example.com", True), ("https://example.com/", True),
    ("https://example.com/?ref=x", True), ("https://example.com/about", False),
])
def test_homepage(url, homepage):
    assert own_site.is_homepage(url) is homepage
