"""Which image shows a thing, and what its alt text is — the one place.

Logos, icons and stand-ins are worse than no image: a null image is a gap
the operator sees, a generic one looks done. `is_generic_image` is that rule
for every image the agent keeps — entity enrichment (engine/research),
performer research, the CMO import, and event images.

Event images (#64): TownLoops requires `image_alt` with any image, and its
own guidance is "write the alt text, don't generate a placeholder — if you
can't say what is in the picture, send no picture". The agent can't see a
picture, so an alt is only ever copied from where the image was found (an
og:image:alt, a JSON-LD ImageObject's caption, a listing card's markdown alt,
an adapter feed's alt) and must say more than the event's title
(`usable_alt`). An image with no such alt is no candidate. Candidates, best
first: the event's own page (`page_image`: its JSON-LD Event `image`, then
og:image), an adapter's structured row, the listing card (`markdown_image`).
With none, TownLoops itself falls back to the venue's image
(`resolved_image_path`), so event-ship never copies one onto the event.
"""

from __future__ import annotations

import html as html_lib
import re
from typing import Any, NamedTuple, Optional
from urllib.parse import urljoin, urlparse


# EventPayload `image_url` / `image_alt` limits.
URL_MAX = 2083
ALT_MAX = 255

# Whole words only ("icon" not "silicon"), except "logo", which turns up
# glued into names ("sitelogo.png", "/logos/").
_GENERIC_IMAGE_RE = re.compile(
    r"logo|placeholder|favicon|spacer|sprite|gravatar|"
    r"(?<![a-z])(?:icons?|default|blank|pixel|no[-_]?image|coming[-_]?soon|missing|"
    r"badges?|avatars?)(?![a-z])",
    re.IGNORECASE,
)


def is_generic_image(url: str, hint: str = "") -> bool:
    """True for logos, icons, placeholders, avatars, SVG/ICO/GIF files and
    data URIs — images that don't show the entity itself. `hint` is the
    <img> alt text."""
    p = urlparse(url)
    if url.startswith("data:") or p.path.lower().endswith((".svg", ".ico", ".gif")):
        return True
    return bool(_GENERIC_IMAGE_RE.search(f"{p.hostname or ''}{p.path}")
                or _GENERIC_IMAGE_RE.search(hint))


def ld_images(node: Any) -> list[str]:
    """Every JSON-LD `image` value (string, {url}, or a list of either)."""
    out: list[str] = []
    if isinstance(node, list):
        for child in node:
            out.extend(ld_images(child))
    elif isinstance(node, dict):
        v = node.get("image")
        for item in v if isinstance(v, list) else [v]:
            if isinstance(item, str):
                out.append(item)
            elif isinstance(item, dict) and isinstance(item.get("url"), str):
                out.append(item["url"])
        for key, child in node.items():
            if key != "image":
                out.extend(ld_images(child))
    return out


# --------------------------------------------------------------------------
# Event images
# --------------------------------------------------------------------------

class Image(NamedTuple):
    url: str
    alt: str


# An alt that names a kind of picture, not what is in it.
_PLACEHOLDER_ALT = re.compile(
    r"(?:(?:the|an?|our|event|featured|main|hero|header|cover|banner|default|thumbnail)\s+)*"
    r"(?:image|photo|picture|pic|img|graphic|thumbnail|banner|poster|flyer|hero|cover)s?"
    r"(?:\s+(?:image|photo|picture|for this event))?",
    re.IGNORECASE)
# A file name or camera stamp ("IMG_1234", "jazz-night-poster.jpg").
_FILE_ALT = re.compile(r"\S+\.(?:jpe?g|png|webp|gif|avif)|(?:img|dsc|dscn|pxl)[-_ ]?\d+",
                       re.IGNORECASE)


def _words(text: str) -> list[str]:
    return re.findall(r"[^\W_]+", text.lower())


def usable_alt(alt, title: Optional[str] = None) -> Optional[str]:
    """`alt` as TownLoops gets it — whitespace collapsed, entities decoded, cut
    on a word to ALT_MAX — or None when it doesn't describe a picture: blank,
    one word, a placeholder ("Event image"), a file name, a logo, or just the
    event's `title` (which "describes nothing")."""
    if not isinstance(alt, str):
        return None
    text = " ".join(html_lib.unescape(alt).split())
    words = _words(text)
    if len(words) < 2 or _PLACEHOLDER_ALT.fullmatch(text.strip(" .")) or _FILE_ALT.fullmatch(text):
        return None
    if _GENERIC_IMAGE_RE.search(text) or (title and words == _words(title)):
        return None
    if len(text) > ALT_MAX:
        text = text[:ALT_MAX - 1].rsplit(" ", 1)[0].rstrip(" ,;:") + "…"
    return text


def candidate(url, alt, title: Optional[str] = None,
              base: Optional[str] = None) -> Optional[Image]:
    """An image TownLoops can fetch for the event — an https URL (resolved
    against `base`) that isn't generic, with a `usable_alt` — else None."""
    if not isinstance(url, str) or not url.strip():
        return None
    url = urljoin(base, url.strip()) if base else url.strip()
    good_alt = usable_alt(alt, title)
    if (not good_alt or not url.startswith("https://") or len(url) > URL_MAX
            or is_generic_image(url, alt if isinstance(alt, str) else "")):
        return None
    return Image(url, good_alt)


def _first(value) -> Optional[str]:
    if isinstance(value, list):
        value = value[0] if value else None
    return value.strip() if isinstance(value, str) and value.strip() else None


def _is_event(node: dict) -> bool:
    t = node.get("@type")
    return any(isinstance(x, str) and x.endswith("Event") for x in (t if isinstance(t, list) else [t]))


def _event_nodes(node: Any) -> list[dict]:
    if isinstance(node, list):
        return [n for child in node for n in _event_nodes(child)]
    if not isinstance(node, dict):
        return []
    own = [node] if _is_event(node) else []
    return own + [n for k, v in node.items() if k != "image" for n in _event_nodes(v)]


def _ld_event_images(blocks: list[Any]) -> list[tuple[str, Optional[str]]]:
    """(url, alt) of each JSON-LD Event's `image`: an ImageObject's caption /
    description / name is its alt; a bare URL has none of its own."""
    out = []
    for node in _event_nodes(blocks):
        v = node.get("image")
        for item in v if isinstance(v, list) else [v]:
            if isinstance(item, str):
                out.append((item, None))
            elif isinstance(item, dict) and isinstance(item.get("url"), str):
                out.append((item["url"], next((item[k] for k in ("caption", "description", "name")
                                               if isinstance(item.get(k), str)), None)))
    return out


def page_image(html: Optional[str], meta: Optional[dict], base_url: str,
               title: Optional[str]) -> Optional[Image]:
    """The event's own page's picture: its JSON-LD Event `image`, then its
    og:image (the page's HTML, else Firecrawl's metadata). A bare JSON-LD URL
    borrows og:image:alt only when og:image is the same picture."""
    from engine import research
    og: dict[str, str] = research.og_tags(html) if html else {}
    meta = meta or {}
    og_image = og.get("image") or _first(meta.get("og:image")) or _first(meta.get("ogImage"))
    og_alt = og.get("image:alt") or _first(meta.get("og:image:alt"))
    found = _ld_event_images(research.ld_json_blocks(html)) if html else []
    if og_image:
        found.append((og_image, og_alt))
    og_url = urljoin(base_url, og_image) if og_image else None
    for url, alt in found:
        if alt is None and og_url and urljoin(base_url, url) == og_url:
            alt = og_alt
        image = candidate(url, alt, title, base_url)
        if image:
            return image
    return None


MD_IMAGE = re.compile(r"!\[(?P<alt>[^\]]*)\]\(\s*<?(?P<src>[^)\s>]+)")


def markdown_image(text: str, title: Optional[str], base: Optional[str] = None) -> Optional[Image]:
    """The first usable markdown image (`![alt](src)`) in `text`."""
    for m in MD_IMAGE.finditer(text or ""):
        image = candidate(m.group("src"), m.group("alt"), title, base)
        if image:
            return image
    return None
