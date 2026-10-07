"""Firecrawl endpoint config, shared by ingest_core / discovery / research.

**Local Firecrawl only — never cloud.** Every Firecrawl call goes to the
town's own instance (the bundled `firecrawl-api` container, or the host's at
`http://localhost:8302`). There is no fallback to api.firecrawl.dev: with no
local URL configured, `base_url()` raises `FirecrawlNotConfigured`, and a URL
pointing at the cloud is refused the same way.

`TOWNLOOPS_FIRECRAWL_URL` wins over `FIRECRAWL_API_URL`. Hermes scrubs every
`FIRECRAWL_*` var from agent-run terminal commands (its provider-credential
blocklist, not overridable via `terminal.env_passthrough`), so on a native
Hermes profile a skill invoked by the agent never sees `FIRECRAWL_API_URL`.
The non-blocklisted name survives the scrub. Docker deploys can keep setting
`FIRECRAWL_API_URL` alone.

`FIRECRAWL_API_KEY` is only forwarded, for a local instance that checks keys;
otherwise a placeholder bearer satisfies the API schema.
"""

from __future__ import annotations

import os
from urllib.parse import urlparse

_CLOUD_HOST = "firecrawl.dev"


class FirecrawlNotConfigured(RuntimeError):
    """No local Firecrawl URL set, or it points at cloud Firecrawl."""


def self_hosted_url() -> str:
    """Configured local base URL as set, or "" when none is set."""
    return (
        os.getenv("TOWNLOOPS_FIRECRAWL_URL")
        or os.getenv("FIRECRAWL_API_URL")
        or ""
    ).strip()


def base_url() -> str:
    """The local Firecrawl base URL without a trailing `/` or `/v1` (callers
    append `/v1/...`). Raises FirecrawlNotConfigured instead of using cloud."""
    raw = self_hosted_url().rstrip("/")
    if raw.endswith("/v1"):
        raw = raw[:-3]
    if not raw:
        raise FirecrawlNotConfigured(
            "No local Firecrawl configured: set TOWNLOOPS_FIRECRAWL_URL (or "
            "FIRECRAWL_API_URL), e.g. http://localhost:8302. Cloud Firecrawl is never used."
        )
    host = (urlparse(raw).hostname or "").lower()
    if host == _CLOUD_HOST or host.endswith("." + _CLOUD_HOST):
        raise FirecrawlNotConfigured(
            f"Firecrawl URL {raw!r} is cloud Firecrawl; only a local instance is allowed."
        )
    return raw


def headers() -> dict:
    key = os.getenv("FIRECRAWL_API_KEY", "").strip()
    return {
        "Authorization": f"Bearer {key or 'self-hosted'}",
        "Content-Type": "application/json",
    }
