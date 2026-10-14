"""engine/preflight — one read-only verdict on whether this town will work.

Assertions are on findings (severity + code), never on wording: one test per
failure mode, plus the baseline that a fully-configured town has no errors.
"""

from __future__ import annotations

import json
import os

import pytest

from engine import autonomy, preflight
from engine.townloops_api import TownloopsError
from tests.fake_townloops import FakeAPI

CATEGORIES = ["municipal", "nonprofit", "community-group", "live-music"]

PROFILE = {
    "name": "Testville",
    "region": "California",
    "operating_mode": "supervised",
    "discovery": {"community": {"query_groups": [
        {"category_slug": "municipal", "terms": ["city council"]},
        {"category_slug": "nonprofit", "terms": ["501c3"]},
    ]}},
}


class HealthyAPI(FakeAPI):
    def __init__(self, categories=CATEGORIES, **kw):
        super().__init__(entities={"categories": [{"slug": s, "name": s} for s in categories]}, **kw)
        self.healthz_error = None
        self.list_error = None
        self.verify_error = None

    def healthz(self):
        if self.healthz_error:
            raise self.healthz_error
        return {"status": "ok"}

    def list(self, resource, **params):
        if self.list_error:
            raise self.list_error
        return super().list(resource, **params)

    def verify_town(self, *a, **k):
        if self.verify_error:
            raise self.verify_error
        return {"town_slug": self.town_slug}


class FakeListmonk:
    def __init__(self, error=None):
        self.error = error

    def lists(self):
        if self.error:
            raise self.error
        return [{"id": 1}]


@pytest.fixture
def town(tmp_state_dir, fresh_db, tmp_path, monkeypatch):
    """A fully-configured supervised town; tests break one thing each."""
    (tmp_state_dir / "categories.json").write_text(
        json.dumps([{"slug": s} for s in CATEGORIES]))
    monkeypatch.delenv("TOWNLOOPS_CATEGORIES_SNAPSHOT", raising=False)
    codex = tmp_path / "codex"
    codex.mkdir()
    (codex / "auth.json").write_text("{}")
    town_dir = tmp_path / "town"
    town_dir.mkdir()
    for k, v in {
        "DASHBOARD_PASSWORD": "$2b$12$hash",
        "FIRECRAWL_API_URL": "http://firecrawl-api:3002",
        "AGENT_TZ": "America/Denver",
        "LISTMONK_URL": "http://listmonk:9000",
        "LISTMONK_USER": "api",
        "LISTMONK_TOKEN": "tok",
        "TELEGRAM_BOT_TOKEN": "123:abc",
        "TELEGRAM_CHAT_ID": "42",
        "CODEX_HOME": str(codex),
        "TOWNLOOPS_TOWN_DIR": str(town_dir),
    }.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("TOWNLOOPS_FIRECRAWL_URL", raising=False)

    class Town:
        api = HealthyAPI()
        profile = json.loads(json.dumps(PROFILE))
        listmonk = FakeListmonk()
        firecrawl_down = False
        conn = fresh_db
        dir = town_dir
        state = tmp_state_dir

        def run(self):
            def http_get(url, timeout):
                if self.firecrawl_down:
                    raise ConnectionError("refused")
                return object()
            return preflight.run(client=self.api, profile=self.profile,
                                 http_get=http_get, listmonk=lambda: self.listmonk)

        def codes(self, severity=None):
            return {f.code for f in self.run() if severity in (None, f.severity)}

    return Town()


def _autonomous(town):
    town.profile["operating_mode"] = "autonomous"
    (town.dir / "docker-compose.override.yml").write_text(
        'services: {dashboard: {profiles: ["supervised-dashboard"]}}\n')


# --------------------------------------------------------------------------
# Baseline
# --------------------------------------------------------------------------

def test_a_fully_configured_supervised_town_has_no_errors_or_warnings(town):
    findings = town.run()
    assert not preflight.has_errors(findings)
    assert [f for f in findings if f.severity == "warning"] == []


def test_a_fully_configured_autonomous_town_has_no_errors(town):
    _autonomous(town)
    assert not preflight.has_errors(town.run())


def test_unverifiable_profile_fields_are_reported_as_info(town):
    infos = [f for f in town.run() if f.severity == "info"]
    assert any(f.code == "profile-unverifiable" for f in infos)


def test_every_finding_names_a_file_and_key_and_a_consequence(town):
    town.api.healthz_error = TownloopsError(0, "network", "down")
    town.profile.pop("name")
    for f in town.run():
        assert f.severity in ("error", "warning", "info")
        assert f.code and f.where and f.message


# --------------------------------------------------------------------------
# Credential
# --------------------------------------------------------------------------

def test_api_unreachable_is_an_error(town):
    town.api.healthz_error = TownloopsError(0, "network", "exhausted retries")
    assert "api-unreachable" in town.codes("error")


def test_a_rejected_key_is_an_error(town):
    town.api.list_error = TownloopsError(401, "unauthorized", "bad token")
    assert "credential-rejected" in town.codes("error")


def test_a_key_scoped_to_another_town_reports_verify_towns_error(town):
    town.api.verify_error = TownloopsError(0, "cross_town_drift", "snapshot drift")
    findings = [f for f in town.run() if f.code == "town-scope"]
    assert findings and findings[0].severity == "error"
    assert "snapshot drift" in findings[0].message


def test_missing_credentials_are_an_error(town, monkeypatch):
    monkeypatch.delenv("TOWNLOOPS_API_KEY", raising=False)
    findings = preflight.run(client=None, profile=town.profile,
                             http_get=lambda u, timeout: object(),
                             listmonk=lambda: town.listmonk)
    assert "credential-missing" in {f.code for f in findings if f.severity == "error"}


# --------------------------------------------------------------------------
# Profile
# --------------------------------------------------------------------------

def test_a_missing_profile_is_an_error(town):
    town.profile = {}
    assert "profile-missing" in town.codes("error")


def test_a_placeholder_town_name_is_an_error(town):
    town.profile["name"] = 'PLACEHOLDER — the town\'s display name'
    assert "profile-placeholder" in town.codes("error")


def test_an_unknown_category_slug_is_an_error_naming_known_slugs(town):
    town.profile["discovery"]["community"]["query_groups"][0]["category_slug"] = "municipality"
    f = next(f for f in town.run() if f.code == "category-unknown")
    assert f.severity == "error"
    assert f.detail["slug"] == "municipality"
    assert "municipal" in f.detail["known"]


def test_a_category_live_but_missing_from_the_snapshot_says_the_snapshot_is_stale(town):
    (town.state / "categories.json").write_text(json.dumps([{"slug": "nonprofit"}]))
    codes = town.codes("error")
    assert "category-snapshot-stale" in codes
    assert "category-unknown" not in codes


def test_a_missing_categories_snapshot_is_an_error(town):
    (town.state / "categories.json").unlink()
    assert "categories-snapshot-missing" in town.codes("error")


# --------------------------------------------------------------------------
# Operating mode
# --------------------------------------------------------------------------

def test_supervised_without_a_dashboard_password_is_an_error(town, monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "")
    assert "dashboard-password-missing" in town.codes("error")


def test_autonomous_with_the_dashboard_enabled_is_an_error(town):
    _autonomous(town)
    (town.dir / "docker-compose.override.yml").write_text("services: {}\n")
    assert "dashboard-enabled-autonomous" in town.codes("error")


def test_autonomous_with_no_override_is_an_error(town):
    _autonomous(town)
    (town.dir / "docker-compose.override.yml").unlink()
    assert "dashboard-enabled-autonomous" in town.codes("error")


def test_autonomous_without_a_visible_town_dir_is_a_warning(town, monkeypatch):
    _autonomous(town)
    monkeypatch.setenv("TOWNLOOPS_TOWN_DIR", str(town.dir / "nope"))
    monkeypatch.setenv("TOWNLOOPS_AGENTS_ROOT", str(town.dir / "nope"))
    assert "dashboard-unverified" in town.codes("warning")


def test_autonomous_does_not_need_a_dashboard_password(town, monkeypatch):
    _autonomous(town)
    monkeypatch.setenv("DASHBOARD_PASSWORD", "")
    assert "dashboard-password-missing" not in town.codes()


# --------------------------------------------------------------------------
# Circuit
# --------------------------------------------------------------------------

def test_an_open_publish_key_circuit_is_an_error_carrying_reason_and_evidence(town):
    _autonomous(town)
    evidence = {"resource": "events", "slug": "jazz", "status": "published",
                "note": "the API key can publish — switch the town to an "
                        "api_ingest key without publish permission"}
    autonomy.open_circuit(town.conn, "published-without-review", evidence, alert=lambda t: None)
    f = next(f for f in town.run() if f.code == "circuit-open")
    assert f.severity == "error"
    assert f.detail["reason"] == "published-without-review"
    assert f.detail["evidence"] == evidence


def test_a_closed_circuit_is_not_reported(town):
    autonomy.open_circuit(town.conn, "rejection-rate", {}, alert=lambda t: None)
    autonomy.close_circuit(town.conn)
    assert "circuit-open" not in town.codes()


def test_preflight_does_not_create_a_missing_state_db(town, monkeypatch, tmp_path):
    missing = tmp_path / "elsewhere" / "townloops.db"
    monkeypatch.setenv("TOWNLOOPS_DB_PATH", str(missing))
    assert "state-db-missing" in town.codes("warning")
    assert not missing.exists()


# --------------------------------------------------------------------------
# Firecrawl / Codex / timezone
# --------------------------------------------------------------------------

def test_no_firecrawl_url_is_an_error(town, monkeypatch):
    monkeypatch.delenv("FIRECRAWL_API_URL")
    assert "firecrawl-not-configured" in town.codes("error")


def test_cloud_firecrawl_is_an_error(town, monkeypatch):
    monkeypatch.setenv("FIRECRAWL_API_URL", "https://api.firecrawl.dev")
    assert "firecrawl-not-configured" in town.codes("error")


def test_unreachable_firecrawl_is_an_error(town):
    town.firecrawl_down = True
    assert "firecrawl-unreachable" in town.codes("error")


def test_a_missing_codex_login_is_an_error(town, monkeypatch, tmp_path):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "no-codex"))
    assert "codex-login-missing" in town.codes("error")


def test_an_invalid_timezone_is_an_error(town, monkeypatch):
    monkeypatch.setenv("AGENT_TZ", "America/Nowhere")
    assert "tz-invalid" in town.codes("error")


def test_the_pacific_fallback_timezone_is_a_warning(town, monkeypatch):
    monkeypatch.delenv("AGENT_TZ")
    assert "tz-fallback" in town.codes("warning")


# --------------------------------------------------------------------------
# Optional pieces — warnings only
# --------------------------------------------------------------------------

def test_unconfigured_listmonk_is_a_warning(town, monkeypatch):
    monkeypatch.setenv("LISTMONK_TOKEN", "")
    findings = town.run()
    assert "listmonk-unconfigured" in {f.code for f in findings if f.severity == "warning"}
    assert not preflight.has_errors(findings)


def test_unreachable_listmonk_is_a_warning(town):
    town.listmonk = FakeListmonk(error=ConnectionError("refused"))
    findings = town.run()
    assert "listmonk-unreachable" in {f.code for f in findings if f.severity == "warning"}
    assert not preflight.has_errors(findings)


def test_autonomous_without_telegram_is_a_warning(town, monkeypatch):
    _autonomous(town)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    findings = town.run()
    assert "telegram-missing" in {f.code for f in findings if f.severity == "warning"}
    assert not preflight.has_errors(findings)


def test_supervised_without_telegram_is_fine(town, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "")
    assert "telegram-missing" not in town.codes()


# --------------------------------------------------------------------------
# Read-only + cheap
# --------------------------------------------------------------------------

def test_preflight_only_reads_one_row_and_never_writes(town):
    town.run()
    methods = {c[0] for c in town.api.calls}
    assert methods <= {"GET", "LIST"}
    for c in town.api.calls:
        if c[0] == "LIST" and c[1] != "categories":
            assert c[2].get("limit") == 1


def test_human_output_and_json_round_trip(town):
    town.api.healthz_error = TownloopsError(0, "network", "down")
    findings = town.run()
    text = preflight.format_human(findings)
    assert "api-unreachable" in text
    payload = json.loads(json.dumps([f.to_dict() for f in findings]))
    assert any(p["code"] == "api-unreachable" and p["severity"] == "error" for p in payload)


# --------------------------------------------------------------------------
# town-init CLI
# --------------------------------------------------------------------------

def _town_init():
    import importlib.util
    from pathlib import Path
    path = Path(__file__).resolve().parents[1] / "skills/business/town-init/init.py"
    spec = importlib.util.spec_from_file_location("town_init_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _cli(mod, monkeypatch, *argv):
    monkeypatch.setattr("sys.argv", ["init.py", *argv])
    return mod.main()


def _wire(mod, town, monkeypatch):
    """town-init talking to the in-process town instead of the network."""
    town.api.base_url = "http://fake"
    monkeypatch.setattr(mod.db, "DEFAULT_DB_PATH", str(town.state / "townloops.db"))
    monkeypatch.setattr(mod.townloops_api, "Client", lambda: town.api)
    real_run = mod.preflight.run
    monkeypatch.setattr(mod.preflight, "run", lambda client=None: real_run(
        client or town.api, profile=town.profile, http_get=lambda u, timeout: object(),
        listmonk=lambda: town.listmonk))


def test_town_init_preflight_exits_zero_on_warnings_only(town, monkeypatch, capsys):
    mod = _town_init()
    _wire(mod, town, monkeypatch)
    monkeypatch.setenv("LISTMONK_TOKEN", "")
    assert _cli(mod, monkeypatch, "preflight") == 0
    assert "listmonk-unconfigured" in capsys.readouterr().out


def test_town_init_preflight_json_exits_nonzero_on_errors(town, monkeypatch, capsys):
    mod = _town_init()
    _wire(mod, town, monkeypatch)
    town.api.healthz_error = TownloopsError(0, "network", "down")
    assert _cli(mod, monkeypatch, "preflight", "--json") != 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False
    assert any(f["code"] == "api-unreachable" and f["severity"] == "error" for f in out["findings"])


def test_town_init_setup_reports_preflight_errors_instead_of_ok(town, monkeypatch, capsys):
    mod = _town_init()
    _wire(mod, town, monkeypatch)
    town.profile["discovery"]["community"]["query_groups"][0]["category_slug"] = "nope"
    assert _cli(mod, monkeypatch, "setup") != 0
    out = json.loads(capsys.readouterr().out)
    assert out["ok"] is False
    assert "category-unknown" in {f["code"] for f in out["findings"]}


def test_town_init_setup_is_ok_for_a_configured_town(town, monkeypatch, capsys):
    mod = _town_init()
    _wire(mod, town, monkeypatch)
    (town.state / "categories.json").unlink()          # setup writes the snapshot itself
    assert _cli(mod, monkeypatch, "setup") == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True
    assert (town.state / "categories.json").exists()
