"""engine/listing_hosts.py — the one list of directory / listing /
aggregator hosts. Their pages name an entity but are never its website."""

from __future__ import annotations

import pytest

from engine import listing_hosts


@pytest.mark.parametrize("url", [
    # seen on SLT: a business enriched with its restaurantji page as its website
    "https://www.restaurantji.com/ca/south-lake-tahoe/cascade-kitchens-food-hall-and-bar-/",
    "https://www.yelp.com/biz/cascade-kitchens-south-lake-tahoe",
    "https://m.yelp.com/biz/foo",
    "https://www.yelp.ca/biz/foo",
    "https://www.tripadvisor.com/Restaurant_Review-g1-d2.html",
    "https://www.tripadvisor.co.uk/Attraction_Review-g1-d2.html",
    "https://www.opentable.com/r/cascade-kitchens",
    "https://www.google.com/maps/place/Cascade+Kitchens",
    "https://maps.google.com/?cid=123",
    "https://maps.app.goo.gl/abc123",
    "https://www.facebook.com/events/123456/",
    "https://www.ticketmaster.com/jane-doe/event/39006",
    "https://dice.fm/event/abc",
    "https://www.eventbrite.com/e/foo-123",
])
def test_listing_pages(url):
    assert listing_hosts.is_listing_url(url)


@pytest.mark.parametrize("url", [
    "https://cascadekitchens.example/",
    "https://sites.google.com/view/tahoe-rotary",     # an entity's own (hosted) site
    "https://www.facebook.com/cuppatahoe",            # its facebook, handled as such
    "https://yelping-dog.example/",
    # plain-word names are real sites elsewhere: Foursquare churches
    "https://foursquare.org/",
    "https://tahoe.foursquare.org/",
    "https://www.foursquare.church/",
    "https://groupon.example/",
    "https://www.google.com/search?q=cafe",
    "",
    None,
])
def test_not_listing_pages(url):
    assert not listing_hosts.is_listing_url(url)
