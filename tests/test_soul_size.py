"""SOUL.md must fit Hermes' context-file limit, with headroom.

Hermes (`agent/prompt_builder.py`, `CONTEXT_FILE_MAX_CHARS = 20_000`, hard-coded,
no config) truncates any context file over 20,000 chars: it keeps the first
14,000 and the last 4,000 and silently drops the middle. A SOUL.md over the
limit means the agent never sees whatever sits in that middle (it once lost
§Hard constraints and §Improving my own skills this way).
"""

from pathlib import Path

SOUL = Path(__file__).resolve().parent.parent / "SOUL.md"
LIMIT = 18_000  # Hermes truncates at 20_000; the rest is headroom.
HEAD = 14_000   # what survives at the front if the file ever overflows


def test_soul_fits_hermes_context_limit():
    size = len(SOUL.read_text(encoding="utf-8"))
    assert size <= LIMIT, (
        f"SOUL.md is {size} chars; keep it <= {LIMIT}. Hermes truncates context "
        "files over 20,000 chars (keeps the first 14,000 + last 4,000, drops the "
        "middle), so the agent would silently lose rules. Move reference material "
        "out — into the relevant skill's SKILL.md or skills/business/<skill>/references/ "
        "— and leave a one-line 'read X when Y' pointer in SOUL.md."
    )


def test_always_needed_sections_come_first():
    text = SOUL.read_text(encoding="utf-8")
    for heading in ("## Who I am", "## Voice", "## Hard constraints",
                    "## Improving my own skills", "## Operating mode"):
        at = text.find(heading)
        assert 0 <= at < HEAD, f"{heading!r} must sit in SOUL.md's first {HEAD} chars"


def test_pointers_resolve():
    text = SOUL.read_text(encoding="utf-8")
    root = SOUL.parent
    for ref in ("skills/business/operator-status/references/dashboard-chat.md",
                "skills/business/onboarding-orchestrator/references/rhythm.md"):
        assert ref in text
        assert (root / ref).is_file(), ref
