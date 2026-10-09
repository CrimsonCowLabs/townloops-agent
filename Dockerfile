# Extends the base hermes-agent image with the Python deps the town skills need,
# plus Node LTS + the OpenAI Codex CLI (used by entity-research for classifier
# fallback via ChatGPT session auth).
#
# The base ships Python 3.13 + uv + the Hermes runtime; it does NOT ship the
# parsers and API clients these skills rely on. Deploying without this
# Dockerfile means the first `event-ingest ingest` blows up on ModuleNotFoundError.
#
# Base image ships uv (not pip) — 2026-07-10 discovery when the first mac deploy
# hit `pip: not found`. The base Python at /usr/bin/python3 is marked
# externally-managed (PEP 668), so `--break-system-packages` is required for
# `uv pip install --system` to write into the system site-packages. Safe here
# because the container is dedicated to the Hermes runtime; nothing else
# competes for that Python.
#
# See docs/STATUS.md § "Second-instance deploy" for smoke tests. Minimum:
# `docker compose run --rm agent whoami` should print the user whose home
# matches HERMES_HOME in docker-compose.yml (default: `agent`).
FROM nousresearch/hermes-agent:v2026.5.16

# --- Node LTS + Codex CLI ---------------------------------------------
# entity-research shells out to `codex exec` for taxonomy classification
# (fallback when the keyword map returns None). Auth is via ChatGPT
# session stored at ~/.codex/auth.json, mounted rw from the host per-town.
#
# Package name to verify against current Codex install docs — was
# `@openai/codex` at plan time; may have changed. If npm install fails
# in a later base-image bump, check https://github.com/openai/codex.
USER root
RUN curl -fsSL https://deb.nodesource.com/setup_22.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && apt-get clean && rm -rf /var/lib/apt/lists/*
RUN npm install -g @openai/codex

# Deps used across skills + the operator dashboard + Telegram inbound.
# Pin nothing — each skill catches its own import error at boot, and
# freezing versions here breaks base-image upgrades. Add new packages
# ABOVE the trailing line so the diff stays readable.
RUN uv pip install --system --no-cache --break-system-packages \
    feedparser \
    requests \
    pyyaml \
    beautifulsoup4 \
    python-dateutil \
    icalendar \
    jinja2 \
    lxml \
    fastapi \
    "uvicorn[standard]" \
    python-multipart \
    itsdangerous \
    bcrypt \
    text-unidecode \
    markdown

# python-telegram-bot must be visible to the Hermes gateway, which runs
# in the /opt/hermes/.venv/ virtualenv (not system Python). Install it
# directly into that venv. Don't upgrade anything else there — Hermes
# pins several packages we mustn't disturb.
RUN uv pip install --python /opt/hermes/.venv/bin/python --no-cache \
    python-telegram-bot
