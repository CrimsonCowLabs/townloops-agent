"""engine.page_extract: LLM picks quotes, only quotes on the page survive."""

from __future__ import annotations

import json

from engine import entity_enrich as ee
from engine import page_extract as px

PAGE = """# Tahoe Trail Runners

We are a volunteer club for runners of every pace.

## Meetings

We meet the **2nd Tuesday** of every month at 6pm in Lake Tahoe Community College room 102.

Members get a newsletter.
"""

MEETING = {"meeting_info": ee.MEETING_INFO}


def _llm(reply):
    calls = []

    def call(prompt):
        calls.append(prompt)
        return reply if isinstance(reply, str) or reply is None else json.dumps(reply)
    call.calls = calls
    return call


def test_supported_value_is_kept_verbatim():
    llm = _llm({"meeting_info": [
        "We meet the 2nd Tuesday of every month at 6pm in Lake Tahoe Community College room 102."]})
    got = px.extract(PAGE, MEETING, llm, subject="Tahoe Trail Runners")
    assert got == {"meeting_info": "We meet the 2nd Tuesday of every month at 6pm in "
                                   "Lake Tahoe Community College room 102."}
    assert "meeting" in llm.calls[0].lower() and "Tahoe Trail Runners" in llm.calls[0]


def test_hallucinated_value_is_dropped():
    llm = _llm({"meeting_info": ["We meet every Thursday at 7pm at the Brewery."]})
    assert px.extract(PAGE, MEETING, llm, subject="x") == {"meeting_info": None}


def test_quote_on_page_but_not_about_meetings_is_dropped():
    llm = _llm({"meeting_info": ["We are a volunteer club for runners of every pace."]})
    assert px.extract(PAGE, MEETING, llm, subject="x") == {"meeting_info": None}


def test_page_without_meeting_info_is_null():
    page = "# Tahoe Trail Runners\n\nWe love running. Join us!"
    assert px.extract(page, MEETING, _llm({"meeting_info": []}), subject="x") == {"meeting_info": None}


def test_only_supported_quotes_survive_in_page_order():
    page = ("Meetings are held at the Rec Center. Board meets monthly on Mondays at 5:30 pm. "
            "General meetings every Wednesday at noon.")
    llm = _llm({"meeting_info": ["General meetings every Wednesday at noon",
                                 "Board meets monthly on Mondays at 5:30 pm.",
                                 "Socials every Friday at 9pm."]})
    assert px.extract(page, MEETING, llm, subject="x") == {
        "meeting_info": "Board meets monthly on Mondays at 5:30 pm. General meetings every Wednesday at noon."}


def test_where_sentence_is_kept_beside_the_when():
    page = "We gather the first Monday of each month at 6pm. Meetings are held in LTCC room 102."
    llm = _llm({"meeting_info": ["We gather the first Monday of each month at 6pm.",
                                 "Meetings are held in LTCC room 102."]})
    assert px.extract(page, MEETING, llm, subject="x") == {
        "meeting_info": "We gather the first Monday of each month at 6pm. Meetings are held in LTCC room 102."}


def test_unknown_or_malformed_fields_are_null():
    llm = _llm({"meeting_info": "We meet the 2nd Tuesday", "other": ["x"]})
    assert px.extract(PAGE, MEETING, llm, subject="x") == {"meeting_info": None}


def test_llm_unavailable_or_unparseable_is_none():
    assert px.extract(PAGE, MEETING, _llm(None), subject="x") is None
    assert px.extract(PAGE, MEETING, _llm("I can't help with that"), subject="x") is None


def test_join_quotes_cuts_on_a_quote_boundary():
    page = "Meets every Monday at 6pm. Meets every Tuesday at 6pm."
    quotes = ["Meets every Monday at 6pm.", "Meets every Tuesday at 6pm."]
    assert px.join_quotes(quotes, page, lambda q: True, max_chars=30) == "Meets every Monday at 6pm."


def test_find_quote_matches_normalized_text_without_outer_punctuation():
    page = px.normalize("Doors open at **7pm**. Tickets are [$10](https://x.example) at the door.")
    found = px.find_quote('  "Tickets are $10   at the door!" ', page, min_chars=4)
    assert found.text == "Tickets are $10 at the door!"
    assert page[found.pos:found.pos + len(found.key)] == "tickets are $10 at the door"
    assert px.find_quote("Tickets are $20 at the door", page, min_chars=4) is None
    assert px.find_quote("7pm", page) is None          # shorter than the default minimum
    assert px.find_quote(None, page) is None
