"""eventcards — a town adapter as a town agent writes one
($HERMES_HOME/adapters/eventcards.py): a server-rendered calendar of event
cards, paged by `?page=N` until there's no "Next" link, each card's own page
read for its description. Kept here as the harness's HTML case."""

from urllib.parse import urljoin

from bs4 import BeautifulSoup

from engine.seed_adapters import Adapter, AdapterError, Param, html_text

MAX_PAGES = 20


def _positive(v):
    return None if isinstance(v, int) and not isinstance(v, bool) and v > 0 else "must be above 0"


def _card(card, base, fetch):
    link = card.select_one("h3 a")
    when = card.select_one("time")
    venue = card.select_one(".venue")
    url = urljoin(base, link["href"]) if link and link.get("href") else None
    description = None
    if url:
        page = BeautifulSoup(fetch.get_text(url), "html.parser")
        body = page.select_one(".event-description")
        description = html_text(str(body)) if body else None
    return {
        "title": link.get_text(" ", strip=True) if link else None,
        "date": when.get("datetime") if when else None,
        "end_at": None,
        "venue": venue.get_text(" ", strip=True) if venue else None,
        "location": None,
        "description": description,
        "categories": [],
        "tags": [],
        "source_url": url,
    }


def read(seed, config, fetch):
    url = config.get("url") or seed["url"]
    max_pages = config.get("max_pages") or MAX_PAGES
    events, page = [], 1
    while True:
        soup = BeautifulSoup(fetch.get_text(url, {"page": page}), "html.parser")
        events += [_card(c, url, fetch) for c in soup.select(".event-card")]
        if not soup.select_one("a.next"):
            return events
        page += 1
        if page > max_pages:
            raise AdapterError(f"more than max_pages {max_pages} pages")


ADAPTER = Adapter(read, {
    "url": Param("the calendar page (default: the seed URL)",
                 lambda v: None if isinstance(v, str) and v.startswith("http") else "must be a URL"),
    "max_pages": Param(f"refuse a calendar longer than this (default {MAX_PAGES})", _positive),
})
