"""Tests for engine/townloops_api.py — HTTP client + cross-town guard.

Mocks the `requests.Session.request` method to control status codes and
response bodies without hitting a real API. `time.sleep` is patched to
zero so the backoff loop doesn't slow the suite down.
"""

from __future__ import annotations

import json
import sqlite3
from unittest.mock import MagicMock, patch

import pytest


# =====================================================================
# Helpers
# =====================================================================


def _mock_response(status_code, body=None, headers=None):
    """Fake `requests.Response` — session.request returns one of these."""
    r = MagicMock()
    r.status_code = status_code
    r.headers = headers or {}
    if body is None:
        r.content = b""
        r.text = ""
        r.json.side_effect = ValueError("no content")
    elif isinstance(body, (dict, list)):
        text = json.dumps(body)
        r.content = text.encode()
        r.text = text
        r.json.return_value = body
    else:
        r.content = body.encode() if isinstance(body, str) else body
        r.text = body if isinstance(body, str) else body.decode()
        r.json.side_effect = ValueError("not json")
    return r


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    """Every retry loop calls time.sleep — zero it so tests run at wall-clock 0."""
    monkeypatch.setattr("engine.townloops_api.time.sleep", lambda s: None)


@pytest.fixture
def client(monkeypatch, tmp_state_dir):
    """Fresh Client with the shared per-process verify_town cache wiped."""
    from engine import townloops_api
    townloops_api.Client._VERIFIED.clear()
    return townloops_api.Client(
        base_url="http://test.invalid:8004",
        api_key="test-key",
        town_slug="test-town",
    )


# =====================================================================
# _request envelope handling
# =====================================================================


class TestRequestEnvelope:
    def test_unwraps_data_envelope(self, client, monkeypatch):
        mock_req = MagicMock(return_value=_mock_response(
            200, body={"data": [{"id": 1, "name": "A"}], "warnings": []}
        ))
        monkeypatch.setattr(client.session, "request", mock_req)
        result = client._request("GET", "http://x/endpoint")
        assert result == [{"id": 1, "name": "A"}]

    def test_returns_full_body_when_no_data_key(self, client, monkeypatch):
        mock_req = MagicMock(return_value=_mock_response(
            200, body={"status": "ok", "value": 42}
        ))
        monkeypatch.setattr(client.session, "request", mock_req)
        result = client._request("GET", "http://x/no-envelope")
        assert result == {"status": "ok", "value": 42}

    def test_empty_response(self, client, monkeypatch):
        mock_req = MagicMock(return_value=_mock_response(200, body=None))
        monkeypatch.setattr(client.session, "request", mock_req)
        result = client._request("DELETE", "http://x/thing")
        assert result == {}


# =====================================================================
# Retry semantics
# =====================================================================


class TestRetries:
    def test_429_triggers_retry_then_success(self, client, monkeypatch):
        """Rate-limited responses trigger backoff and retry."""
        mock_req = MagicMock(side_effect=[
            _mock_response(429),
            _mock_response(429),
            _mock_response(200, body={"data": "eventually"}),
        ])
        monkeypatch.setattr(client.session, "request", mock_req)
        result = client._request("GET", "http://x/rate-limited")
        assert result == "eventually"
        assert mock_req.call_count == 3

    def test_503_triggers_retry(self, client, monkeypatch):
        mock_req = MagicMock(side_effect=[
            _mock_response(503),
            _mock_response(200, body={"ok": True}),
        ])
        monkeypatch.setattr(client.session, "request", mock_req)
        result = client._request("GET", "http://x/flap")
        assert result == {"ok": True}
        assert mock_req.call_count == 2

    def test_network_exception_triggers_retry(self, client, monkeypatch):
        import requests
        mock_req = MagicMock(side_effect=[
            requests.ConnectionError("no route"),
            _mock_response(200, body={"data": "ok"}),
        ])
        monkeypatch.setattr(client.session, "request", mock_req)
        result = client._request("GET", "http://x/net-flap")
        assert result == "ok"
        assert mock_req.call_count == 2

    def test_exhausted_retries_raises_townloops_error(self, client, monkeypatch):
        from engine.townloops_api import TownloopsError
        mock_req = MagicMock(return_value=_mock_response(503))
        monkeypatch.setattr(client.session, "request", mock_req)
        with pytest.raises(TownloopsError) as exc_info:
            client._request("GET", "http://x/permaflap", max_retries=3)
        # After 3 attempts all returning 503, the loop exits without success →
        # falls through to the terminal `raise TownloopsError(0, "network", ...)`.
        assert exc_info.value.code == "network"


# =====================================================================
# Error extraction on 4xx/5xx
# =====================================================================


class TestErrorHandling:
    def test_400_raises_with_error_envelope(self, client, monkeypatch):
        from engine.townloops_api import TownloopsError
        body = {"error": {
            "code": "validation_error",
            "message": "name is required",
            "details": {"field": "name"},
        }}
        monkeypatch.setattr(client.session, "request",
                              MagicMock(return_value=_mock_response(400, body=body)))
        with pytest.raises(TownloopsError) as exc_info:
            client._request("POST", "http://x/create", json_body={})
        assert exc_info.value.status == 400
        assert exc_info.value.code == "validation_error"
        assert exc_info.value.message == "name is required"
        assert exc_info.value.details == {"field": "name"}

    def test_401_no_body(self, client, monkeypatch):
        from engine.townloops_api import TownloopsError
        monkeypatch.setattr(client.session, "request",
                              MagicMock(return_value=_mock_response(401)))
        with pytest.raises(TownloopsError) as exc_info:
            client._request("GET", "http://x/protected")
        assert exc_info.value.status == 401
        assert exc_info.value.code == "http_error"

    def test_404_default_error_code(self, client, monkeypatch):
        from engine.townloops_api import TownloopsError
        monkeypatch.setattr(client.session, "request",
                              MagicMock(return_value=_mock_response(404, body={})))
        with pytest.raises(TownloopsError) as exc_info:
            client._request("GET", "http://x/notthere")
        assert exc_info.value.status == 404


# =====================================================================
# Idempotency-Key header propagation
# =====================================================================


class TestIdempotencyKey:
    def test_header_set_when_provided(self, client, monkeypatch):
        mock_req = MagicMock(return_value=_mock_response(200, body={"data": {"ok": 1}}))
        monkeypatch.setattr(client.session, "request", mock_req)
        client._request("POST", "http://x/create", idempotency_key="my-key-abc")
        _, kwargs = mock_req.call_args
        assert kwargs["headers"]["Idempotency-Key"] == "my-key-abc"

    def test_no_header_when_absent(self, client, monkeypatch):
        mock_req = MagicMock(return_value=_mock_response(200, body={"data": {}}))
        monkeypatch.setattr(client.session, "request", mock_req)
        client._request("GET", "http://x/read")
        _, kwargs = mock_req.call_args
        assert "Idempotency-Key" not in kwargs["headers"]


# =====================================================================
# verify_town (P3.2 cross-town guard)
# =====================================================================


class TestVerifyTown:
    def test_passes_when_categories_non_empty_no_snapshot(self, client, monkeypatch, tmp_state_dir):
        """No snapshot on disk → guard passes as long as live returns categories."""
        # First call is Client.list("categories") — returns list of categories.
        monkeypatch.setattr(client.session, "request", MagicMock(
            return_value=_mock_response(200, body={"data": [
                {"slug": "arts"}, {"slug": "food-drink"}
            ]})
        ))
        result = client.verify_town()
        assert result["snapshot_seen"] is False
        assert result["live_categories"] == 2

    def test_fails_on_empty_categories(self, client, monkeypatch, tmp_state_dir):
        from engine.townloops_api import TownloopsError
        monkeypatch.setattr(client.session, "request", MagicMock(
            return_value=_mock_response(200, body={"data": []})
        ))
        with pytest.raises(TownloopsError) as exc:
            client.verify_town()
        assert exc.value.code == "empty_categories"

    def test_passes_when_snapshot_subset_of_live(self, client, monkeypatch, tmp_state_dir):
        """Snapshot slugs ⊆ live slugs → guard passes."""
        (tmp_state_dir / "categories.json").write_text(json.dumps([
            {"slug": "arts"}, {"slug": "community"}
        ]))
        monkeypatch.setattr(client.session, "request", MagicMock(
            return_value=_mock_response(200, body={"data": [
                {"slug": "arts"}, {"slug": "community"}, {"slug": "food-drink"}
            ]})
        ))
        result = client.verify_town()
        assert result["snapshot_seen"] is True

    def test_fails_when_snapshot_has_slugs_live_lacks(self, client, monkeypatch, tmp_state_dir):
        """Snapshot has 'utah-only' but live doesn't → cross-town drift → fatal."""
        from engine.townloops_api import TownloopsError
        (tmp_state_dir / "categories.json").write_text(json.dumps([
            {"slug": "utah-only"}, {"slug": "arts"}
        ]))
        monkeypatch.setattr(client.session, "request", MagicMock(
            return_value=_mock_response(200, body={"data": [
                {"slug": "arts"}, {"slug": "community"}
            ]})
        ))
        with pytest.raises(TownloopsError) as exc:
            client.verify_town()
        assert exc.value.code == "cross_town_drift"
        assert "utah-only" in exc.value.message

    def test_result_cached_per_process(self, client, monkeypatch, tmp_state_dir):
        """Once verified, no more API calls — the cache keyed on (base_url, town_slug)."""
        req = MagicMock(return_value=_mock_response(200, body={"data": [
            {"slug": "arts"}
        ]}))
        monkeypatch.setattr(client.session, "request", req)
        first = client.verify_town()
        second = client.verify_town()
        assert first["cached"] is False
        assert second["cached"] is True
        # Only ONE actual request — the cached call didn't hit the API.
        assert req.call_count == 1

    def test_missing_snapshot_file_is_treated_as_no_drift(self, client, monkeypatch, tmp_path):
        """The categories.json file might not exist yet (fresh deploy) — that's not drift."""
        # Point HERMES_HOME at an empty tmp so state/categories.json doesn't exist
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        monkeypatch.setattr(client.session, "request", MagicMock(
            return_value=_mock_response(200, body={"data": [{"slug": "arts"}]})
        ))
        result = client.verify_town()
        assert result["snapshot_seen"] is False


# =====================================================================
# TownloopsError formatting
# =====================================================================


class TestTownloopsError:
    def test_stringifies_with_status_and_code(self):
        from engine.townloops_api import TownloopsError
        err = TownloopsError(422, "validation_error", "msg here")
        assert "422" in str(err)
        assert "validation_error" in str(err)
        assert "msg here" in str(err)

    def test_carries_details(self):
        from engine.townloops_api import TownloopsError
        err = TownloopsError(400, "bad", "no", {"field": "x"})
        assert err.details == {"field": "x"}
