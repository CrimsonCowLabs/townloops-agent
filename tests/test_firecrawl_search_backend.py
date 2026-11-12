"""Issue #64: local Firecrawl search answered nothing ("search returned no
results") — performer research resolved 0 of ~2500 runs and discovery
sweeps came back empty. Self-hosted Firecrawl searches through SearXNG when
`SEARXNG_ENDPOINT` is set, else it scrapes DuckDuckGo, which blocks it. Every
compose file that runs Firecrawl must run a SearXNG next to it, wired in, with
the JSON API Firecrawl reads switched on."""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
STACKS = [ROOT / "templates" / "_shared" / "firecrawl" / "docker-compose.yml",
          ROOT / "docker-compose.yml"]


def _service(compose: dict, name: str) -> dict:
    return compose["services"][name]


@pytest.mark.parametrize("path", STACKS, ids=lambda p: str(p.relative_to(ROOT)))
def test_firecrawl_searches_through_the_stacks_own_searxng(path):
    compose = yaml.safe_load(path.read_text())
    api = _service(compose, "firecrawl-api")
    endpoint = api["environment"]["SEARXNG_ENDPOINT"]
    host = urlparse(endpoint).hostname
    searxng = _service(compose, host)   # the service name is its in-network DNS name
    assert "searxng" in searxng["image"]
    assert set(searxng["networks"]) & set(api["networks"])   # reachable in-network
    assert "ports" not in searxng                             # never published

    [mount] = [v for v in searxng["volumes"] if v.endswith("/etc/searxng/settings.yml:ro")]
    settings = yaml.safe_load((path.parent / mount.split(":")[0]).read_text())
    assert "json" in settings["search"]["formats"]   # Firecrawl reads format=json
    assert settings["server"]["limiter"] is False    # its only client is Firecrawl
    assert settings["use_default_settings"] is True
