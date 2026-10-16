"""Seed maintenance verbs (issue #28): an agent fixes a seed's URL (`move`),
takes a seed out of service (`hold`) and puts it back (`release`) through
event-seed-discovery — audited in the decision log, reversible, never raw
SQL. Driven in a whole autonomous town (tests/autonomy_town.py)."""

from __future__ import annotations

import contextlib
import io
import json
import sqlite3

import pytest

from engine import seed_adapters
from tests.autonomy_town import PROFILE, SKILLS, Town, load, page_text, soon

ARENA = "https://arena.example/events"
MOVED = "https://arena.example/calendar"


@pytest.fixture
def town(fresh_db, tmp_state_dir, monkeypatch):
    fresh_db.row_factory = sqlite3.Row
    return Town(tmp_state_dir, monkeypatch, fresh_db)


def concert(title="Jazz Night", days=7):
    return {"title": title, "date": soon(days), "venue": "Lake Tahoe Arena",
            "location": "55 Main St, South Lake Tahoe, CA", "description": "Live music.",
            "categories": ["music"], "tags": [], "source_url": None}


def seeds_cli(town: Town, *argv: str) -> tuple[int, dict]:
    """Run event-seed-discovery with the town's DB and canned pages."""
    mod = load(SKILLS / "event-seed-discovery" / "discover.py", "seed_maintenance")
    town.mp.setattr(mod, "_DB_PATH", town.state / "townloops.db")
    town.mp.setattr("engine.event_extract._firecrawl_scrape", _scrape(town))
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = mod.main(list(argv))
    lines = out.getvalue().strip().splitlines()
    return rc, json.loads(lines[-1]) if lines else {}


def _scrape(town: Town):
    """Firecrawl under event-find's listing fetch: the canned page, else a 404."""
    def scrape(url: str, timeout_s: int = 0):
        md = town.pages.get(url)
        return (True, md, "", {"statusCode": 200}) if md else (False, "", "firecrawl http 404", {})
    return scrape


def why(town: Town, query: str) -> list[dict]:
    mod = load(SKILLS / "autonomy" / "autonomy.py", "seed_maintenance_why")
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert mod.main(["why", query]) == 0
    return json.loads(out.getvalue())["decisions"]


class TestMove:
    def test_a_verified_url_replaces_the_seed_url_keeping_the_old_one(self, town):
        sid = town.seed("Arena", ARENA)
        town.listing(MOVED, [concert()])
        rc, out = seeds_cli(town, "move", "--seed-id", str(sid), "--url", MOVED,
                            "--note", "calendar moved to /calendar")
        assert rc == 0, out
        seed = town.seed_row("Arena")
        assert (seed["url"], seed["previous_url"], seed["status"]) == (MOVED, ARENA, "approved")
        [d] = why(town, str(sid))
        assert (d["verdict"], d["reason"]) == ("move", "url-corrected")
        assert d["evidence"]["from"] == ARENA and d["evidence"]["to"] == MOVED
        assert d["evidence"]["note"] == "calendar moved to /calendar"
        town.run()
        assert town.submitted_titles() == ["Jazz Night"]

    @pytest.mark.parametrize("page", [
        None,                                                        # fetch fails
        "Just a moment... Checking your browser before accessing.",  # challenge
        "# Page Not Found\n\nThe page you requested could not be found.",
    ])
    def test_a_url_that_fails_or_is_a_challenge_or_error_page_is_refused(self, town, page):
        sid = town.seed("Arena", ARENA)
        if page:
            town.pages[MOVED] = page_text(page)
        rc, out = seeds_cli(town, "move", "--seed-id", str(sid), "--url", MOVED)
        assert rc == 1 and "error" in out
        seed = town.seed_row("Arena")
        assert (seed["url"], seed["previous_url"]) == (ARENA, None)
        assert town.decisions("seed") == []

    def test_a_url_another_seed_already_has_is_refused(self, town):
        sid = town.seed("Arena", ARENA)
        town.seed("Arena calendar", MOVED)
        town.listing(MOVED, [concert()])
        rc, out = seeds_cli(town, "move", "--seed-id", str(sid), "--url", MOVED)
        assert rc == 1 and "seed" in out["error"]
        assert town.seed_row("Arena")["url"] == ARENA

    def test_an_adapter_seed_is_verified_by_its_adapter_at_the_new_url(self, town):
        sid = town.seed("Arena", ARENA, adapter="tribe")
        read = []

        def tribe(seed, config, get_json):
            read.append(seed["url"])
            return [concert()]
        town.mp.setitem(seed_adapters.ADAPTERS, "tribe", tribe)
        rc, out = seeds_cli(town, "move", "--seed-id", str(sid), "--url", MOVED)
        assert rc == 0, out
        assert read == [MOVED]
        assert town.seed_row("Arena")["url"] == MOVED

        def broken(seed, config, get_json):
            raise seed_adapters.AdapterError("tribe feed http 404")
        town.mp.setitem(seed_adapters.ADAPTERS, "tribe", broken)
        rc, out = seeds_cli(town, "move", "--seed-id", str(sid), "--url", ARENA)
        assert rc == 1 and "tribe feed http 404" in out["error"]
        assert town.seed_row("Arena")["url"] == MOVED

    def test_restore_previous_url_undoes_a_move_and_keeps_the_status(self, town):
        sid = town.seed("Arena", ARENA)
        town.listing(MOVED, [concert()])
        seeds_cli(town, "move", "--seed-id", str(sid), "--url", MOVED)
        seeds_cli(town, "hold", "--seed-id", str(sid), "--reason", "manual-only")
        rc, out = seeds_cli(town, "restore", "--seed-id", str(sid), "--previous-url")
        assert rc == 0, out
        seed = town.seed_row("Arena")
        assert (seed["url"], seed["previous_url"], seed["status"]) == (ARENA, MOVED, "held")
        d = why(town, str(sid))[0]
        assert d["verdict"] == "restore"
        assert (d["evidence"]["from"], d["evidence"]["to"]) == (MOVED, ARENA)

    def test_restore_previous_url_without_one_is_refused(self, town):
        sid = town.seed("Arena", ARENA)
        rc, out = seeds_cli(town, "restore", "--seed-id", str(sid), "--previous-url")
        assert rc == 1 and "previous" in out["error"]


class TestHold:
    def test_a_held_seed_is_never_scraped(self, town):
        sid = town.seed("Arena", ARENA)
        town.listing(ARENA, [concert()])
        rc, out = seeds_cli(town, "hold", "--seed-id", str(sid), "--reason", "awaiting-adapter",
                            "--note", "Tribe feed needs a town filter")
        assert rc == 0, out
        seed = town.seed_row("Arena")
        assert (seed["status"], seed["hold_reason"]) == ("held", "awaiting-adapter")
        assert "Tribe feed needs a town filter" in seed["status_note"]
        town.run()
        assert town.submitted_titles() == []
        assert town.seed_row("Arena")["last_scraped_at"] is None

    def test_an_unknown_reason_is_refused(self, town):
        sid = town.seed("Arena", ARENA)
        with pytest.raises(SystemExit):
            seeds_cli(town, "hold", "--seed-id", str(sid), "--reason", "because")
        assert town.seed_row("Arena")["status"] == "approved"

    def test_autonomous_review_leaves_held_seeds_untouched(self, town):
        """Not trialed, re-approved, re-judged, relocated or retired — even a
        held seed whose page works or whose runs all came back empty."""
        live = town.seed("Live", ARENA)
        town.listing(ARENA, [concert()])
        dead = town.seed("Dead", "https://dead.example/events")
        town.listing("https://dead.example/events", [])
        for _ in range(4):
            town.find()
        town.redirects["https://dead.example/events"] = MOVED
        town.listing(MOVED, [concert("Other")])
        for sid in (live, dead):
            seeds_cli(town, "hold", "--seed-id", str(sid), "--reason", "manual-only")
        before = {n: town.seed_row(n) for n in ("Live", "Dead")}
        n_decisions = len(town.decisions())
        town.seed_review()
        for n in ("Live", "Dead"):
            after = town.seed_row(n)
            for col in ("status", "url", "previous_url", "status_note", "source_role",
                        "trial_at", "hold_reason"):
                assert after[col] == before[n][col], (n, col)
        assert len(town.decisions()) == n_decisions

    def test_why_explains_a_hold(self, town):
        sid = town.seed("Arena", ARENA)
        seeds_cli(town, "hold", "--seed-id", str(sid), "--reason", "manual-only",
                  "--note", "JS calendar")
        [d] = why(town, str(sid))
        assert (d["verdict"], d["reason"]) == ("hold", "manual-only")
        assert d["evidence"] == {"was": "approved", "note": "JS calendar"}
        assert why(town, "Arena") == [d]


class TestRelease:
    def test_autonomous_release_sends_the_seed_back_to_trial(self, town):
        sid = town.seed("Arena", ARENA, trial_at="2026-01-01T00:00:00+00:00")
        seeds_cli(town, "hold", "--seed-id", str(sid), "--reason", "awaiting-adapter")
        rc, out = seeds_cli(town, "release", "--seed-id", str(sid))
        assert (rc, out["status"]) == (0, "proposed")
        seed = town.seed_row("Arena")
        assert (seed["status"], seed["hold_reason"], seed["trial_at"]) == ("proposed", None, None)
        town.listing(ARENA, [concert()])
        town.seed_review()
        assert town.seed_row("Arena")["status"] == "approved"
        d = why(town, str(sid))
        assert [x["verdict"] for x in d[:3]] == ["approve", "release", "hold"]
        assert d[1]["reason"] == "back-to-trial"
        assert d[1]["evidence"] == {"hold_reason": "awaiting-adapter", "held_from": "approved",
                                    "to": "proposed"}

    def test_supervised_release_returns_an_approved_seed_to_service(self, town):
        town.set_profile({**PROFILE, "operating_mode": "supervised"})
        sid = town.seed("Arena", ARENA)
        seeds_cli(town, "hold", "--seed-id", str(sid), "--reason", "manual-only")
        rc, out = seeds_cli(town, "release", "--seed-id", str(sid))
        assert (rc, out["status"]) == (0, "approved")
        assert town.seed_row("Arena")["hold_reason"] is None
        d = why(town, str(sid))[0]
        assert (d["reason"], d["evidence"]["held_from"]) == ("back-in-service", "approved")

    @pytest.mark.parametrize("was", ["proposed", "rejected"])
    def test_supervised_release_never_approves_a_seed_held_before_approval(self, town, was):
        town.set_profile({**PROFILE, "operating_mode": "supervised"})
        sid = town.seed("Arena", ARENA, status=was)
        town.seed("Arena calendar", MOVED)    # a name sharing the prefix: its log is not Arena's
        seeds_cli(town, "hold", "--seed-id", "2", "--reason", "manual-only")
        seeds_cli(town, "hold", "--seed-id", str(sid), "--reason", "manual-only")
        rc, out = seeds_cli(town, "release", "--seed-id", str(sid))
        assert (rc, out["status"]) == (0, "proposed")
        d = why(town, str(sid))[0]
        assert (d["reason"], d["evidence"]["held_from"]) == ("awaiting-approval", was)

    def test_only_a_held_seed_can_be_released(self, town):
        sid = town.seed("Arena", ARENA)
        rc, out = seeds_cli(town, "release", "--seed-id", str(sid))
        assert rc == 1 and "not held" in out["error"]

    def test_restore_does_not_release_a_held_seed(self, town):
        sid = town.seed("Arena", ARENA)
        seeds_cli(town, "hold", "--seed-id", str(sid), "--reason", "manual-only")
        rc, out = seeds_cli(town, "restore", "--seed-id", str(sid))
        assert rc == 1 and "release" in out["error"]
        assert town.seed_row("Arena")["status"] == "held"


class TestVisibility:
    def test_held_seeds_and_reasons_are_in_the_nightly_digest(self, town):
        sid = town.seed("Arena", ARENA)
        seeds_cli(town, "hold", "--seed-id", str(sid), "--reason", "awaiting-adapter",
                  "--note", "Tribe feed")
        out, md, tg = town.digest()
        assert out["held_seeds"] == [{"seed_id": sid, "name": "Arena", "url": ARENA,
                                      "hold_reason": "awaiting-adapter",
                                      "hold_note": "Tribe feed"}]
        assert "## Held seeds" in md
        assert f"Arena (#{sid}) — awaiting-adapter: Tribe feed" in md
        assert "held seeds: Arena (awaiting-adapter)" in tg

    def test_held_seeds_and_reasons_are_in_the_dashboard_seed_list(
            self, town, tmp_state_dir, monkeypatch):
        import dashboard.db as dashboard_db
        from dashboard import db_v2
        monkeypatch.setattr(dashboard_db, "_DB_PATH", str(tmp_state_dir / "townloops.db"))
        sid = town.seed("Arena", ARENA)
        seeds_cli(town, "hold", "--seed-id", str(sid), "--reason", "manual-only",
                  "--note", "JS calendar")
        [row] = db_v2.event_seeds_by_status("held")
        assert (row["hold_reason"], row["hold_note"]) == ("manual-only", "JS calendar")
        assert db_v2.seed_counts()["held"] == 1

        from dashboard import main
        html = main._TEMPLATES.get_template("v2/_fragments/seeds_held.html").render(
            seeds=[row])
        assert "Arena" in html and "manual-only" in html and "JS calendar" in html

    def test_a_dashboard_approve_is_a_recorded_seed_status_change(
            self, town, tmp_state_dir, monkeypatch):
        import dashboard.db as dashboard_db
        from dashboard import db_v2
        monkeypatch.setattr(dashboard_db, "_DB_PATH", str(tmp_state_dir / "townloops.db"))
        sid = town.seed("Arena", ARENA)
        seeds_cli(town, "hold", "--seed-id", str(sid), "--reason", "manual-only")
        assert db_v2.set_seed_status(sid, "approved") == 1
        seed = town.seed_row("Arena")
        assert (seed["status"], seed["hold_reason"]) == ("approved", None)
        d = why(town, str(sid))[0]
        assert (d["verdict"], d["reason"], d["evidence"]) == ("approve", "operator",
                                                             {"was": "held"})
        assert db_v2.set_seed_status(999, "rejected") == 0
