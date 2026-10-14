"""listing-website-cleanup: the one-off clear of listing-page websites, then
re-enrich (engine/listing_website_cleanup.py).

Supervised mode is driven through the engine against FakeAPI + a fresh DB
(no town profile → supervised); autonomous mode through a whole town
(tests/autonomy_town.py) and the skill's `apply`: local cleared, live
flagged for the nightly digest, never PATCHed or submitted.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from engine import entity_enrich as ee
from engine import entity_sync
from engine import listing_website_cleanup as lwc
from tests.autonomy_town import Town
from tests.fake_townloops import FakeAPI

RESTAURANTJI = "https://www.restaurantji.com/ca/south-lake-tahoe/cascade-kitchens-food-hall-and-bar-/"
CASCADE = "Cascade Kitchens Food Hall & Bar"
OWN_SITE = "https://cascadekitchens.com/"
SLUG = "cascade-kitchens-food-hall-bar"


@pytest.fixture
def conn(fresh_db):
    fresh_db.row_factory = sqlite3.Row
    fresh_db.isolation_level = None
    return fresh_db


@pytest.fixture
def net(monkeypatch):
    """Firecrawl search finds Cascade's own site; reading it gives its phone."""
    calls = {"search": [], "research": []}

    class Res:
        def __init__(self, url):
            self.payload = {"name": CASCADE, "phone": "530-555-0100"}
            self.fetched_from = [url]
            self.text = ""
            self.links = []

    def fake_research(url, use_firecrawl=False):
        calls["research"].append(url)
        return Res(url)

    def fake_search(q, limit=5, **kw):
        calls["search"].append(q)
        return [{"url": RESTAURANTJI, "title": CASCADE},
                {"url": OWN_SITE, "title": f"{CASCADE} | South Lake Tahoe"}]

    import engine.discovery as discovery
    import engine.research as research
    monkeypatch.setattr(research, "research_url", fake_research)
    monkeypatch.setattr(discovery, "firecrawl_search", fake_search)
    return calls


def _shipped(conn, uid="b1", website=RESTAURANTJI, slug=SLUG, source_kind="derived",
             table="businesses", name=CASCADE):
    """A local entity shipped and in sync with live (pushed_hash = content_hash)."""
    conn.execute(
        f"INSERT INTO {table} (entity_uid, source, source_kind, name, website, townloops_slug,"
        " pushed_at, extra_json, enrichment_status, first_seen_at, last_seen_at,"
        " last_modified_at, content_hash) VALUES (?, 'seed', ?, ?, ?, ?, '2026-10-11T18:10:36Z',"
        " ?, 'enriched', '2026-10-01T00:00:00Z', '2026-10-01T00:00:00Z',"
        " '2026-10-01T00:00:00Z', '')",
        (uid, source_kind, name, website, slug, json.dumps({"live_id": 90})))
    kind = {"businesses": "business", "venues": "venue", "communities": "community"}[table]
    entity_sync.refresh_content_hash(conn, kind, uid)
    conn.execute(f"UPDATE {table} SET pushed_hash = content_hash WHERE entity_uid = ?", (uid,))


def _api(website=RESTAURANTJI, **kw):
    return FakeAPI(entities={"businesses": [{"slug": SLUG, "id": 90, "name": CASCADE,
                                             "website": website, "address": "3411 Lake Tahoe Blvd"}]},
                   **kw)


def _row(conn, uid="b1", table="businesses"):
    return dict(conn.execute(f"SELECT * FROM {table} WHERE entity_uid = ?", (uid,)).fetchone())


def _enrich(conn, kind, uid):
    return ee.enrich_entity(conn, kind, uid, "South Lake Tahoe", ee.Budget(8)).status


def _website_patches(api):
    return [c for c in api.calls if c[0] == "PATCH" and "website" in c[3]]


# --------------------------------------------------------------------------
# Supervised
# --------------------------------------------------------------------------

class TestSupervised:
    def test_proposal_lists_the_restaurantji_business_and_changes_nothing(self, conn):
        _shipped(conn)
        api = _api()
        out = lwc.propose(conn, api)
        assert out["proposal"] == {"kind": "clear_listing_websites", "uids": ["business:b1"],
                                   "count": 1}
        assert out["preview"] == [{"target": "business:b1", "kind": "business", "name": CASCADE,
                                   "side": "both", "url": RESTAURANTJI, "live_slug": SLUG}]
        assert api.patches() == [] and api.creates() == []
        assert _row(conn)["website"] == RESTAURANTJI

    def test_no_clear_without_operator_confirmation(self, conn):
        _shipped(conn)
        api = _api()
        lwc.propose(conn, api)
        calls = len(api.calls)
        assert lwc.run(conn, api, enrich=_enrich)["applied"] == []
        assert len(api.calls) == calls            # not even a read
        assert _row(conn)["website"] == RESTAURANTJI
        assert api.tables["businesses"][SLUG]["website"] == RESTAURANTJI

    def test_confirmed_clear_goes_live_then_local_and_keeps_the_url_as_evidence(self, conn):
        _shipped(conn)
        api = _api()
        assert lwc.confirm(conn, ["business:b1"])["confirmed"] == 1
        [rep] = lwc.run(conn, api)["applied"]
        assert (rep["status"], rep["changed"], rep["reenrich"]) == ("cleared", "both", "queued")
        [patch] = _website_patches(api)
        assert patch[3]["website"] == "" and patch[3]["address"] == "3411 Lake Tahoe Blvd"
        assert not api.tables["businesses"][SLUG]["website"]
        row = _row(conn)
        assert row["website"] is None
        assert json.loads(row["extra_json"])["listing_pages"] == [RESTAURANTJI]
        assert row["enrichment_status"] == "pending"
        # In sync with live: the next entity-live-sync neither re-pushes nor resyncs.
        assert row["pushed_hash"] == row["content_hash"]
        assert not entity_sync.needs_sync(row)

    def test_later_resync_does_not_bring_the_listing_url_back(self, conn):
        _shipped(conn)
        api = _api()
        lwc.confirm(conn, ["business:b1"])
        lwc.run(conn, api)
        # A curator puts the listing back live; local has changed since.
        api.tables["businesses"][SLUG]["website"] = RESTAURANTJI
        conn.execute("UPDATE businesses SET phone = '530-555-0100' WHERE entity_uid = 'b1'")
        entity_sync.refresh_content_hash(conn, "business", "b1")
        assert entity_sync.sync_entity(api, conn, "business", "b1").status == "synced"
        assert _row(conn)["website"] is None

    def test_cleared_entity_is_re_enriched_and_its_own_site_reaches_live(self, conn, net):
        _shipped(conn)
        api = _api()
        lwc.confirm(conn, ["business:b1"])
        out = lwc.run(conn, api, enrich=_enrich)
        assert out["reenriched"] == [{"target": "business:b1", "reenrich": "enriched",
                                      "website": OWN_SITE}]
        assert net["search"]                      # searched past the listing page
        row = _row(conn)
        assert row["website"] == OWN_SITE and row["phone"] == "530-555-0100"
        entity_sync.sweep(api, conn)
        assert api.tables["businesses"][SLUG]["website"] == OWN_SITE
        [d] = lwc.ledger(conn)
        assert (d["changed"], d["reenrich"], d["website"]) == ("both", "enriched", OWN_SITE)

    def test_live_patch_that_does_not_land_parks_the_clear_and_leaves_local(self, conn):
        _shipped(conn)
        api = _api(ignore={"website"})
        lwc.confirm(conn, ["business:b1"])
        [rep] = lwc.run(conn, api)["applied"]
        assert (rep["status"], rep["changed"]) == ("failed", "none")
        assert "didn't land" in rep["note"]
        assert _row(conn)["website"] == RESTAURANTJI
        assert lwc.run(conn, api)["applied"] == []          # not retried blind
        assert lwc.confirm(conn, ["business:b1"])["confirmed"] == 1   # until re-confirmed

    def test_local_only_listing_takes_lives_own_site_without_a_patch(self, conn):
        _shipped(conn)
        api = _api(website=OWN_SITE)
        lwc.confirm(conn, ["business:b1"])
        [rep] = lwc.run(conn, api)["applied"]
        assert (rep["changed"], rep["reenrich"]) == ("local", "skipped")
        assert api.patches() == []
        assert _row(conn)["website"] == OWN_SITE

    def test_live_only_row_is_cleared_live(self, conn):
        api = _api()
        out = lwc.propose(conn, api)
        assert out["proposal"]["uids"] == ["business#90"]
        lwc.confirm(conn, out["proposal"]["uids"])
        [rep] = lwc.run(conn, api)["applied"]
        assert (rep["status"], rep["changed"], rep["url"]) == ("cleared", "live", RESTAURANTJI)
        assert not api.tables["businesses"][SLUG]["website"]

    def test_unread_live_side_is_never_proposed(self, conn, monkeypatch):
        from engine.townloops_api import TownloopsError
        _shipped(conn, slug="gone-elsewhere")
        api = _api(website=OWN_SITE)
        api.tables["businesses"][SLUG]["id"] = 91     # pairs by neither slug nor id

        def boom(resource, slug):
            raise TownloopsError(503, "unavailable", "down")
        monkeypatch.setattr(api, "get", boom)
        out = lwc.propose(conn, api)
        assert out["proposal"] is None
        assert out["skipped"][0]["why"].startswith("live side unread")

    def test_a_listing_row_that_is_gone_is_resolved_without_changes(self, conn):
        _shipped(conn)
        api = _api()
        lwc.confirm(conn, ["business:b1"])
        api.tables["businesses"][SLUG]["website"] = OWN_SITE
        conn.execute("UPDATE businesses SET website = ? WHERE entity_uid = 'b1'", (OWN_SITE,))
        [rep] = lwc.run(conn, api)["applied"]
        assert (rep["status"], rep["changed"]) == ("resolved", "none")
        assert api.patches() == []

    def test_mirror_of_a_live_listing_website_keeps_it_as_evidence_only(self, conn):
        from engine.derive_helpers import mirror_live_entity
        uid = mirror_live_entity(conn, "business", {"slug": SLUG, "id": 90, "name": CASCADE,
                                                    "website": RESTAURANTJI})
        row = _row(conn, uid)
        assert row["website"] is None
        assert json.loads(row["extra_json"]) == {"live_id": 90, "listing_pages": [RESTAURANTJI]}


def test_sync_never_fills_a_live_blank_with_a_listing_url(conn):
    _shipped(conn)
    api = _api(website=None)
    conn.execute("UPDATE businesses SET pushed_hash = NULL WHERE entity_uid = 'b1'")
    assert entity_sync.sync_entity(api, conn, "business", "b1").status == "synced"
    assert _website_patches(api) == []


@pytest.fixture
def dash_db(conn, tmp_state_dir, monkeypatch):
    import dashboard.db as dashboard_db
    monkeypatch.setattr(dashboard_db, "_DB_PATH", str(tmp_state_dir / "townloops.db"))
    conn.commit()
    return conn


def test_confirm_widget_confirms_the_clear(dash_db):
    from dashboard import chat_worker, intent_handlers
    _shipped(dash_db)
    proposal = {"kind": "clear_listing_websites", "uids": ["business:b1"], "count": 1}
    assert intent_handlers.validate_proposal(proposal) == (True, "")
    assert "clear_listing_websites" in chat_worker._ALLOWED_PROPOSAL_KINDS
    out = intent_handlers.execute({**proposal, "uids": ["business:b1", "business:nope"]})
    assert (out["confirmed"], out["skipped_uids"]) == (1, ["business:nope"])
    [d] = lwc.ledger(dash_db)
    assert d["status"] == "confirmed" and d["changed"] is None
    assert _row(dash_db)["website"] == RESTAURANTJI     # confirming changes nothing yet


def test_a_clear_another_run_already_made_is_skipped(conn):
    _shipped(conn)
    lwc.confirm(conn, ["business:b1"])
    conn.execute("UPDATE listing_website_clears SET status = 'cleared'")
    row = {"kind": "business", "entity_uid": "b1", "live_slug": SLUG, "url": RESTAURANTJI,
           "live_website": RESTAURANTJI, "name": CASCADE}
    assert lwc._finish(conn, "business:b1", "confirmed", "cleared", row=row) is None
    assert _row(conn)["website"] == RESTAURANTJI       # the local clear rolled back with it


def test_cli_migrates_before_it_reads_the_ledger(tmp_state_dir, monkeypatch, capsys):
    from tests.autonomy_town import SKILLS, load
    monkeypatch.setattr("engine.derive_helpers._DB_PATH", tmp_state_dir / "townloops.db")
    mod = load(SKILLS / "listing-website-cleanup" / "cleanup.py", "lwc_cli")
    assert mod.main(["list"]) == 0
    assert json.loads(capsys.readouterr().out) == []


# --------------------------------------------------------------------------
# Autonomous: local cleared, live flagged — never PATCHed, never submitted
# --------------------------------------------------------------------------

@pytest.fixture
def town(fresh_db, tmp_state_dir, monkeypatch):
    fresh_db.row_factory = sqlite3.Row
    t = Town(tmp_state_dir, monkeypatch, fresh_db,
             api=FakeAPI(entity_status="pending", event_status="pending",
                         entities={"businesses": [{"slug": SLUG, "id": 90, "name": CASCADE,
                                                   "website": RESTAURANTJI,
                                                   "status": "published"}]}))
    _shipped(fresh_db)
    fresh_db.commit()
    return t


def _apply(town, *argv):
    import contextlib
    import io
    from tests.autonomy_town import SKILLS, load
    mod = load(SKILLS / "listing-website-cleanup" / "cleanup.py", "town_lwc")
    town.conn.commit()
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        assert mod.main(["apply", *argv]) == 0
    return json.loads(out.getvalue())


def _digest_live(town):
    from engine import moderation
    return moderation.digest(town.conn, "2000-01-01")["listing_websites_live"]


class TestAutonomous:
    def test_clears_local_and_flags_live_without_touching_townloops(self, town):
        out = _apply(town)
        [rep] = out["flagged"]
        assert (rep["status"], rep["changed"], rep["reenrich"]) == ("flagged", "local", "queued")
        assert lwc.LIVE_FIX_NOTE in rep["note"]
        assert town.api.creates() == [] and town.api.patches() == []
        assert town.api.tables["businesses"][SLUG]["website"] == RESTAURANTJI
        row = _row(town.conn)
        assert row["website"] is None and row["enrichment_status"] == "pending"
        assert json.loads(row["extra_json"])["listing_pages"] == [RESTAURANTJI]
        assert _digest_live(town) == [f"{CASCADE} ({SLUG}): {RESTAURANTJI}"]
        [d] = town.decisions("listing-website")
        assert d["verdict"] == "flag-live"

    def test_re_enrichment_finds_the_own_site_and_resync_keeps_the_listing_out(
            self, town, net, monkeypatch):
        monkeypatch.setenv("TOWNLOOPS_ENRICH_MAX_LOOKUPS", "8")
        out = _apply(town)
        assert out["reenriched"] == [{"target": "business:b1", "reenrich": "enriched",
                                      "website": OWN_SITE}]
        town.live_sync()
        assert _row(town.conn)["website"] == OWN_SITE      # live's listing not pulled back
        # Live isn't blank, so the own site doesn't fill it: a full-body PATCH
        # (the phone fill) only resends live's own website.
        assert {p[3]["website"] for p in _website_patches(town.api)} <= {RESTAURANTJI}
        assert town.api.tables["businesses"][SLUG]["website"] == RESTAURANTJI

    def test_flag_stays_until_live_is_fixed_and_is_recorded_once(self, town):
        _apply(town)
        _apply(town)
        assert len(town.decisions("listing-website")) == 1
        assert len(_digest_live(town)) == 1
        town.api.tables["businesses"][SLUG]["website"] = OWN_SITE   # fixed in admin
        [rep] = _apply(town)["resolved"]
        assert (rep["status"], rep["changed"]) == ("resolved", "none")
        assert _digest_live(town) == []

    def test_local_only_listing_is_cleared_not_flagged(self, town):
        town.api.tables["businesses"][SLUG]["website"] = OWN_SITE
        out = _apply(town)
        assert out["flagged"] == [] and out["cleared"][0]["changed"] == "local"
        assert _row(town.conn)["website"] == OWN_SITE
        assert _digest_live(town) == []

    def test_supervised_widget_and_proposer_are_inactive(self, town, capsys):
        from tests.autonomy_town import SKILLS, load
        mod = load(SKILLS / "listing-website-cleanup" / "cleanup.py", "town_lwc")
        assert mod.main(["propose-clear"]) == 0
        assert json.loads(capsys.readouterr().out)["inactive"] is True
        out = lwc.confirm(town.conn, ["business:b1"])
        assert out["inactive"] is True and out["confirmed"] == 0
        assert lwc.ledger(town.conn) == []

    def test_entity_live_sync_does_not_run_the_cleanup(self, town):
        out = town.live_sync()
        assert "listing_websites" not in out
        assert lwc.ledger(town.conn) == []
        assert _row(town.conn)["website"] == RESTAURANTJI
