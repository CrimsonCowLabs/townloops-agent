"""Self-hosted Firecrawl URL lookup, shared by ingest_core / discovery / research.

`TOWNLOOPS_FIRECRAWL_URL` wins over `FIRECRAWL_API_URL`. Hermes scrubs every
`FIRECRAWL_*` var from agent-run terminal commands (its provider-credential
blocklist, not overridable via `terminal.env_passthrough`), so on a native
Hermes profile a skill invoked by the agent never sees `FIRECRAWL_API_URL` and
would silently fall back to cloud. The non-blocklisted name survives the scrub.
Docker deploys can keep setting `FIRECRAWL_API_URL` alone.
"""

from __future__ import annotations

import os


def self_hosted_url() -> str:
    """Configured self-hosted base URL, or "" when none is set (→ cloud)."""
    return (
        os.getenv("TOWNLOOPS_FIRECRAWL_URL")
        or os.getenv("FIRECRAWL_API_URL")
        or ""
    ).strip()
