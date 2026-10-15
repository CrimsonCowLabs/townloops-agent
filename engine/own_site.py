"""Could a URL be an entity's *own* page, rather than a page *about* it?

The one rule, shared by performer research's presence test and discovery's
verify (a travel blog's "24 BEST South Lake Tahoe Restaurants" names the
town and has a "contact us" footer, but it is nobody's own site):

- never a directory/listing/ticketing host (engine.listing_hosts) or a host
  the caller excludes (performer research: the event's own source host);
- on a social platform, only a profile — not a post, event, video or share
  link;
- never an event page;
- a site whose host carries the entity's name (moe.org for "moe.",
  janedoemusic.com for "Jane Doe") — any page on it;
- otherwise only a homepage, and only where the caller says a homepage
  counts (a venue linked to it; discovery found it as a site of its own).
  A deeper page on a host that doesn't carry the name is an article, a
  review or a directory entry about the entity, not theirs.
"""

from __future__ import annotations

import re
from typing import Optional
from urllib.parse import urlparse

from engine import listing_hosts
from engine.derive_helpers import normalize_name

# host → field. Anything else is the entity's own website.
PLATFORM_FIELDS = {
    "instagram.com": "instagram", "facebook.com": "facebook", "tiktok.com": "tiktok",
    "twitter.com": "twitter", "x.com": "twitter", "linkedin.com": "linkedin",
    "spotify.com": "spotify_url", "youtube.com": "youtube", "youtu.be": "youtube",
}
EVENT_PATH = re.compile(
    r"/(?:events?|e|tickets?|shows?|concerts?|calendar|tour-dates|whats-on)(?:/|$)", re.IGNORECASE)
# A site's own utility pages — joining, jobs, deals, its member list — are
# never an organisation's own site, even on a host that carries the name.
UTILITY_PATH = re.compile(
    r"/(?:(?:member(?:ship)?-?application|jobs?|job-?board|careers?|hot-?deals"
    r"|deals|list|member-?(?:list|directory)|search)(?:/|$)|members/list(?:/|$))", re.IGNORECASE)
# Editorial pages — a blog post, an article, a visitor/travel guide — are
# written *about* organisations; discovery's link filter and verify both
# drop them.
ARTICLE_PATH = re.compile(
    r"/(blogs?|posts?|articles?|travel-guides?|visitors?-guides?)(?=/|$|[-_.\d])", re.IGNORECASE)
# What a profile (not a post, event, video, share link or site page) looks
# like on each platform.
PROFILE_PATH = {
    "instagram": r"/(?!(?:explore|reels?|p)(?:/|$))[\w.]+/?", "tiktok": r"/@[\w.]+/?",
    "twitter": r"/(?!share\b|intent\b|home\b|search\b|hashtag\b|explore\b|i\b)\w+/?",
    "facebook": r"/(?!events\b|groups\b|share\b|sharer\b|watch\b|reels?\b|login\b|photo|story"
                r"|profile\.php|permalink\.php)[\w.\-]+/?",
    "spotify_url": r"/artist/\w+/?",
    "youtube": r"/(?:@[\w.\-]+|channel/[\w\-]+|c/[\w.\-]+|user/[\w.\-]+)/?",
    "linkedin": r"/in/[\w\-]+/?",
}


def platform(url: str) -> Optional[str]:
    """The entity field a social-platform URL belongs in, or None."""
    host = listing_hosts.bare_host(url)
    for h, fld in PLATFORM_FIELDS.items():
        if host == h or host.endswith("." + h):
            return fld
    return None


def is_profile(url: str) -> bool:
    """A platform profile — not a post, event, video, share link or site page."""
    p = platform(url)
    return bool(p) and bool(re.fullmatch(PROFILE_PATH[p], urlparse(url).path or "/"))


def is_utility_page(url: str) -> bool:
    """A site's own utility page: a member application, job board, deals,
    member list or search."""
    return bool(UTILITY_PATH.search(urlparse(url).path or "/"))


def is_article_page(url: str) -> bool:
    """An editorial page: a blog post, an article, a visitor or travel guide."""
    return bool(ARTICLE_PATH.search(urlparse(url).path or "/"))


def is_homepage(url: str) -> bool:
    return (urlparse(url).path or "/").strip("/") == ""


def compact_name(s: str) -> str:
    return normalize_name(s).replace(" ", "")


def _carries(name: Optional[str], text: str) -> bool:
    # Shorter names match too much by accident.
    c = compact_name(name or "")
    return len(c) >= 5 and c in text


def host_carries_name(name: Optional[str], url: str) -> bool:
    """The site's host carries the name: moe.org for "moe.",
    janedoe.bandcamp.com / janedoemusic.com for "Jane Doe"."""
    host, c = listing_hosts.bare_host(url), compact_name(name or "")
    return bool(c) and (host.split(".")[0].replace("-", "") == c
                        or _carries(name, host.replace("-", "").replace(".", "")))


def url_carries_name(name: Optional[str], url: str) -> bool:
    """The name appears in the URL's host or path: a handle or slug
    (venue.example/artists/jane-doe for "Jane Doe")."""
    return _carries(name, compact_name(f"{listing_hosts.bare_host(url)} {urlparse(url).path}"))


def not_own_reason(name: Optional[str], url: str, *, homepage_counts: bool,
                   exclude_hosts: set[str] = frozenset()) -> Optional[str]:
    """Why `url` can't be `name`'s own page, or None when it could be."""
    host, path = listing_hosts.bare_host(url), urlparse(url).path or "/"
    if host in exclude_hosts:
        return f"{host} is excluded"
    # Ticketing / listing hosts name the entity and the town, but they are a
    # listing, not the entity.
    if listing_hosts.is_listing_host(host):
        return f"{host} is a listing host"
    if platform(url):
        return None if is_profile(url) else f"a {host} page, not a profile"
    if EVENT_PATH.search(path):
        return "an event page"
    if host_carries_name(name, url):
        return None
    if is_homepage(url):
        return None if homepage_counts else f"{host} doesn't carry the name"
    return f"a page on {host}, whose host doesn't carry the name — about it, not its own"


def is_own(name: Optional[str], url: str, *, homepage_counts: bool,
           exclude_hosts: set[str] = frozenset()) -> bool:
    return not_own_reason(name, url, homepage_counts=homepage_counts,
                          exclude_hosts=exclude_hosts) is None
