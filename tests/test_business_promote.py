"""Operator-gated promote of business candidates (engine/business_promote.py).

A verified candidate ships only after the operator approves it (a status
flip to `approved`, from the dashboard button or a chat Confirm widget);
the promote run then checks live again, creates the local business,
enriches it and ships it through the standard entity ship path
(engine/entity_ship — live lookup first). A declined candidate is
remembered — by URL and by name — and never proposed again. TownLoops is
the in-memory FakeAPI; enrichment is an injected stub."""

from __future__ import annotations

import json

import pytest

from engine import business_discovery as bd
from engine import business_promote as bp
from engine import discovery
from engine.discovery import Candidate
from tests.fake_townloops import FakeAPI


LIVE = [{"slug": "cuppa-tahoe", "name": "Cuppa Tahoe", "website": "https://www.cuppatahoe.com/"}]


def _seed(conn, url, name, verdict=None):
    """A swept + verified candidate; returns its uid."""
    c = Candidate(canonical_url=discovery.canonicalize_url(url), display_name=name,
                  source_signal="web-search:bakery")
    bd.record_sweep(conn, "slt", [c], bd.LiveIndex.build([]))
    row = bd.pending_verify(conn, "slt", limit=50)[-1]
    verdict = verdict or {"verified": True, "reasons": ["in-town"], "address": "1 Main St",
                          "phone": "530-555-0100", "hours": "7-3"}
    bd.record_verdict(conn, row, verdict, bd.LiveIndex.build([]))
    return row["candidate_uid"]


def _cand(conn, uid):
    return dict(conn.execute("SELECT * FROM discovery_candidates WHERE candidate_uid = ?",
                             (uid,)).fetchone())


def _business(conn, entity_uid):
    return dict(conn.execute("SELECT * FROM businesses WHERE entity_uid = ?",
                             (entity_uid,)).fetchone())


def _enrich_filling(**fields):
    from engine import entity_enrich
    calls = []

    def enrich(conn, entity_uid):
        calls.append(entity_uid)
        entity_enrich.fill_blanks(conn, "business", entity_uid, fields, "enriched")
        return "enriched"
    enrich.calls = calls
    return enrich


@pytest.fixture
def api():
    return FakeAPI(entities={"businesses": LIVE})


# ---------------------------------------------------------------- approval gate

def test_unapproved_candidates_never_ship(fresh_db, api):
    _seed(fresh_db, "https://brand-new.example/", "Brand New Bakery")
    out = bp.promote_approved(fresh_db, api, "slt", enrich=_enrich_filling())
    assert out["processed"] == 0
    assert api.creates() == []
    assert fresh_db.execute("SELECT COUNT(*) FROM businesses").fetchone()[0] == 0


def test_promote_run_with_nothing_approved_does_not_call_the_api(fresh_db, api):
    _seed(fresh_db, "https://brand-new.example/", "Brand New Bakery")
    bp.promote_approved(fresh_db, api, "slt", enrich=_enrich_filling())
    assert api.calls == []


def test_approve_only_takes_verified_business_candidates(fresh_db):
    ok = _seed(fresh_db, "https://brand-new.example/", "Brand New Bakery")
    bad = _seed(fresh_db, "https://closed.example/", "Closed Cafe",
                {"verified": False, "reasons": ["closure marker"]})
    out = bp.approve(fresh_db, [ok, bad, "no-such-uid"])
    assert out["approved"] == 1
    assert sorted(out["skipped_uids"]) == sorted([bad, "no-such-uid"])
    assert _cand(fresh_db, ok)["status"] == "approved"
    assert _cand(fresh_db, bad)["status"] == "rejected"


# ---------------------------------------------------------------- approve → ship

def test_approved_candidate_is_promoted_enriched_and_shipped(fresh_db, api):
    uid = _seed(fresh_db, "https://brand-new.example/", "Brand New Bakery")
    bp.approve(fresh_db, [uid])
    enrich = _enrich_filling(description="Sourdough by the lake.")

    out = bp.promote_approved(fresh_db, api, "slt", enrich=enrich)

    assert out["promoted"] == 1
    [post] = api.creates()
    assert post["name"] == "Brand New Bakery"
    assert post["website"] == "https://brand-new.example/"
    assert post["address"] == "1 Main St" and post["phone"] == "530-555-0100"
    assert post["description"] == "Sourdough by the lake."   # enriched before shipping
    cand = _cand(fresh_db, uid)
    assert cand["status"] == "promoted"
    assert cand["live_slug"] == api.created_slug("businesses") and "slug" not in post
    biz = _business(fresh_db, cand["research_entity_uid"])
    assert biz["townloops_slug"] == api.created_slug("businesses")
    assert biz["pushed_hash"] == biz["content_hash"]
    assert json.loads(biz["extra_json"])["candidate_uid"] == uid
    assert enrich.calls == [cand["research_entity_uid"]]


def test_a_candidate_that_went_live_after_approval_is_not_posted(fresh_db, api):
    uid = _seed(fresh_db, "https://cuppa-tahoe.example/", "Cuppa Tahoe")
    # not live when swept (empty index in _seed); live by the time it's promoted
    bp.approve(fresh_db, [uid])
    out = bp.promote_approved(fresh_db, api, "slt", enrich=_enrich_filling())
    assert out["already_live"] == 1
    assert api.creates() == []
    cand = _cand(fresh_db, uid)
    assert (cand["status"], cand["live_slug"]) == ("already-live", "cuppa-tahoe")
    assert fresh_db.execute("SELECT COUNT(*) FROM businesses").fetchone()[0] == 0


def test_a_facebook_page_ships_as_the_facebook_link_not_the_website(fresh_db, api):
    uid = _seed(fresh_db, "https://www.facebook.com/brandnewbakery", "Brand New Bakery")
    bp.approve(fresh_db, [uid])
    bp.promote_approved(fresh_db, api, "slt", enrich=_enrich_filling())
    [post] = api.creates()
    assert "website" not in post
    assert post["facebook"].endswith("facebook.com/brandnewbakery")


def test_a_listing_page_candidate_ships_without_a_website(fresh_db, api):
    uid = _seed(fresh_db, "https://www.restaurantji.com/ca/south-lake-tahoe/brand-new-bakery-/",
                "Brand New Bakery")
    bp.approve(fresh_db, [uid])
    bp.promote_approved(fresh_db, api, "slt", enrich=_enrich_filling())
    [post] = api.creates()
    assert "website" not in post


def test_a_failed_ship_stays_approved_and_the_retry_ships_once(fresh_db, api):
    uid = _seed(fresh_db, "https://brand-new.example/", "Brand New Bakery")
    bp.approve(fresh_db, [uid])
    real_create = api.create

    def down(*a, **k):
        raise RuntimeError("503 service unavailable")
    api.create = down
    out = bp.promote_approved(fresh_db, api, "slt", enrich=_enrich_filling())
    assert out["failed"] == 1 and out["errors"]
    assert _cand(fresh_db, uid)["status"] == "approved"

    api.create = real_create
    out = bp.promote_approved(fresh_db, api, "slt", enrich=_enrich_filling())
    assert out["promoted"] == 1
    assert len([c for c in api.calls if c[0] == "POST"]) == 1
    assert fresh_db.execute("SELECT COUNT(*) FROM businesses").fetchone()[0] == 1


def test_deferred_enrichment_waits_for_the_next_run(fresh_db, api):
    uid = _seed(fresh_db, "https://brand-new.example/", "Brand New Bakery")
    bp.approve(fresh_db, [uid])
    out = bp.promote_approved(fresh_db, api, "slt", enrich=lambda conn, e: "deferred")
    assert out["deferred"] == 1
    assert api.creates() == []
    assert _cand(fresh_db, uid)["status"] == "approved"


def test_an_unshipped_local_business_of_the_same_name_is_reused(fresh_db, api):
    from engine.derive_helpers import insert_entity
    insert_entity(fresh_db, "businesses", "local-biz",
                  {"source": "derived", "source_kind": "derived", "name": "Brand New Bakery"})
    uid = _seed(fresh_db, "https://brand-new.example/", "Brand New Bakery")
    bp.approve(fresh_db, [uid])
    bp.promote_approved(fresh_db, api, "slt", enrich=_enrich_filling())
    assert _cand(fresh_db, uid)["research_entity_uid"] == "local-biz"
    assert fresh_db.execute("SELECT COUNT(*) FROM businesses").fetchone()[0] == 1
    assert _business(fresh_db, "local-biz")["website"] == "https://brand-new.example/"


# ---------------------------------------------------------------- decline memory

def test_declined_candidate_is_never_reproposed_by_url_or_name(fresh_db):
    uid = _seed(fresh_db, "https://brand-new.example/", "Brand New Bakery")
    assert bp.decline(fresh_db, [uid], note="chain, not local")["declined"] == 1
    cand = _cand(fresh_db, uid)
    assert cand["status"] == "rejected"
    assert cand["reject_reason"].startswith("operator")

    again = [Candidate(canonical_url="https://brand-new.example/", display_name="Brand New Bakery",
                       source_signal="directory:x"),
             Candidate(canonical_url="https://brandnewbakery.example/", display_name="Brand New Bakery!",
                       source_signal="web-search:bakery")]
    bd.record_sweep(fresh_db, "slt", again, bd.LiveIndex.build([]))
    assert bd.list_candidates(fresh_db, "slt") == []
    other = bd.list_candidates(fresh_db, "slt", status="rejected")
    assert len(other) == 2
    assert any("declined" in c["reject_reason"] for c in other if c["candidate_uid"] != uid)


def test_a_directory_candidate_named_at_verify_after_a_decline_is_rejected(fresh_db):
    uid = _seed(fresh_db, "https://brand-new.example/", "Brand New Bakery")
    bp.decline(fresh_db, [uid])
    c = Candidate(canonical_url="https://bnb.example/", display_name=None, source_signal="directory:x")
    bd.record_sweep(fresh_db, "slt", [c], bd.LiveIndex.build([]))
    row = bd.pending_verify(fresh_db, "slt", limit=1)[0]
    verdict = {"verified": True, "reasons": [], "title": "Home | Brand New Bakery"}
    assert bd.record_verdict(fresh_db, row, verdict, bd.LiveIndex.build([])) == "rejected"


def test_a_verifier_rejection_is_not_a_decline_memory(fresh_db):
    _seed(fresh_db, "https://closed.example/", "Brand New Bakery",
          {"verified": False, "reasons": ["closure marker"]})
    c = Candidate(canonical_url="https://brand-new.example/", display_name="Brand New Bakery",
                  source_signal="web-search:bakery")
    bd.record_sweep(fresh_db, "slt", [c], bd.LiveIndex.build([]))
    assert [r["canonical_url"] for r in bd.list_candidates(fresh_db, "slt")] == \
        ["https://brand-new.example/"]


def test_a_declined_candidate_cannot_be_approved_or_promoted(fresh_db, api):
    uid = _seed(fresh_db, "https://brand-new.example/", "Brand New Bakery")
    bp.approve(fresh_db, [uid])
    bp.decline(fresh_db, [uid])           # operator changed their mind before the run
    assert bp.approve(fresh_db, [uid])["approved"] == 0
    bp.promote_approved(fresh_db, api, "slt", enrich=_enrich_filling())
    assert api.creates() == []


# ---------------------------------------------------------------- proposals

def test_propose_promote_is_read_only_and_names_the_uids(fresh_db):
    a = _seed(fresh_db, "https://brand-new.example/", "Brand New Bakery")
    b = _seed(fresh_db, "https://pie.example/", "Lakeside Pie")
    out = bp.propose(fresh_db, "slt", "promote")
    assert out["proposal"]["kind"] == "promote_businesses"
    assert sorted(out["proposal"]["uids"]) == sorted([a, b])
    assert out["proposal"]["count"] == 2
    assert {p["name"] for p in out["preview"]} == {"Brand New Bakery", "Lakeside Pie"}
    assert {_cand(fresh_db, u)["status"] for u in (a, b)} == {"verified"}


def test_propose_by_uid_prefix_and_decline_kind(fresh_db):
    a = _seed(fresh_db, "https://brand-new.example/", "Brand New Bakery")
    _seed(fresh_db, "https://pie.example/", "Lakeside Pie")
    out = bp.propose(fresh_db, "slt", "decline", uids=[a[:10]])
    assert out["proposal"] == {"kind": "decline_businesses", "uids": [a], "count": 1}


def test_propose_with_no_match_has_no_proposal(fresh_db):
    out = bp.propose(fresh_db, "slt", "promote")
    assert out["proposal"] is None and out["preview"] == []


# ---------------------------------------------------------------- confirm widget

@pytest.fixture
def dash_db(fresh_db, tmp_state_dir, monkeypatch):
    import dashboard.db as dashboard_db
    monkeypatch.setattr(dashboard_db, "_DB_PATH", str(tmp_state_dir / "townloops.db"))
    return fresh_db


def test_confirm_widget_approves_and_declines_candidates(dash_db):
    from dashboard import chat_worker, intent_handlers
    a = _seed(dash_db, "https://brand-new.example/", "Brand New Bakery")
    b = _seed(dash_db, "https://pie.example/", "Lakeside Pie")
    for kind in ("promote_businesses", "decline_businesses"):
        assert intent_handlers.validate_proposal({"kind": kind, "uids": [a]}) == (True, "")
        assert kind in chat_worker._ALLOWED_PROPOSAL_KINDS

    out = intent_handlers.execute({"kind": "promote_businesses", "uids": [a]})
    assert out["approved"] == 1
    out = intent_handlers.execute({"kind": "decline_businesses", "uids": [b]})
    assert out["declined"] == 1
    assert _cand(dash_db, a)["status"] == "approved"
    assert _cand(dash_db, b)["status"] == "rejected"


def test_dashboard_row_buttons_promote_and_never_reject_a_shipped_candidate(dash_db, api):
    import dashboard.db as dashboard_db
    uid = _seed(dash_db, "https://brand-new.example/", "Brand New Bakery")
    assert dashboard_db.promote_candidate(uid) == 1
    assert dashboard_db.promote_candidate(uid) == 0          # already approved
    bp.promote_approved(dash_db, api, "slt", enrich=_enrich_filling())
    assert dashboard_db.reject_candidate(uid) == 0
    assert _cand(dash_db, uid)["status"] == "promoted"


def test_a_decline_during_the_run_stops_the_ship(fresh_db, api):
    uid = _seed(fresh_db, "https://brand-new.example/", "Brand New Bakery")
    bp.approve(fresh_db, [uid])

    def enrich_then_operator_declines(conn, entity_uid):
        bp.decline(conn, [uid])
        return "enriched"
    out = bp.promote_approved(fresh_db, api, "slt", enrich=enrich_then_operator_declines)
    assert api.creates() == []
    assert out["promoted"] == 0
    assert _cand(fresh_db, uid)["status"] == "rejected"


def test_candidates_that_keep_failing_do_not_block_later_approvals(fresh_db, api):
    stuck = [_seed(fresh_db, f"https://stuck{i}.example/", f"Stuck Shop {i}") for i in range(3)]
    later = _seed(fresh_db, "https://brand-new.example/", "Brand New Bakery")
    bp.approve(fresh_db, [*stuck, later])
    defer_stuck = lambda conn, e: "deferred" if "Stuck" in _business(conn, e)["name"] else "enriched"
    bp.promote_approved(fresh_db, api, "slt", enrich=defer_stuck, limit=3)
    out = bp.promote_approved(fresh_db, api, "slt", enrich=defer_stuck, limit=3)
    assert out["promoted"] == 1
    assert _cand(fresh_db, later)["status"] == "promoted"


def test_declining_one_row_declines_its_same_name_twins(fresh_db):
    site = _seed(fresh_db, "https://brand-new.example/", "Brand New Bakery")
    fb = _seed(fresh_db, "https://www.facebook.com/brandnewbakery", "Brand New Bakery")
    other = _seed(fresh_db, "https://pie.example/", "Lakeside Pie")
    out = bp.decline(fresh_db, [site])
    assert out["declined"] == 1 and out["twins_declined"] == 1
    assert _cand(fresh_db, fb)["status"] == "rejected"
    assert _cand(fresh_db, other)["status"] == "verified"
    assert bp.propose(fresh_db, "slt", "promote")["proposal"]["uids"] == [other]


def test_an_approved_candidate_matching_a_live_venue_is_never_promoted(fresh_db):
    uid = _seed(fresh_db, "https://cascade.example/", "Cascade Kitchens")
    bp.approve(fresh_db, [uid])
    api = FakeAPI(entities={"businesses": LIVE},
                  venues=[{"slug": "cascade-kitchens", "name": "Cascade Kitchens", "website": None}])
    out = bp.promote_approved(fresh_db, api, "slt", enrich=_enrich_filling())
    assert out["already_live"] == 1 and out["promoted"] == 0
    assert api.creates() == []
    cand = _cand(fresh_db, uid)
    assert (cand["status"], cand["live_slug"]) == ("already-live", "cascade-kitchens")
    assert "live venue cascade-kitchens" in cand["reject_reason"]
