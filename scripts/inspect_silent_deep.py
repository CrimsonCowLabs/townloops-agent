"""Deep-dive: dump middle sections of Sierra Sun + NTEC markdown to see
whether events are actually present (LLM missed them?) or absent (page
truly has none)."""
from __future__ import annotations

import sys
sys.path.insert(0, '/app')

from engine.discovery import _firecrawl_base, _firecrawl_headers, FIRECRAWL_TIMEOUT_S
from engine.policy import firecrawl_bucket
import requests


TARGETS = [
    ("Sierra Sun Events", "https://www.sierrasun.com/events/"),
    ("North Tahoe Event Center", "https://northtahoeevents.com/"),
]


def fetch(url: str) -> str:
    firecrawl_bucket.wait()
    r = requests.post(
        f"{_firecrawl_base()}/v1/scrape",
        headers=_firecrawl_headers(),
        json={"url": url, "formats": ["markdown"]},
        timeout=FIRECRAWL_TIMEOUT_S,
    )
    r.raise_for_status()
    return (r.json().get("data") or {}).get("markdown") or ""


def main():
    for name, url in TARGETS:
        print(f"\n{'='*72}\n{name}  ({url})\n{'='*72}")
        md = fetch(url)
        # Drop the cookie/nav prefix; find where "event"-like content starts
        low = md.lower()
        i = low.find("event")
        # Skip cookie-banner mentions
        while i != -1 and i < 800:
            i = low.find("event", i + 1)
        start = max(0, (i or 0) - 100)
        chunk = md[start:start + 2500]
        print(chunk)


if __name__ == "__main__":
    main()
