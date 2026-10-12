"""Directory, listing and aggregator hosts — the one list.

A page on one of these names an entity (a restaurant's restaurantji or Yelp
page, a show's Ticketmaster page) but is never that entity's website: it is
research evidence at most. Enrichment's website choice, discovery's search
blocklist, the business/community promote path and performer research all
check here (seen on SLT: a business enriched with its restaurantji page as
its `website`).

Social profiles are not listings — a Facebook page is the entity's
`facebook` — but a Facebook *event* is.
"""

from __future__ import annotations

from typing import Optional
from urllib.parse import urlparse

# Domains, matched with their subdomains (m.yelp.com).
LISTING_DOMAINS = (
    # reviews / directories / restaurant aggregators
    "restaurantji.com", "allmenus.com", "menupix.com", "menuism.com", "sirved.com",
    "yellowpages.com", "foursquare.com", "mapquest.com", "bbb.org", "manta.com",
    "chamberofcommerce.com", "nextdoor.com", "wanderlog.com", "groupon.com",
    "doordash.com", "ubereats.com", "grubhub.com", "seamless.com",
    "wikipedia.org", "wikivoyage.org", "wikitravel.org", "reddit.com", "pinterest.com",
    # ticketing / event listings
    "livenation.com", "eventbrite.com", "axs.com", "songkick.com", "bandsintown.com",
    "seatgeek.com", "stubhub.com", "vividseats.com", "jambase.com", "allevents.in",
    "eventful.com", "extrachill.com", "ticketweb.com", "etix.com", "dice.fm",
    "showclix.com", "tixr.com", "seetickets.us", "seetickets.com",
)
# Multi-country brands, matched as any label of the host so every country
# domain counts (yelp.ca, tripadvisor.co.uk, ticketmaster.ie).
LISTING_BRANDS = ("yelp", "tripadvisor", "ticketmaster", "opentable", "zomato",
                  "restaurantguru")


def bare_host(url: str) -> str:
    """`url`'s lowercased host without a leading www."""
    return (urlparse(url).hostname or "").lower().removeprefix("www.")


def is_listing_host(host: str) -> bool:
    """Is `host` (lowercased, as bare_host gives it) a directory/listing/
    aggregator host?"""
    labels = host.split(".")[:-1]
    return (any(host == d or host.endswith("." + d) for d in LISTING_DOMAINS)
            or any(b in labels for b in LISTING_BRANDS))


def is_listing_url(url: Optional[str]) -> bool:
    """Is `url` a listing page — on a LISTING_HOSTS host, a map (Google,
    Bing, Apple), or a Facebook event — rather than an entity's own page?"""
    host = bare_host(url or "")
    if not host:
        return False
    path = urlparse(url).path.lower()
    labels = host.split(".")
    if "maps" in labels[:-1] and ("google" in labels or "apple" in labels or "goo" in labels):
        return True                     # maps.google.com, maps.apple.com, maps.app.goo.gl
    if ("google" in labels or host == "bing.com") and path.startswith("/maps"):
        return True
    if host == "g.page" or (host == "goo.gl" and path.startswith("/maps")):
        return True
    if (host == "facebook.com" or host.endswith(".facebook.com")) and path.startswith("/events"):
        return True
    return is_listing_host(host)
