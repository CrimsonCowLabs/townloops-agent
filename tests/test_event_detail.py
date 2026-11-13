"""engine/event_detail.py — one budgeted read of an event's own detail page.

Accessibility notes are a product differentiator, but a wrong claim does
harm: a value is kept only when the page itself says it (quote check) and
it is about access (not "accessible by trail", not "all welcome"); else null.
The Firecrawl fetch and the Codex call are stubbed with fixture pages.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from engine import event_detail
from engine.event_extract import event_uid, upsert_event


FIXTURES = Path(__file__).parent / "fixtures" / "detail"
SEED_URL = "https://tahoe.example/events"
DETAIL_URL = "https://tahoe.example/events/jazz-night"


def page(name: str) -> str:
    return (FIXTURES / f"{name}.md").read_text()


class Stub:
    """Firecrawl + Codex stand-ins that record their calls."""

    def __init__(self, markdown="", quotes=(), fetch_ok=True, llm_ok=True, raw=None):
        self.markdown, self.quotes = markdown, list(quotes)
        self.fetch_ok, self.llm_ok, self.raw = fetch_ok, llm_ok, raw
        self.fetched: list[str] = []
        self.prompts: list[str] = []

    def fetch(self, url, timeout_s=0):
        self.fetched.append(url)
        if not self.fetch_ok:
            return (False, "", "firecrawl http 503")
        return (True, self.markdown, "")

    def llm(self, prompt, timeout_s=0):
        self.prompts.append(prompt)
        if not self.llm_ok:
            return (False, "", "codex exec timed out")
        if self.raw is not None:
            return (True, self.raw, "")
        return (True, json.dumps({"accessibility_quotes": self.quotes}), "")


@pytest.fixture
def conn(fresh_db):
    fresh_db.row_factory = sqlite3.Row
    fresh_db.isolation_level = None
    fresh_db.execute(
        "INSERT INTO event_seeds(name, url, kind, status, first_seen_at, last_seen_at) "
        "VALUES ('Tahoe Arts', ?, 'venue', 'approved', 'now', 'now')", (SEED_URL,))
    return fresh_db


def stage(conn, source_url=DETAIL_URL, title="Jazz Night", date="2026-10-09T20:00:00") -> dict:
    ev = {"title": title, "date": date, "source_url": source_url}
    upsert_event(conn, "Tahoe Arts", ev)
    uid = event_uid("Tahoe Arts", title, ev["date"])
    return dict(conn.execute("SELECT * FROM events WHERE event_uid = ?", (uid,)).fetchone())


def run(conn, event, stub, final_attempt=False):
    return event_detail.read_detail(conn, event, final_attempt=final_attempt,
                                    fetch=stub.fetch, llm=stub.llm)


def row(conn, event):
    return conn.execute("SELECT * FROM events WHERE event_uid = ?",
                        (event["event_uid"],)).fetchone()


# --------------------------------------------------------------------------
# The quote + subject checks
# --------------------------------------------------------------------------

class TestAccessibilityNotes:
    def test_stated_access_quote_is_kept(self):
        notes = event_detail.accessibility_notes(
            ["The lounge is wheelchair accessible, with step-free entry from the north parking lot."],
            page("accessible"))
        assert notes == ("The lounge is wheelchair accessible, with step-free entry "
                         "from the north parking lot.")

    def test_quote_survives_markdown_and_line_wrapping(self):
        assert event_detail.accessibility_notes(
            ["ASL interpretation is available on request"], page("accessible"))

    def test_quote_not_on_the_page_is_dropped(self):
        assert event_detail.accessibility_notes(
            ["Hearing loops are installed in every row."], page("accessible")) is None

    @pytest.mark.parametrize("quote", [
        "The overlook is accessible by trail only",
        "Everyone welcome, all levels.",
    ])
    def test_off_subject_quote_on_the_page_is_dropped(self, quote):
        assert event_detail.accessibility_notes([quote], page("trail")) is None

    def test_site_accessibility_statement_is_not_event_access(self):
        assert event_detail.accessibility_notes(
            ["We are committed to digital accessibility."], page("accessible")) is None

    def test_several_quotes_join_in_page_order_as_sentences_without_repeats(self):
        notes = event_detail.accessibility_notes(
            ["ASL interpretation is available on request",
             "The lounge is wheelchair accessible",
             "The lounge is wheelchair accessible"],
            page("accessible"))
        assert notes == ("The lounge is wheelchair accessible. "
                         "ASL interpretation is available on request.")

    def test_capped_at_the_api_limit_on_a_quote_boundary(self):
        sentence = "Wheelchair seating is in row {}."
        text = " ".join(sentence.format(i) for i in range(200))
        notes = event_detail.accessibility_notes(
            [sentence.format(i) for i in range(200)], text)
        assert len(notes) <= event_detail.ACCESSIBILITY_MAX
        assert notes.endswith(".")

    def test_non_list_and_non_string_values_are_ignored(self):
        assert event_detail.accessibility_notes("wheelchair accessible", page("accessible")) is None
        assert event_detail.accessibility_notes([None, 3], page("accessible")) is None


# --------------------------------------------------------------------------
# read_detail — one recorded read per event
# --------------------------------------------------------------------------

class TestReadDetail:
    def test_stated_access_info_is_stored(self, conn):
        ev = stage(conn)
        stub = Stub(page("accessible"), quotes=[
            "The lounge is wheelchair accessible, with step-free entry from the north parking lot.",
            "ASL interpretation is available on request"])
        result = run(conn, ev, stub)
        assert result.status == "read"
        assert stub.fetched == [DETAIL_URL]
        r = row(conn, ev)
        assert r["accessibility_notes"].startswith("The lounge is wheelchair accessible")
        assert "ASL interpretation" in r["accessibility_notes"]
        assert r["detail_read_status"] == "read"
        assert r["detail_read_url"] == DETAIL_URL
        assert r["detail_read_at"]

    def test_prompt_carries_the_detail_page(self, conn):
        stub = Stub(page("accessible"))
        run(conn, stage(conn), stub)
        assert "Lakeview Trio" in stub.prompts[0]
        assert "accessibility_quotes" in stub.prompts[0]

    def test_page_with_no_access_info_is_null(self, conn):
        ev = stage(conn)
        assert run(conn, ev, Stub(page("no_access"), quotes=[])).status == "read"
        r = row(conn, ev)
        assert r["accessibility_notes"] is None
        assert r["detail_read_status"] == "read"

    def test_trail_false_positive_is_null(self, conn):
        ev = stage(conn, source_url="https://tahoe.example/events/sunrise-yoga", title="Sunrise Yoga")
        run(conn, ev, Stub(page("trail"), quotes=["The overlook is accessible by trail only"]))
        assert row(conn, ev)["accessibility_notes"] is None

    def test_info_only_on_the_listing_page_is_null(self, conn):
        """The listing says it about every venue; this event's own page doesn't."""
        ev = stage(conn)
        run(conn, ev, Stub(page("no_access"), quotes=[
            "All our venues are wheelchair accessible and offer assisted listening devices."]))
        assert row(conn, ev)["accessibility_notes"] is None

    @pytest.mark.parametrize("source_url", [SEED_URL, SEED_URL + "/", "https://www.tahoe.example/events", None])
    def test_listing_page_or_missing_url_is_not_read(self, conn, source_url):
        ev = stage(conn, source_url=source_url)
        stub = Stub(page("listing"), quotes=["All our venues are wheelchair accessible"])
        assert run(conn, ev, stub).status == "no-detail-page"
        assert stub.fetched == [] and stub.prompts == []
        r = row(conn, ev)
        assert r["accessibility_notes"] is None
        assert r["detail_read_status"] == "no-detail-page"

    def test_a_read_page_is_not_read_again(self, conn):
        ev = stage(conn)
        run(conn, ev, Stub(page("accessible"), quotes=["The lounge is wheelchair accessible"]))
        again = Stub(page("accessible"))
        assert run(conn, dict(row(conn, ev)), again).status == "already-read"
        assert again.fetched == []
        assert row(conn, ev)["accessibility_notes"] == "The lounge is wheelchair accessible."

    def test_a_new_source_url_is_read_and_a_null_keeps_the_old_value(self, conn):
        ev = stage(conn)
        run(conn, ev, Stub(page("accessible"), quotes=["The lounge is wheelchair accessible"]))
        conn.execute("UPDATE events SET source_url = ? WHERE event_uid = ?",
                     (DETAIL_URL + "-2026", ev["event_uid"]))
        again = Stub(page("no_access"), quotes=[])
        assert run(conn, dict(row(conn, ev)), again).status == "read"
        assert again.fetched == [DETAIL_URL + "-2026"]
        r = row(conn, ev)
        assert r["accessibility_notes"] == "The lounge is wheelchair accessible."
        assert r["detail_read_url"] == DETAIL_URL + "-2026"

    @pytest.mark.parametrize("stub", [
        Stub(fetch_ok=False),
        Stub(page("accessible"), llm_ok=False),
        Stub(page("accessible"), raw="sorry, I can't help with that"),
    ], ids=["firecrawl-down", "codex-down", "codex-no-json"])
    def test_a_failed_read_is_retried(self, conn, stub):
        ev = stage(conn)
        result = run(conn, ev, stub)
        assert result.status == "retry" and result.error
        assert row(conn, ev)["detail_read_status"] is None

    def test_an_empty_firecrawl_bucket_is_a_retry_not_a_crash(self, conn):
        def starved(url):
            raise TimeoutError("token bucket wait exceeded 30.0s")
        ev = stage(conn)
        result = event_detail.read_detail(conn, ev, final_attempt=False,
                                          fetch=starved, llm=Stub().llm)
        assert result.status == "retry" and "bucket" in result.error

    def test_the_last_attempt_gives_up_without_blocking_the_event(self, conn):
        ev = stage(conn)
        result = run(conn, ev, Stub(fetch_ok=False), final_attempt=True)
        assert result.status == "failed"
        r = row(conn, ev)
        assert r["detail_read_status"] == "failed"
        assert r["accessibility_notes"] is None

    def test_a_failed_read_is_tried_again_on_the_next_pass(self, conn):
        ev = stage(conn)
        run(conn, ev, Stub(fetch_ok=False), final_attempt=True)
        stub = Stub(page("accessible"), quotes=["The lounge is wheelchair accessible"])
        assert run(conn, dict(row(conn, ev)), stub).status == "read"
        assert row(conn, ev)["accessibility_notes"] == "The lounge is wheelchair accessible."


# --------------------------------------------------------------------------
# The skill on the workqueue
# --------------------------------------------------------------------------

def _skill(conn, monkeypatch, stub):
    import importlib.util
    path = Path(__file__).resolve().parent.parent / "skills/business/event-read-detail/read.py"
    spec = importlib.util.spec_from_file_location("event_read_detail", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "open_db", lambda: conn)
    monkeypatch.setattr(event_detail, "firecrawl_page", stub.fetch)
    monkeypatch.setattr(event_detail, "codex_exec", stub.llm)
    return mod


class TestSkill:
    def test_read_exits_zero(self, conn, monkeypatch):
        from engine import workqueue
        ev = stage(conn)
        workqueue.enqueue(conn, ev["event_uid"], "read-detail")
        mod = _skill(conn, monkeypatch, Stub(page("accessible"),
                                             quotes=["The lounge is wheelchair accessible"]))
        monkeypatch.setattr(mod, "parse_event_uid_arg", lambda: ev["event_uid"])
        assert mod.main() == 0
        assert row(conn, ev)["accessibility_notes"] == "The lounge is wheelchair accessible."

    def test_failure_retries_then_gives_up_on_the_last_attempt(self, conn, monkeypatch):
        from engine import workqueue
        ev = stage(conn)
        qid = workqueue.enqueue(conn, ev["event_uid"], "read-detail")
        mod = _skill(conn, monkeypatch, Stub(fetch_ok=False))
        monkeypatch.setattr(mod, "parse_event_uid_arg", lambda: ev["event_uid"])
        assert mod.main() == 1
        workqueue.fail(conn, qid, "rc=1")
        assert mod.main() == 1
        workqueue.fail(conn, qid, "rc=1")
        # Third and last attempt: recorded as failed, the event moves on.
        assert mod.main() == 0
        assert row(conn, ev)["detail_read_status"] == "failed"
        assert not row(conn, ev)["state"].startswith("stuck-")

    def test_unknown_event_is_an_error(self, conn, monkeypatch):
        mod = _skill(conn, monkeypatch, Stub())
        monkeypatch.setattr(mod, "parse_event_uid_arg", lambda: "nope")
        assert mod.main() == 2


class TestPipeline:
    def test_approval_reads_the_detail_page_before_deriving_the_venue(self):
        from engine import event_state
        assert event_state.next_action_for("approved") == "read-detail"
        assert event_state.next_state_for("read-detail") == "detail-read"
        assert event_state.next_action_for("detail-read") == "extract-venue"
        assert "detail-read" in event_state.IN_FLIGHT_STATES


# --------------------------------------------------------------------------
# The blanks a listing or an adapter left: description, categories, tags
# --------------------------------------------------------------------------

ABOUT = "A quartet from Reno plays standards and originals in the lounge all night."
FILLED_PAGE = (f"# Jazz Night\n\n{ABOUT}\n\nAdd to Google Calendar\n\n"
               "Subscribe to our newsletter for more great shows every single week.\n")


def vocabulary(state):
    (state / "categories.json").write_text(json.dumps(
        [{"slug": "live-music", "name": "Live Music"}, {"slug": "sports", "name": "Sports"}]))


def answer(**parsed):
    return Stub(FILLED_PAGE, raw=json.dumps({"accessibility_quotes": [], **parsed}))


class TestDetailDescription:
    def test_the_pages_sentences_about_the_event_are_kept(self):
        assert event_detail.detail_description([ABOUT], FILLED_PAGE) == ABOUT

    @pytest.mark.parametrize("quote", [
        "Subscribe to our newsletter for more great shows every single week.",
        "Add to Google Calendar",
        "Jazz Night",                              # too short to describe anything
    ])
    def test_page_furniture_is_dropped(self, quote):
        assert event_detail.detail_description([quote], FILLED_PAGE) is None

    def test_a_sentence_not_on_the_page_is_dropped(self):
        assert event_detail.detail_description(
            ["A beloved trio returns for one night only to the lounge."], FILLED_PAGE) is None


class TestFillsBlanks:
    def test_a_blank_description_is_asked_for_and_kept_apart_from_the_listings(self, conn):
        ev = stage(conn)
        stub = answer(description_quotes=[ABOUT])
        assert run(conn, ev, stub).fields["detail_description"] == ABOUT
        assert '"description_quotes"' in stub.prompts[0]
        r = row(conn, ev)
        assert r["detail_description"] == ABOUT
        assert r["description"] == "" and r["content_hash"] == ev["content_hash"]

    def test_a_listing_description_is_not_asked_for_again(self, conn):
        ev = {"title": "Jazz Night", "date": "2026-10-09T20:00:00", "source_url": DETAIL_URL,
              "description": ABOUT}
        upsert_event(conn, "Tahoe Arts", ev)
        stub = answer()
        run(conn, dict(conn.execute("SELECT * FROM events").fetchone()), stub)
        assert '"description_quotes"' not in stub.prompts[0]

    def test_categories_come_only_from_the_towns_vocabulary(self, conn, tmp_state_dir):
        vocabulary(tmp_state_dir)
        ev = stage(conn)
        stub = answer(categories=["live-music", "opera"], tags=["Jazz", "Lounge"])
        run(conn, ev, stub)
        assert "live-music (Live Music)" in stub.prompts[0]
        r = row(conn, ev)
        assert json.loads(r["category_slugs"]) == ["live-music"]
        assert r["category_slug"] == "live-music"
        assert json.loads(r["tags"]) == ["jazz", "lounge"]

    def test_categories_the_row_has_are_neither_asked_for_nor_replaced(self, conn, tmp_state_dir):
        vocabulary(tmp_state_dir)
        upsert_event(conn, "Tahoe Arts", {"title": "Jazz Night", "date": "2026-10-09T20:00:00",
                                          "source_url": DETAIL_URL, "categories": ["sports"],
                                          "tags": ["x"]})
        ev = dict(conn.execute("SELECT * FROM events").fetchone())
        stub = answer(categories=["live-music"], tags=["jazz"])
        run(conn, ev, stub)
        assert '"categories"' not in stub.prompts[0] and '"tags"' not in stub.prompts[0]
        r = row(conn, ev)
        assert json.loads(r["category_slugs"]) == ["sports"] and json.loads(r["tags"]) == ["x"]

    def test_no_page_of_its_own_is_classified_from_its_text_without_a_fetch(
            self, conn, tmp_state_dir):
        vocabulary(tmp_state_dir)
        ev = stage(conn, source_url=None)
        stub = Stub(raw=json.dumps({"categories": ["live-music"], "tags": []}))
        assert run(conn, ev, stub).status == "no-detail-page"
        assert stub.fetched == []
        [prompt] = stub.prompts
        assert prompt.startswith("Classify this local event") and "Title: Jazz Night" in prompt
        assert json.loads(row(conn, ev)["category_slugs"]) == ["live-music"]

    def test_a_failed_classification_fills_nothing(self, conn, tmp_state_dir):
        vocabulary(tmp_state_dir)
        ev = stage(conn, source_url=None)
        assert run(conn, ev, Stub(llm_ok=False)).status == "no-detail-page"
        assert row(conn, ev)["category_slugs"] is None


class TestBackfill:
    def _read_before(self, conn, monkeypatch):
        """An event read before the detail read asked for its blanks."""
        ev = stage(conn)
        conn.execute("UPDATE events SET detail_read_status = 'read', detail_read_url = ?, "
                     "date_utc = '2999-01-01T00:00:00+00:00', state = 'entities-enriched' "
                     "WHERE event_uid = ?", (DETAIL_URL, ev["event_uid"]))
        monkeypatch.setattr(event_detail, "cached_page",
                            lambda uid: FILLED_PAGE if uid == ev["event_uid"] else None)
        return dict(row(conn, ev))

    def test_it_asks_only_the_blanks_of_the_cached_page(self, conn, tmp_state_dir, monkeypatch):
        vocabulary(tmp_state_dir)
        ev = self._read_before(conn, monkeypatch)
        stub = Stub(raw=json.dumps({"description_quotes": [ABOUT], "categories": ["live-music"]}))
        assert [c["event_uid"] for c in event_detail.backfill_candidates(conn, 10)] \
            == [ev["event_uid"]]
        found = event_detail.backfill(conn, ev, llm=stub.llm)
        assert set(found) == {"detail_description", "category_slugs"}
        assert stub.fetched == [] and '"accessibility_quotes"' not in stub.prompts[0]
        r = row(conn, ev)
        assert r["detail_description"] == ABOUT
        assert json.loads(r["category_slugs"]) == ["live-music"]

    def test_the_skill_tries_each_event_once(self, conn, tmp_state_dir, monkeypatch):
        vocabulary(tmp_state_dir)
        ev = self._read_before(conn, monkeypatch)
        mod = _skill(conn, monkeypatch, Stub(raw="{}"))
        assert mod.main(["backfill", "--limit", "5"]) == 0
        assert event_detail.backfill_candidates(conn, 10) == []
        assert row(conn, ev)["detail_description"] is None

    def test_a_shipped_or_complete_event_is_no_candidate(self, conn, monkeypatch):
        ev = self._read_before(conn, monkeypatch)
        conn.execute("UPDATE events SET townloops_slug = 'jazz' WHERE event_uid = ?",
                     (ev["event_uid"],))
        assert event_detail.backfill_candidates(conn, 10) == []
