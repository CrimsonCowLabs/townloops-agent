"""Town-local seed adapters: a town agent writes `$HERMES_HOME/adapters/<name>.py`
exposing an `ADAPTER`, and every place an adapter name is resolved
(engine/seed_adapters `registry`) sees it beside the shared ones — a shared
adapter of the same name wins, a broken file is skipped and named. Plus the
no-network fixture runner (`event-seed-discovery test-adapter`)."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import sqlite3
import textwrap
from pathlib import Path

import pytest

from engine import autonomy, seed_adapters, seeds

SKILLS = Path(__file__).resolve().parent.parent / "skills" / "business"
CASES = Path(__file__).parent / "fixtures" / "adapters"

CLOCK = '''
from engine.seed_adapters import Adapter, Param, AdapterError, html_text, local_time


def _positive(v):
    return None if isinstance(v, int) and v > 0 else "must be above 0"


def read(seed, config, get_json):
    body = get_json(config.get("url") or seed["url"], {"page": 1})
    return [{"title": html_text(r["name"]), "date": local_time(None, r["when"]),
             "end_at": None, "venue": None, "location": None, "description": None,
             "categories": [], "tags": [], "source_url": r.get("link")}
            for r in body["rows"][:config.get("limit", 99)]]


ADAPTER = Adapter(read, {"limit": Param("rows to keep", _positive),
                         "url": Param("endpoint", lambda v: None)}, feed_types=("clockfeed",))
'''

FEED = {"rows": [{"name": "Story &amp; Time", "when": "2027-02-01 10:00:00",
                  "link": "https://lib.example/e/1"},
                 {"name": "Chess Club", "when": "2027-02-02 16:00:00"}]}


@pytest.fixture
def home(tmp_path, monkeypatch):
    """$HERMES_HOME at a tmp path; returns a writer for adapters/<file>."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    adapters = tmp_path / "adapters"
    adapters.mkdir()

    def write(filename: str, source: str) -> Path:
        path = adapters / filename
        path.write_text(textwrap.dedent(source))
        return path
    write.dir = adapters
    return write


def seed(**kw) -> dict:
    return {"seed_id": 7, "name": "Library", "url": "https://lib.example/api",
            "adapter": "clock", "adapter_config": None, "feed_type": None, "feed_url": None, **kw}


def feed(url, params):
    return FEED


class TestLoading:
    def test_no_adapters_dir_means_the_shared_ones(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        assert set(seed_adapters.registry()) == set(seed_adapters.ADAPTERS)
        assert seed_adapters.town_adapters().errors == {}

    def test_a_town_adapter_reads_a_seed(self, home):
        home("clock.py", CLOCK)
        listing = seed_adapters.read(seed(), get_json=feed)
        assert [e["title"] for e in listing.events] == ["Story & Time", "Chess Club"]
        assert listing.events[0]["date"] == "2027-02-01T10:00:00"
        assert "Story & Time" in listing.page

    def test_its_params_are_checked(self, home):
        home("clock.py", CLOCK)
        seed_adapters.check_config("clock", {"limit": 1})
        with pytest.raises(ValueError, match="limit"):
            seed_adapters.check_config("clock", {"limit": 0})
        with pytest.raises(ValueError, match="nope"):
            seed_adapters.check_config("clock", {"nope": 1})
        events = seed_adapters.read(seed(adapter_config='{"limit": 1}'), get_json=feed).events
        assert len(events) == 1

    def test_its_feed_types_name_it(self, home):
        home("clock.py", CLOCK)
        assert seed_adapters.for_feed("clockfeed") == "clock"
        assert seed_adapters.for_feed("tribe") == "tribe"

    def test_an_edited_file_is_loaded_again(self, home):
        path = home("clock.py", CLOCK)
        seed_adapters.read(seed(), get_json=feed)
        path.write_text(textwrap.dedent(CLOCK).replace("html_text(r[\"name\"])",
                                                       "r[\"name\"].upper()") + "\n# v2\n")
        assert seed_adapters.read(seed(), get_json=feed).events[1]["title"] == "CHESS CLUB"


class TestShadowing:
    def test_a_shared_adapter_of_the_same_name_wins(self, home):
        home("tribe.py", CLOCK)
        assert seed_adapters.registry()["tribe"] is seed_adapters.ADAPTERS["tribe"]
        town = seed_adapters.town_adapters()
        assert town.shadowed == ["tribe"]
        assert "tribe" not in town.adapters
        with pytest.raises(ValueError, match="limit"):        # the shared one's params
            seed_adapters.check_config("tribe", {"limit": 1})

    def test_shadowing_is_logged(self, home, monkeypatch):
        logged = []
        monkeypatch.setattr(seed_adapters._log, "warning", lambda msg, **k: logged.append(msg))
        home("showare.py", CLOCK)
        seed_adapters.registry()
        assert any("shadowed" in m for m in logged)

    def test_a_shared_feed_type_stays_with_the_shared_adapter(self, home):
        home("mytribe.py", CLOCK.replace('("clockfeed",)', '("tribe",)'))
        assert seed_adapters.for_feed("tribe") == "tribe"


class TestBadFiles:
    @pytest.mark.parametrize("filename,source,why", [
        ("broken.py", "def read(:\n", "SyntaxError"),
        ("crashes.py", "raise RuntimeError('boom')\n", "boom"),
        ("exits.py", "import sys\nsys.exit(3)\n", "SystemExit"),
        ("noadapter.py", "X = 1\n", "no ADAPTER"),
        ("notadapter.py", "ADAPTER = {'read': None}\n", "not an engine.seed_adapters.Adapter"),
        ("Bad-Name.py", CLOCK, "name"),
        ("generic.py", CLOCK, "generic"),
    ])
    def test_a_bad_file_is_skipped_and_named(self, home, filename, source, why):
        home(filename, source)
        home("clock.py", CLOCK)
        town = seed_adapters.town_adapters()
        assert filename in town.errors and why in town.errors[filename]
        reg = seed_adapters.registry()
        assert {"tribe", "showare", "clock"} <= set(reg)
        assert seed_adapters.read(seed(), get_json=feed).events

    def test_a_bad_adapter_value_is_refused(self, home):
        home("badparams.py", "from engine.seed_adapters import Adapter\n"
                             "ADAPTER = Adapter(lambda s, c, g: [], {'x': 1})\n")
        assert "Param" in seed_adapters.town_adapters().errors["badparams.py"]

    def test_a_seed_naming_a_broken_adapter_is_told_why(self, home):
        home("clock.py", "def read(:\n")
        with pytest.raises(seed_adapters.AdapterError, match="clock.py.*SyntaxError"):
            seed_adapters.read(seed(), get_json=feed)

    def test_helper_and_non_python_files_are_ignored(self, home):
        home("_helpers.py", "raise RuntimeError('not an adapter')\n")
        home("notes.md", "# notes\n")
        assert seed_adapters.town_adapters().errors == {}


class TestReadFailures:
    def test_a_reader_that_raises_is_an_adapter_error(self, home):
        home("clock.py", CLOCK)
        with pytest.raises(seed_adapters.AdapterError, match="KeyError"):
            seed_adapters.read(seed(), get_json=lambda url, params: {})

    @pytest.mark.parametrize("returned", ["None", "{'a': 1}", "[1, 2]"])
    def test_a_reader_returning_no_event_list_is_an_adapter_error(self, home, returned):
        home("odd.py", "from engine.seed_adapters import Adapter\n"
                       f"ADAPTER = Adapter(lambda s, c, g: {returned}, {{}})\n")
        with pytest.raises(seed_adapters.AdapterError, match="list of event objects"):
            seed_adapters.read(seed(adapter="odd"), get_json=feed)


class TestSetAdapter:
    @pytest.fixture
    def conn(self, fresh_db, home):
        fresh_db.row_factory = sqlite3.Row
        fresh_db.execute(
            "INSERT INTO event_seeds (name, url, kind, status, first_seen_at, last_seen_at) "
            "VALUES ('Library', 'https://lib.example/api', 'library', 'approved', 'x', 'x')")
        fresh_db.commit()
        home("clock.py", CLOCK)
        return fresh_db

    def _set(self, conn, adapter, config=None):
        row = dict(conn.execute("SELECT * FROM event_seeds").fetchone())
        return seeds.set_adapter(conn, row, adapter, config, autonomy.Settings(mode="autonomous"),
                                 read_adapter=lambda s: seed_adapters.read(s, get_json=feed))

    def test_a_town_adapter_is_set_after_its_trial(self, conn):
        out = self._set(conn, "clock", {"limit": 1})
        assert (out.adapter, out.trial_events) == ("clock", 1)
        row = conn.execute("SELECT adapter, adapter_config FROM event_seeds").fetchone()
        assert tuple(row) == ("clock", '{"limit": 1}')

    def test_its_params_still_apply_and_unknown_names_list_it(self, conn):
        with pytest.raises(ValueError, match="limit"):
            self._set(conn, "clock", {"limit": -1})
        with pytest.raises(ValueError, match="clock"):
            self._set(conn, "nope")

    def test_a_trial_that_raises_leaves_the_seed_unchanged(self, conn, home):
        home("clock.py", CLOCK.replace("body = get_json", "1 / 0\n    body = get_json"))
        with pytest.raises(ValueError, match="ZeroDivisionError"):
            self._set(conn, "clock")
        assert conn.execute("SELECT adapter FROM event_seeds").fetchone()[0] is None

    def test_review_assigns_a_town_adapter_for_its_feed_type(self, conn):
        conn.execute("UPDATE event_seeds SET feed_type = 'clockfeed'")
        conn.commit()
        out = seeds.assign_adapters(conn, autonomy.Settings(mode="autonomous"),
                                    read_adapter=lambda s: seed_adapters.read(s, get_json=feed),
                                    limit=2)
        assert out["adapters_assigned"] == 1
        assert conn.execute("SELECT adapter FROM event_seeds").fetchone()[0] == "clock"


# ---------------------------------------------------------------------------
# The fixture runner
# ---------------------------------------------------------------------------

def _discover():
    path = SKILLS / "event-seed-discovery" / "discover.py"
    spec = importlib.util.spec_from_file_location("town_adapters_discover", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def cli(*argv) -> tuple[int, str]:
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = _discover().main(list(argv))
    return rc, out.getvalue()


def _last(out: str) -> dict:
    return json.loads(out.strip().splitlines()[-1])


class TestFixtureFeed:
    def test_a_file_answers_every_request(self, tmp_path):
        (tmp_path / "r.json").write_text(json.dumps(FEED))
        f = seed_adapters.FixtureFeed(tmp_path / "r.json")
        assert f("https://a.example", {"page": 1}) == FEED == f("https://b.example", {})
        assert [r["url"] for r in f.requests] == ["https://a.example", "https://b.example"]

    def test_a_dir_answers_each_page(self, tmp_path):
        for n in (1, 2):
            (tmp_path / f"page-{n}.json").write_text(json.dumps({"n": n}))
        f = seed_adapters.FixtureFeed(tmp_path)
        assert f("u", {}) == {"n": 1} and f("u", {"page": 2}) == {"n": 2}
        with pytest.raises(seed_adapters.AdapterError, match="page-3.json"):
            f("u", {"page": 3})


class TestTestAdapterCli:
    @pytest.fixture
    def case(self, home, tmp_path):
        home("clock.py", CLOCK)
        d = tmp_path / "case"
        d.mkdir()
        (d / "fixture.json").write_text(json.dumps(FEED))
        (d / "seed.json").write_text(json.dumps({"url": "https://lib.example/api"}))
        return d

    def test_prints_the_events(self, case):
        rc, out = cli("test-adapter", "--adapter", "clock", "--fixture", str(case / "fixture.json"))
        got = _last(out)
        assert rc == 0 and [e["title"] for e in got["events"]] == ["Story & Time", "Chess Club"]
        assert got["requests"][0]["url"] == "https://fixture.invalid/"

    def test_expected_passes_and_a_mismatch_fails_with_a_diff(self, case):
        rc, out = cli("test-adapter", "--adapter", "clock", "--case", str(case))
        events = _last(out)["events"]
        (case / "expected.json").write_text(json.dumps(events))
        rc, out = cli("test-adapter", "--adapter", "clock", "--case", str(case))
        assert rc == 0 and _last(out)["pass"] is True
        events[1]["title"] = "Checkers Club"
        (case / "expected.json").write_text(json.dumps(events))
        rc, out = cli("test-adapter", "--adapter", "clock", "--case", str(case))
        got = _last(out)
        assert rc == 1 and got["pass"] is False and "Checkers Club" in got["diff"]

    def test_config_is_checked_and_applied(self, case):
        rc, out = cli("test-adapter", "--adapter", "clock", "--case", str(case),
                      "--config", '{"limit": 1}')
        assert rc == 0 and len(_last(out)["events"]) == 1
        rc, out = cli("test-adapter", "--adapter", "clock", "--case", str(case),
                      "--config", '{"limit": 0}')
        assert rc == 1 and "limit" in _last(out)["error"]

    def test_an_adapter_error_or_unknown_adapter_is_reported(self, case):
        (case / "fixture.json").write_text("{}")
        rc, out = cli("test-adapter", "--adapter", "clock", "--case", str(case))
        assert rc == 1 and "KeyError" in _last(out)["error"]
        rc, out = cli("test-adapter", "--adapter", "nope", "--case", str(case))
        assert rc == 1 and "unknown adapter" in _last(out)["error"]

    def test_adapters_lists_shared_town_shadowed_and_broken(self, home):
        home("clock.py", CLOCK)
        home("tribe.py", CLOCK)
        home("broken.py", "def read(:\n")
        rc, out = cli("adapters")
        got = _last(out)
        assert rc == 0
        assert {a["name"]: a["source"] for a in got["adapters"]} == {
            "tribe": "shared", "showare": "shared", "clock": "town"}
        assert got["shadowed"] == ["tribe"] and "broken.py" in got["errors"]


def test_every_shared_case_runs_through_its_adapter():
    """The shipped cases (tests/test_adapter_fixtures) exist for each shared adapter."""
    assert {d.name for d in CASES.iterdir() if d.is_dir()} >= set(seed_adapters.ADAPTERS)
