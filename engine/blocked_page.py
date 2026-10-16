"""What a fetch got back that is not the page asked for — the one detector.

`reason(text, status, title)` names a challenge or error response — an
HTTP error status, an Incapsula / Cloudflare / captcha challenge, a server's
access-denied page, a platform's "page not found", Showare's `no action`, a
near-empty body — or returns None for a page. The names are the module
constants below (`http_status(n)` for a status), so callers compare against
those, never against hand-typed strings.

Callers: event-find's listing reads (the generic Firecrawl path via
`event_extract.firecrawl_listing`, an adapter's fetch via
`seed_adapters._get_json`) — a named reason is a failed run, never
LLM-extracted, never a source-health zero, never a vanish miss; the
autonomous seed trial (`seeds.trial`); performer research's unreadable-page
check (`performer_research._unreadable`, which reads a near-empty profile
anyway).

Markers are matched only on small bodies (challenge and error pages are a
few lines): a real listing that mentions Cloudflare or "page not found"
stays content. Works on Firecrawl markdown, page text and raw HTML alike.
"""

from __future__ import annotations

import re
from typing import Optional

INCAPSULA = "incapsula challenge"
CLOUDFLARE = "cloudflare challenge"
CAPTCHA = "captcha challenge"
ACCESS_DENIED = "access denied page"
NO_ACTION = "showare no action"
NOT_FOUND = "platform 404 page"
NEAR_EMPTY = "near-empty body"
CHALLENGES = frozenset({INCAPSULA, CLOUDFLARE, CAPTCHA, ACCESS_DENIED})


def http_status(status: int) -> str:
    return f"http {status}"


# Visible text this short is no page (an 82-char Incapsula answer, a
# "Loading..." shell). A small page that really lists nothing — a heading
# and a "no upcoming events" sentence — is longer.
NEAR_EMPTY_CHARS = 64
# Challenge / 404 markers count only on a body with this little visible text.
MARKER_MAX_CHARS = 4000
# A generic challenge phrase ("Just a moment...") alone on a page this short
# is the challenge, even without Cloudflare's name on it.
TINY_CHARS = 200
# Not-found sentences are looked for this far into the visible text.
HEAD_CHARS = 600

_INCAPSULA = ("incapsula incident id", "_incapsula_resource", "request unsuccessful. incapsula")
_CLOUDFLARE = (
    "/cdn-cgi/challenge-platform", "_cf_chl_opt", "cf-chl-", "attention required! | cloudflare",
    "checking your browser before accessing",
    "needs to review the security of your connection",
    "enable javascript and cookies to continue",
    "performance & security by cloudflare", "performance &amp; security by cloudflare",
)
# Generic on their own: a challenge alongside "cloudflare", or on a tiny page.
_CHALLENGE_WEAK = ("just a moment", "verifying you are human", "verify you are human")
# Not a bare "captcha" — a contact form's "protected by reCAPTCHA" is content.
_CAPTCHA = ("complete the captcha", "solve the captcha", "captcha to continue",
            "press & hold to confirm you are", "px-captcha",
            "please verify you are a human", "confirm you are not a robot")
# Not a bare "access denied" — a members area can say it. The server's own words.
_ACCESS_DENIED = ("you don't have permission to access", "you do not have permission to access",
                  "the requested url was rejected")
# A heading, title or first line that is only a not-found notice. "404"
# counts alone or as "404 error / not found", never as a name ("404 Lounge").
_NOT_FOUND_LINE_RE = re.compile(
    r"^\W*(?:error\W*404\b|404(?:\W*$|\W+(?:error|(?:page |file )?not found)\b)"
    r"|page not found|not found\W*$)", re.IGNORECASE)
# A not-found sentence anywhere near the top.
_NOT_FOUND_SENTENCE_RE = re.compile(
    r"\bpage (?:you (?:requested|are looking for|were looking for) )?"
    r"(?:could not|cannot|can't|can not) be found\b|\bpage (?:no longer exists|not found)\b",
    re.IGNORECASE)
_HEADING_RE = re.compile(r"^\s{0,3}#{1,3}\s+(.+?)\s*#*\s*$|<h[1-3][^>]*>(.*?)</h[1-3]>",
                         re.IGNORECASE | re.MULTILINE | re.DOTALL)
_NO_ACTION_RE = re.compile(r"^no action\W*$", re.IGNORECASE)

_SCRIPT_RE = re.compile(r"<(script|style|noscript)\b.*?</\1>", re.IGNORECASE | re.DOTALL)
_TAG_RE = re.compile(r"<[^>]+>")
_MD_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_MD_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")
_MD_PUNCT_RE = re.compile(r"[#*_>`|\-=~]+")


def visible_text(text: str) -> str:
    """The words a reader would see: no tags, scripts, image or link URLs,
    markdown punctuation; whitespace collapsed."""
    t = _SCRIPT_RE.sub(" ", text or "")
    t = _TAG_RE.sub(" ", t)
    t = _MD_IMAGE_RE.sub(" ", t)
    t = _MD_LINK_RE.sub(r"\1", t)
    t = _MD_PUNCT_RE.sub(" ", t)
    return " ".join(t.split())


def _first_line(text: str) -> str:
    for line in (text or "").splitlines():
        seen = visible_text(line)
        if seen:
            return seen
    return ""


def _not_found(text: str, seen: str, title: Optional[str]) -> bool:
    lines = [title or "", _first_line(text)]
    lines += [visible_text(md or html) for md, html in _HEADING_RE.findall(text)]
    return (any(_NOT_FOUND_LINE_RE.search(line.strip()) for line in lines)
            or bool(_NOT_FOUND_SENTENCE_RE.search(seen[:HEAD_CHARS])))


def reason(text: str, status=None, title=None) -> Optional[str]:
    """Why `text` (fetched with HTTP `status`, page `title`) is not the page,
    or None when it is. A non-int status or non-str title is ignored."""
    status = status if isinstance(status, int) else None
    title = title if isinstance(title, str) else None
    raw = ((text or "") + "\n" + (title or "")).lower()
    seen = visible_text(text)
    small = len(seen) <= MARKER_MAX_CHARS
    if small:
        if any(m in raw for m in _INCAPSULA):
            return INCAPSULA
        weak = any(m in raw for m in _CHALLENGE_WEAK)
        if any(m in raw for m in _CLOUDFLARE) or (
                weak and ("cloudflare" in raw or len(seen) < TINY_CHARS)):
            return CLOUDFLARE
        if any(m in raw for m in _CAPTCHA):
            return CAPTCHA
        if any(m in raw for m in _ACCESS_DENIED):
            return ACCESS_DENIED
    if _NO_ACTION_RE.match(seen):
        return NO_ACTION
    if status is not None and status >= 400:
        return http_status(status)
    if small and _not_found(text or "", seen, title):
        return NOT_FOUND
    if len(seen) < NEAR_EMPTY_CHARS:
        return NEAR_EMPTY
    return None
