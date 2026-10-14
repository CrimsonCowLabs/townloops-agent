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
                                   exclude_hosts={"janedoe.example"})
