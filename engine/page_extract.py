"""Quote-checked LLM extraction from a page's text.

The LLM only picks sentences: for each wanted field it returns sentences
copied from the page, and a sentence is kept only if it is on the page
(compared after markdown/whitespace/quote-mark normalization) and passes the
field's own relevance check. The kept sentences, in page order, are the
value — page wording, never synthesized copy. Nothing survives → None.

Used by entity enrichment (a community's `meeting_info`, a venue's
`accessibility_notes`) and by the event
detail read (`accessibility_notes`, engine/event_detail).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Callable, NamedTuple, Optional

# Prompt → reply text, or None when the call failed or wasn't made
# (engine.entity_classify.codex_llm).
LLM = Callable[[str], Optional[str]]

PAGE_CHARS = 12000
_MIN_QUOTE_CHARS = 12


@dataclass(frozen=True)
class FieldSpec:
    hint: str                          # what to copy, one prompt line
    keep: Callable[[str], bool]        # a quote is about this field
    max_chars: int


# Markdown as pages come from Firecrawl — shared with event_detail and
# event_provenance, which read the same pages.
MD_LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")      # a link or image → its text
MD_EMPHASIS = re.compile(r"[*_`]+")
PAGE_URL = re.compile(r"https?://[^\s)\]>\"'<]+")        # a bare URL in page text
_MD_BLOCKS = re.compile(r"[#>|]+")
_DASHES = str.maketrans({"—": "-", "–": "-", "‘": "'", "’": "'",
                         "“": '"', "”": '"', " ": " "})


def normalize(text: str) -> str:
    """Page and quote text as compared: markdown links → their text, emphasis
    and heading marks gone, dashes/quotes unified, whitespace collapsed."""
    text = MD_LINK.sub(r"\1", text.translate(_DASHES))
    text = _MD_BLOCKS.sub(" ", MD_EMPHASIS.sub("", text))
    text = re.sub(r"\s+([,.;:!?])", r"\1", text)
    return re.sub(r"\s+", " ", text).strip().lower()


def plain_text(text: str) -> str:
    """Markdown as read: links and images → their text, emphasis gone."""
    return MD_EMPHASIS.sub("", MD_LINK.sub(r"\1", text or "")).strip()


class Found(NamedTuple):
    text: str   # the quote, whitespace collapsed and outer quote marks gone
    key: str    # its normalized form, as found in the page
    pos: int    # where, in the normalized page


def find_quote(quote, norm_page: str, min_chars: int = _MIN_QUOTE_CHARS) -> Optional[Found]:
    """The one quote-on-page check: `quote` as found in `norm_page`
    (`normalize(page)`), compared without outer punctuation. None when it
    isn't a string, is shorter than `min_chars` normalized, or isn't there."""
    if not isinstance(quote, str):
        return None
    text = re.sub(r"\s+", " ", quote).strip().strip("\"'")
    key = normalize(text).strip(" .,;:!?\"'")
    if len(key) < min_chars:
        return None
    pos = norm_page.find(key)
    return Found(text, key, pos) if pos >= 0 else None


def join_quotes(quotes, page: str, keep: Callable[[str], bool],
                max_chars: int) -> Optional[str]:
    """The quotes that are on `page` and pass `keep`, in page order, as
    sentences joined by spaces and cut on a quote boundary to `max_chars`.
    None when none survive."""
    if not isinstance(quotes, list):
        return None
    norm_page = normalize(page)
    kept: dict[str, tuple[int, str]] = {}
    for q in quotes:
        found = find_quote(q, norm_page)
        if not found or found.key in kept or not keep(found.text):
            continue
        quote = found.text
        kept[found.key] = (found.pos, quote if quote[-1] in ".!?" else quote.rstrip(",;:") + ".")
    joined = ""
    for _, sentence in sorted(kept.values()):
        longer = f"{joined} {sentence}".strip()
        if len(longer) > max_chars:
            break
        joined = longer
    return joined or None


def parse_object(reply: Optional[str]) -> Optional[dict]:
    """The JSON object in an LLM reply (CLI output can carry noise around
    it), or None."""
    text = (reply or "").strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end < start:
        return None
    try:
        obj = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def build_prompt(page: str, fields: dict[str, FieldSpec], subject: str) -> str:
    keys = "\n".join(
        f'  "{k}": JSON array of sentences copied word for word from the page that '
        f"state {h.hint}. Copy the page's exact words; do not paraphrase, summarize "
        "or infer. [] if the page says nothing"
        for k, h in fields.items())
    return f"""Below is the text of the website of "{subject}".

Return ONLY a JSON object — no prose, no markdown, no code fence — with these keys:
{keys}

Page text follows:
---
{page[:PAGE_CHARS]}
---
"""


def extract(page: str, fields: dict[str, FieldSpec], llm: LLM, *,
            subject: str) -> Optional[dict]:
    """Ask `llm` for `fields` from `page`. Returns {field: value or None},
    or None when the LLM call failed or its reply had no JSON object — not
    looked at, as opposed to not found."""
    parsed = parse_object(llm(build_prompt(page, fields, subject)))
    if parsed is None:
        return None
    return {k: join_quotes(parsed.get(k), page, h.keep, h.max_chars) for k, h in fields.items()}

