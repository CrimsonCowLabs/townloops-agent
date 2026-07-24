#!/usr/bin/env python3
"""business-discovery — Firecrawl-backed sweep for new businesses in the
configured town. See SKILL.md."""
from __future__ import annotations

import sys

# Import path resolves via pytest.ini's `pythonpath = .` in tests; in the
# container, /app is on PYTHONPATH so `from engine ...` just works.
from engine import discovery_skill


if __name__ == "__main__":
    sys.exit(discovery_skill.main(kind="business", argv=sys.argv[1:]))
