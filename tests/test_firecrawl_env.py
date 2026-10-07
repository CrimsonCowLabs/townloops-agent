"""engine.firecrawl_env — TOWNLOOPS_FIRECRAWL_URL precedence over FIRECRAWL_API_URL."""
from __future__ import annotations

import pytest

from engine import discovery, ingest_core, research
from engine.firecrawl_env import self_hosted_url


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("TOWNLOOPS_FIRECRAWL_URL", raising=False)
    monkeypatch.delenv("FIRECRAWL_API_URL", raising=False)
    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)


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


# ------------------------------------------------- local only, never cloud

from engine import firecrawl_env


def test_base_url_requires_a_local_instance(monkeypatch):
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-cloud-key")
    with pytest.raises(firecrawl_env.FirecrawlNotConfigured):
        firecrawl_env.base_url()


def test_base_url_refuses_cloud_firecrawl(monkeypatch):
    monkeypatch.setenv("TOWNLOOPS_FIRECRAWL_URL", "https://api.firecrawl.dev")
    with pytest.raises(firecrawl_env.FirecrawlNotConfigured):
        firecrawl_env.base_url()


def test_base_url_strips_slash_and_v1(monkeypatch):
    monkeypatch.setenv("FIRECRAWL_API_URL", "http://localhost:8302/v1/")
    assert firecrawl_env.base_url() == "http://localhost:8302"


def test_no_client_falls_back_to_cloud_when_unset(monkeypatch):
    monkeypatch.setenv("FIRECRAWL_API_KEY", "fc-cloud-key")
    with pytest.raises(firecrawl_env.FirecrawlNotConfigured):
        discovery._firecrawl_base()
    with pytest.raises(firecrawl_env.FirecrawlNotConfigured):
        ingest_core._firecrawl_endpoint()
    posts = []
    monkeypatch.setattr(research.requests, "post", lambda *a, **k: posts.append(a))
    assert research.firecrawl_fetch("https://example.com") == (None, None)
    assert posts == []


def test_key_is_forwarded_to_a_local_instance_that_checks_keys(monkeypatch):
    monkeypatch.setenv("TOWNLOOPS_FIRECRAWL_URL", "http://localhost:8302")
    monkeypatch.setenv("FIRECRAWL_API_KEY", "local-key")
    assert firecrawl_env.headers()["Authorization"] == "Bearer local-key"
    monkeypatch.delenv("FIRECRAWL_API_KEY")
    assert firecrawl_env.headers()["Authorization"] == "Bearer self-hosted"
