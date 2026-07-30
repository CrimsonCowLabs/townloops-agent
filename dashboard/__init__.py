"""townloops-agent operator dashboard.

Standalone FastAPI service run as a second container in the per-town
docker-compose. Exposes a browser UI (server-rendered HTML + htmx) for
operators to observe agent state and take the mutations the CLI already
supports: approve/reject discovery candidates, approve event sources,
push the weekly newsletter, force ticks, force sweeps.

Entry point: `uvicorn dashboard.main:app`. Auth via bcrypt password stored
in DASHBOARD_PASSWORD env var; session cookie signed with an HMAC key
auto-generated on first boot at $HERMES_HOME/state/dashboard_secret.
"""
