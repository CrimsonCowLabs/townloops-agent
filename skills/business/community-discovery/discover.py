#!/usr/bin/env python3
"""community-discovery — Firecrawl-backed sweep for new communities /
nonprofits / municipal entities in the configured town. See SKILL.md."""
from __future__ import annotations

import sys

from engine import discovery_skill


if __name__ == "__main__":
    sys.exit(discovery_skill.main(kind="community", argv=sys.argv[1:]))
