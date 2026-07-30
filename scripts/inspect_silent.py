"""Diagnostic: fetch each silent seed URL via Firecrawl and print how much
markdown came back, plus a rough signal of whether it contains event-like
patterns (dates, "event", etc.)."""
from __future__ import annotations

import re
import sys
sys.path.insert(0, '/app')

from engine.discovery import _firecrawl_base, _firecrawl_headers, FIRECRAWL_TIMEOUT_S
from engine.policy import firecrawl_bucket
import requests


SILENT = [
    (2,  "Placer County North Lake Tahoe Calendar",
         "https://www.placer.ca.gov/Calendar.aspx?CID=73"),
    (5,  "Placer County Library Calendar",
         "https://www.placer.ca.gov/9133/Calendar"),
    (7,  "North Tahoe Recreation & Parks",
         "https://ntpud.org/recreation-parks/"),
    (8,  "Tahoe City Parks & Recreation",
         "https://www.tcpud.org/departments/recreation"),
    (11, "Sierra Sun Events",
         "https://www.sierrasun.com/events/"),
    (13, "North Tahoe Event Center",
         "https://northtahoeevents.com/"),
]


# Rough event-ness heuristics
_DATE_PATTERNS = [
    re.compile(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]* \d{1,2}\b",
               re.IGNORECASE),
    re.compile(r"\b\d{1,2}/\d{1,2}/\d{2,4}\b"),
    re.compile(r"\b\d{4}-\d{2}-\d{2}\b"),
]
_TIME_PATTERN = re.compile(r"\b\d{1,2}:\d{2}\s*(am|pm)?\b", re.IGNORECASE)
_EVENT_WORDS = re.compile(r"\b(event|meeting|program|calendar|schedule|"
                          r"registration|register|rsvp)\b", re.IGNORECASE)


def fetch(url: str) -> tuple[bool, str, str]:
    firecrawl_bucket.wait()
    try:
        r = requests.post(
            f"{_firecrawl_base()}/v1/scrape",
            headers=_firecrawl_headers(),
            json={"url": url, "formats": ["markdown"]},
            timeout=FIRECRAWL_TIMEOUT_S,
        )
    except requests.RequestException as e:
        return (False, "", f"{type(e).__name__}: {e}")
    if not r.ok:
        return (False, "", f"http {r.status_code}")
    body = r.json()
    md = (body.get("data") or {}).get("markdown") or ""
    return (True, md, "")


def summarize(name: str, md: str) -> dict:
    n_chars = len(md)
    n_lines = md.count("\n")
    dates = 0
    for p in _DATE_PATTERNS:
        dates += len(p.findall(md))
    times = len(_TIME_PATTERN.findall(md))
    ev_words = len(_EVENT_WORDS.findall(md))
    return {
        "chars": n_chars, "lines": n_lines,
        "dates": dates, "times": times, "event_words": ev_words,
    }


def main():
    for seed_id, name, url in SILENT:
        print(f"\n{'='*72}")
        print(f"#{seed_id} {name}")
        print(f"    {url}")
        ok, md, err = fetch(url)
        if not ok:
            print(f"    FIRECRAWL FAIL: {err}")
            continue
        s = summarize(name, md)
        print(f"    markdown: {s['chars']} chars / {s['lines']} lines")
        print(f"    signals:  {s['dates']} date-like / {s['times']} time-like / {s['event_words']} event-words")
        # Show a headline snippet — first 400 chars post-nav
        first = md[:400].replace("\n", " ")
        print(f"    head: {first[:280]}...")


if __name__ == "__main__":
    main()
