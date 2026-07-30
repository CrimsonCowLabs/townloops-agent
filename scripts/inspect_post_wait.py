"""Verify that Firecrawl waitFor: 3500 is actually producing richer
markdown for the JS-heavy pages that stayed empty after the patch."""
from __future__ import annotations

import re
import sys
sys.path.insert(0, '/app')

from engine.discovery import _firecrawl_base, _firecrawl_headers, FIRECRAWL_TIMEOUT_S
from engine.policy import firecrawl_bucket
import requests


TARGETS = [
    ("#2 Placer County North Lake Tahoe", "https://www.placer.ca.gov/Calendar.aspx?CID=73"),
    ("#5 Placer County Library",          "https://www.placer.ca.gov/9133/Calendar"),
    ("#8 TCPUD /recreation/special-events", "https://www.tcpud.org/recreation/special-events"),
]


def fetch(url: str, wait_ms: int = 3500) -> str:
    firecrawl_bucket.wait()
    r = requests.post(
        f"{_firecrawl_base()}/v1/scrape",
        headers=_firecrawl_headers(),
        json={"url": url, "formats": ["markdown"], "waitFor": wait_ms},
        timeout=FIRECRAWL_TIMEOUT_S + 5,
    )
    r.raise_for_status()
    return (r.json().get("data") or {}).get("markdown") or ""


_DATES = re.compile(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]* \d{1,2}\b",
                    re.IGNORECASE)


def main():
    for name, url in TARGETS:
        print(f"\n{'='*72}\n{name}\n  {url}")
        md = fetch(url, 3500)
        dates = len(_DATES.findall(md))
        loading = md.lower().count("loading")
        print(f"  markdown: {len(md):>6d} chars · {dates} date matches · 'loading' occurs {loading}x")
        # find first date-ish line
        for i, line in enumerate(md.splitlines()):
            if _DATES.search(line):
                print(f"  first date line: {line[:200]}")
                break
        else:
            print("  first date line: (none)")
        # Also try a longer wait if empty
        if dates == 0:
            print(f"  → retrying with waitFor=7000")
            md2 = fetch(url, 7000)
            print(f"    waitFor=7000: {len(md2):>6d} chars · {len(_DATES.findall(md2))} dates")


if __name__ == "__main__":
    main()
