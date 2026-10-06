"""engine.firecrawl_env — TOWNLOOPS_FIRECRAWL_URL precedence over FIRECRAWL_API_URL."""
from __future__ import annotations

import pytest

from engine import discovery, ingest_core, research
from engine.firecrawl_env import self_hosted_url


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("TOWNLOOPS_FIRECRAWL_URL", raising=False)
    monkeypatch.delenv("FIRECRAWL_API_URL", raising=False)


def test_unset_is_empty():
    assert self_hosted_url() == ""


def test_falls_back_to_firecrawl_api_url(monkeypatch):
    monkeypatch.setenv("FIRECRAWL_API_URL", "http://firecrawl-api:3002")
    assert self_hosted_url() == "http://firecrawl-api:3002"


def test_townloops_var_wins(monkeypatch):
    monkeypatch.setenv("FIRECRAWL_API_URL", "http://firecrawl-api:3002")
    monkeypatch.setenv("TOWNLOOPS_FIRECRAWL_URL", "http://localhost:8302")
    assert self_hosted_url() == "http://localhost:8302"


def test_all_three_clients_honor_townloops_var(monkeypatch):
    """The Hermes-scrubbed case: only TOWNLOOPS_FIRECRAWL_URL survives."""
    monkeypatch.setenv("TOWNLOOPS_FIRECRAWL_URL", "http://localhost:8302/v1")
    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
    assert discovery._firecrawl_base() == "http://localhost:8302"
    assert research._firecrawl_base() == "http://localhost:8302"
    assert ingest_core._firecrawl_endpoint().startswith("http://localhost:8302/")
    # self-hosted → placeholder bearer, no "misconfigured" raise
    assert discovery._firecrawl_headers()["Authorization"] == "Bearer self-hosted"
