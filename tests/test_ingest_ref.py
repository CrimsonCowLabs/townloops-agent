"""engine/ingest_ref — the agent's identity key on TownLoops (#63)."""

from engine import ingest_ref
from engine.townloops_api import TownloopsError


class FakeClient:
    def __init__(self, rows=(), patch_error=None):
        self.rows = list(rows)
        self.patch_error = patch_error
        self.patches = []
        self.lists = []

    def list(self, resource, **params):
        self.lists.append((resource, params))
        return [r for r in self.rows if r.get("ingest_ref") == params.get("ingest_ref")]

    def update(self, resource, slug, payload):
        self.patches.append((resource, slug, payload))
        if self.patch_error:
            raise self.patch_error
        return {"slug": slug, **payload}


def test_refs_are_town_scoped_and_kept_apart(monkeypatch):
    monkeypatch.setenv("TOWNLOOPS_TOWN_SLUG", "slt")
    uid = "9a2008bc5fffa495deadbeef"
    assert ingest_ref.ref_for(uid) == "hermes-slt-9a2008bc5fffa495"
    assert len({ingest_ref.ref_for(uid), ingest_ref.change_ref_for(uid),
                ingest_ref.cancel_ref_for(uid)}) == 3


def test_taken_slug_reads_only_an_ingest_ref_conflict():
    taken = TownloopsError(409, "ingest_ref_taken", "x",
                           {"ingest_ref": "hermes-slt-1", "slug": "jazz-2026-08-01"})
    assert ingest_ref.taken_slug(taken) == "jazz-2026-08-01"
    assert ingest_ref.taken_slug(TownloopsError(409, "slug_taken", "x", {"slug": "a"})) is None
    assert ingest_ref.taken_slug(TownloopsError(409, "ingest_ref_taken", "x", {})) is None
    assert ingest_ref.taken_slug(RuntimeError("409")) is None


def test_find_by_ref_is_one_filtered_list_read():
    c = FakeClient([{"slug": "a", "ingest_ref": "r1"}, {"slug": "b", "ingest_ref": None}])
    assert ingest_ref.find_by_ref(c, "events", "r1")["slug"] == "a"
    assert ingest_ref.find_by_ref(c, "events", "r2") is None
    assert ingest_ref.find_by_ref(c, "events", None) is None
    assert [p["ingest_ref"] for _, p in c.lists] == ["r1", "r2"]


def test_adopt_sets_a_missing_ref_once():
    c = FakeClient()
    assert ingest_ref.adopt(c, "venues", {"slug": "the-mill"}, "r").status == "adopted"
    assert c.patches == [("venues", "the-mill", {"ingest_ref": "r"})]
    assert ingest_ref.adopt(c, "venues", {"slug": "m", "ingest_ref": "r"}, "r").status == "already"
    assert ingest_ref.adopt(c, "venues", {"slug": "m", "ingest_ref": "z"}, "r").status == "other-ref"
    assert len(c.patches) == 1


def test_adopt_reports_conflicts_without_raising():
    taken = TownloopsError(409, "ingest_ref_taken", "x", {"slug": "dup"})
    assert ingest_ref.adopt(FakeClient(patch_error=taken), "events", {"slug": "a"}, "r").status == "taken"
    refused = TownloopsError(422, "validation_error", "x", {"field": "ingest_ref"})
    assert ingest_ref.adopt(FakeClient(patch_error=refused), "events", {"slug": "a"}, "r").status == "other-ref"
    assert ingest_ref.adopt(FakeClient(patch_error=RuntimeError("boom")), "events",
                            {"slug": "a"}, "r").status == "error"
