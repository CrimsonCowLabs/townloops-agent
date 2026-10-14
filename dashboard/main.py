"""FastAPI app for the operator dashboard.

Routes are grouped:
  - GET /health           → unauth JSON probe
  - GET /login, POST /login, GET /logout → session flow
  - Everything else       → require_session dependency

Templates live at dashboard/templates/ (Jinja2). Static assets under
dashboard/static/.
"""

from __future__ import annotations

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import quote, unquote

import markdown as md
from fastapi import Depends, FastAPI, Form, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from dashboard import auth, chat, chat_db, db, db_v2, intent_handlers, skills

from engine import autonomy, db as _engine_db


_HERMES_HOME = Path(os.environ.get("HERMES_HOME", "/home/agent/.hermes"))
_LLM_HEALTH_PATH = _HERMES_HOME / "state" / "llm_health.json"
_REPORTS_DIR = _HERMES_HOME / "state" / "reports"
_NEWSLETTERS_DIR = _HERMES_HOME / "state" / "newsletters"
_SQUID_LOG_PATH = Path(os.environ.get("SQUID_ACCESS_LOG_PATH", "/squid-log/access.log"))
_TOWN_SLUG = os.environ.get("TOWNLOOPS_TOWN_SLUG", "?")

_BASE_DIR = Path(__file__).resolve().parent
_TEMPLATES = Jinja2Templates(directory=str(_BASE_DIR / "templates"))


# ---- local_time Jinja filter --------------------------------------------
# Server stores every timestamp as an ISO string (usually UTC). Emit a
# semantic <time datetime="..."> element; a tiny <script> in base.html
# rewrites its text to the viewer's locale on load. That way the UI shows
# times in the *browser's* timezone without any server-side tz gymnastics.

from markupsafe import Markup, escape as _escape


def _local_time_filter(value):
    if not value:
        return Markup("<span class='muted'>—</span>")
    v = _escape(str(value))
    return Markup(f'<time datetime="{v}">{v}</time>')


_TEMPLATES.env.filters["local_time"] = _local_time_filter


def _fromjson_filter(value):
    """Parse a JSON string, returning None on error so templates can `if`-guard."""
    if not value:
        return None
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return None


_TEMPLATES.env.filters["fromjson"] = _fromjson_filter

app = FastAPI(title=f"townloops-agent dashboard — {_TOWN_SLUG}")
app.mount("/static", StaticFiles(directory=str(_BASE_DIR / "static")), name="static")


def _refuse_autonomous_town(log) -> None:
    """The dashboard is a supervised-mode surface. In an autonomous town the
    agent makes the local calls and the TownLoops admin moderation queue is
    the only human gate, so a dashboard offering local approvals must not
    run. The mode is read fresh from town-profile.yaml at each start;
    raising here makes uvicorn log the error and exit non-zero. The per-town
    docker-compose.override.yml (bin/townloops-agent-add --autonomous) keeps
    the service from being started at all; this is the backstop."""
    if not autonomy.is_autonomous():
        return
    msg = ("refusing to start: this town runs in autonomous mode "
           "(town-profile.yaml operating_mode: autonomous) — the TownLoops admin "
           "moderation queue is its only review step and the local dashboard does "
           "not run. Disable the service with "
           "`services: {dashboard: {profiles: [\"supervised-dashboard\"]}}` in the "
           "town's docker-compose.override.yml, or set operating_mode: supervised.")
    log.error(msg)
    raise RuntimeError(msg)


@app.on_event("startup")
def _dashboard_startup() -> None:
    """One-shot boot tasks:
    - Apply any pending schema migrations. Today only town-init calls
      `run_migrations`; without this hook, brand-new schema (e.g. the
      chat_messages table introduced with the operator chat panel) never
      lands on the dashboard container's view of the DB — every /v2/chat/*
      call 500s with `no such table`.
    - Reap orphaned 'processing' chat rows so a container restart
      doesn't leave "thinking…" bubbles frozen forever.

    Wrapped in try/except so a failed migration serves the rest of /v2
    degraded rather than fail-closed.

    Except the mode check, which fails closed: see `_refuse_autonomous_town`."""
    import logging
    log = logging.getLogger("dashboard.startup")
    _refuse_autonomous_town(log)
    try:
        with _engine_db.connect() as conn:
            new_files = _engine_db.run_migrations(conn)
        if new_files:
            log.info("startup: applied migrations %s", new_files)
    except Exception as exc:  # noqa: BLE001 — log-and-serve-degraded is the goal
        log.exception("startup: migrations failed: %s", exc)
    try:
        swept = chat_db.reap_orphans()
        if swept:
            log.info("startup: swept %d orphan chat placeholder(s)", swept)
    except Exception as exc:  # noqa: BLE001
        log.exception("startup: chat orphan sweep failed: %s", exc)
    try:
        rotated = skills.rotate_spawn_logs(older_than_days=7)
        if rotated:
            log.info("startup: rotated %d spawn log file(s) > 7 days old", rotated)
    except Exception as exc:  # noqa: BLE001
        log.exception("startup: spawn log rotation failed: %s", exc)


# require_session raises 401. Two paths depending on caller:
#  - Full-page nav: 303 → /login. Browser follows, user sees login form.
#  - htmx fragment poll (HX-Request: true): 401 + HX-Redirect: /login.
#    htmx sees the header and does a TOP-LEVEL navigation, so the login
#    form doesn't get swapped into a widget slot. Without this branch,
#    an expired-mid-poll session cascades login-form HTML into every
#    polling widget on the page.
@app.exception_handler(HTTPException)
def _http_exc_handler(request: Request, exc: HTTPException):
    if exc.status_code == 401:
        is_htmx = request.headers.get("hx-request", "").lower() == "true"
        if is_htmx:
            return Response(
                status_code=401,
                headers={"HX-Redirect": "/login"},
            )
        return RedirectResponse(url="/login", status_code=303)
    # Fall back to the default handler for other status codes.
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)


# --------------------------------------------------------------------------
# Flash cookie plumbing — one-shot signed messages surfaced by base.html.
# --------------------------------------------------------------------------

def _flash(response, level: str, msg: str) -> None:
    """Attach a flash cookie. Consumed on next render. URL-encoded so any
    Unicode (arrows, emoji, non-latin) is latin-1 safe for HTTP headers —
    Starlette's set_cookie otherwise raises UnicodeEncodeError on → etc."""
    response.set_cookie(
        key=f"flash_{level}",
        value=quote(msg[:400], safe=""),
        max_age=30, httponly=True, samesite="strict",
    )


def _consume_flashes(request: Request, response) -> dict:
    out = {}
    for level in ("ok", "error"):
        key = f"flash_{level}"
        val = request.cookies.get(key)
        if val:
            try:
                out[level] = unquote(val)
            except Exception:
                out[level] = val
            response.delete_cookie(key)
    return out


def _tmpl(name: str, request: Request, ctx: dict, status_code: int = 200):
    """Render a template with flash consumption + common context."""
    response = HTMLResponse(status_code=status_code)
    flashes = _consume_flashes(request, response)
    ctx = {
        "request": request,
        "town": _TOWN_SLUG,
        "flashes": flashes,
        **ctx,
    }
    html = _TEMPLATES.get_template(name).render(**ctx)
    response.body = html.encode("utf-8")
    response.headers["content-length"] = str(len(response.body))
    return response


# --------------------------------------------------------------------------
# Unauth routes
# --------------------------------------------------------------------------

@app.get("/health")
def health() -> JSONResponse:
    state = db.agent_state()
    return JSONResponse({
        "ok": True,
        "town": _TOWN_SLUG,
        "phase": state.get("phase"),
        "tick_count": state.get("tick_count"),
        "last_tick_at": state.get("last_tick_at"),
    })


@app.get("/login")
def login_page(request: Request):
    return _tmpl("login.html", request, {})


@app.post("/login")
def login_submit(request: Request, password: str = Form(...)):
    if not auth.verify_password(password):
        resp = RedirectResponse(url="/login", status_code=303)
        _flash(resp, "error", "Wrong password.")
        return resp
    resp = RedirectResponse(url="/", status_code=303)
    name, val, kw = auth.issue_session_cookie()
    resp.set_cookie(name, val, **kw)
    _flash(resp, "ok", "Signed in.")
    return resp


@app.get("/logout")
def logout(request: Request):
    resp = RedirectResponse(url="/login", status_code=303)
    name, kw = auth.clear_session_cookie()
    resp.delete_cookie(name, **kw)
    return resp


# --------------------------------------------------------------------------
# Auth-required routes
# --------------------------------------------------------------------------

@app.get("/", dependencies=[Depends(auth.require_session)])
def overview(request: Request):
    """The 3-band dashboard. Band 1 = agent, Band 2 = events, Band 3 =
    todos. First paint is server-rendered from these values; htmx polls
    keep each widget fresh on its own cadence thereafter."""
    state = db.agent_state()
    candidates = db.candidate_counts()
    entities = db.entity_counts()
    events = db.event_counts()
    sources = db.source_counts()
    source_runs = db.recent_source_runs(hours=24)
    llm_health = {}
    if _LLM_HEALTH_PATH.exists():
        try:
            llm_health = json.loads(_LLM_HEALTH_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            llm_health = {"status": "unreadable"}
    last_tick = db.last_tick_summary()
    running = db.orchestrator_running()
    next_fires = db.next_cron_fires(limit=6)
    recent_cron = db.cron_output_files(limit=8)

    # Band 2 + Band 3 data
    events_list = db.events_upcoming(window_days=30)
    days = db.events_by_day(days=30)
    max_count = max((d["count"] for d in days), default=0)
    by_source = db.events_by_source(days=30, limit=10)
    silent = db.silent_sources(days=30)
    gaps = db.venues_without_events(limit=20)
    recent = db.events_recent_ingested(limit=20)
    empty_state = _empty_state_branch() if not events_list else None

    return _tmpl("overview.html", request, {
        # Band 1
        "state": state,
        "candidates": candidates,
        "entities": entities,
        "events": events,
        "sources": sources,
        "source_runs": source_runs,
        "llm_health": llm_health,
        "last_tick": last_tick,
        "running": running,
        "next_fires": next_fires,
        "recent_cron": recent_cron,
        # Band 2
        "events_list": events_list,
        "days": days,
        "max_count": max_count,
        "window": 30,
        "empty_state": empty_state,
        # Band 3
        "by_source": by_source,
        "silent": silent,
        "gaps": gaps,
        "recent": recent,
    })


@app.get("/activity", dependencies=[Depends(auth.require_session)])
def activity_page(request: Request, job: Optional[str] = None):
    """Full history of cron runs. Click a row to see its output."""
    files = db.cron_output_files(job_id=job, limit=100)
    # Group by job for the sidebar
    jobs_seen: dict[str, int] = {}
    for f in db.cron_output_files(limit=500):
        jobs_seen[f["job_id"]] = jobs_seen.get(f["job_id"], 0) + 1
    return _tmpl("activity.html", request, {
        "files": files,
        "jobs_seen": sorted(jobs_seen.items()),
        "selected_job": job,
        "running": db.orchestrator_running(),
        "next_fires": db.next_cron_fires(limit=12),
    })


@app.get("/activity/{job_id}/{filename}",
         dependencies=[Depends(auth.require_session)])
def activity_run(request: Request, job_id: str, filename: str):
    body = db.cron_output_read(job_id, filename)
    if body is None:
        raise HTTPException(status_code=404, detail="cron output not found")
    return _tmpl("activity_run.html", request, {
        "job_id": job_id,
        "filename": filename,
        "body": body,
    })


@app.get("/candidates", dependencies=[Depends(auth.require_session)])
def candidates_page(request: Request,
                    kind: Optional[str] = None,
                    status: Optional[str] = None,
                    page: int = 0):
    rows = db.list_candidates(kind=kind, status=status, page=page)
    return _tmpl("candidates.html", request, {
        "rows": rows,
        "kind": kind or "",
        "status": status or "",
        "page": page,
        "sources": db.sources_grouped_by_signal(),
    })


@app.post("/candidates/{uid}/approve",
          dependencies=[Depends(auth.require_session)])
def candidate_approve(uid: str, request: Request):
    n = db.approve_candidate(uid)
    row = db.get_candidate(uid)
    if not row:
        raise HTTPException(status_code=404, detail="candidate not found")
    if n == 0:
        # already verified/promoted — render current state anyway.
        pass
    html = _TEMPLATES.get_template("_candidate_row.html").render(
        request=request, row=row,
    )
    return HTMLResponse(html)


@app.post("/candidates/{uid}/promote",
          dependencies=[Depends(auth.require_session)])
def candidate_promote(uid: str, request: Request):
    db.promote_candidate(uid)
    row = db.get_candidate(uid)
    if not row:
        raise HTTPException(status_code=404, detail="candidate not found")
    html = _TEMPLATES.get_template("_candidate_row.html").render(
        request=request, row=row,
    )
    return HTMLResponse(html)


@app.post("/candidates/{uid}/reject",
          dependencies=[Depends(auth.require_session)])
def candidate_reject(uid: str, request: Request):
    db.reject_candidate(uid)
    row = db.get_candidate(uid)
    if not row:
        raise HTTPException(status_code=404, detail="candidate not found")
    html = _TEMPLATES.get_template("_candidate_row.html").render(
        request=request, row=row,
    )
    return HTMLResponse(html)


@app.post("/candidates/bulk-approve",
          dependencies=[Depends(auth.require_session)])
def candidate_bulk_approve(request: Request,
                           kind: Optional[str] = Form(None),
                           source: Optional[str] = Form(None)):
    n = db.bulk_approve(kind=kind or None, source_signal_like=source or None)
    resp = RedirectResponse(url="/candidates?status=new", status_code=303)
    _flash(resp, "ok", f"Bulk-approved {n} candidate(s).")
    return resp


@app.get("/entities/{kind}", dependencies=[Depends(auth.require_session)])
def entities_page(request: Request, kind: str):
    if kind not in ("business", "venue", "community", "performer", "creator"):
        raise HTTPException(status_code=404, detail="unknown kind")
    rows = db.list_entities(kind, limit=200)
    return _tmpl("entities.html", request, {
        "kind": kind,
        "rows": rows,
    })


@app.get("/sources", dependencies=[Depends(auth.require_session)])
def sources_page(request: Request):
    rc, out, err = skills.source_list_json()
    # discover.py list prints ONE JSON object per line — collect them all.
    parsed = []
    if rc == 0 and out:
        for line in out.splitlines():
            line = line.strip()
            if not line.startswith("{"):
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            probe = obj.get("probe") or {}
            parsed.append({
                "host": (obj.get("url") or "").split("//")[-1].split("/")[0],
                "url": obj.get("url"),
                "kind": obj.get("approved_parser") or probe.get("ical_url") and "ical"
                        or probe.get("rss_url") and "rss"
                        or probe.get("jsonld_events") and "jsonld"
                        or "—",
                "status": obj.get("status"),
                "title": obj.get("title"),
                "hosting_group": obj.get("hosting_group"),
                "default_organization_kind": obj.get("default_organization_kind"),
            })
    return _tmpl("sources.html", request, {
        "parsed": parsed,
        "raw": out if not parsed else "",
        "err": err if rc != 0 else "",
    })


@app.post("/sources/{host}/approve",
          dependencies=[Depends(auth.require_session)])
def source_approve(host: str, request: Request):
    rc, out, err = skills.source_approve(host)
    resp = RedirectResponse(url="/sources", status_code=303)
    if rc == 0:
        _flash(resp, "ok", f"Approved source: {host}")
    else:
        _flash(resp, "error", f"approve {host} failed (rc={rc}): {err[:200]}")
    return resp


@app.get("/newsletter", dependencies=[Depends(auth.require_session)])
def newsletter_page(request: Request, week: Optional[str] = None):
    # Default to current ISO week.
    if not week:
        now = datetime.now(timezone.utc)
        iso_year, iso_week, _ = now.isocalendar()
        week = f"{iso_year}-W{iso_week:02d}"
    draft_path = _NEWSLETTERS_DIR / f"{week}.html"
    body = ""
    if draft_path.exists():
        try:
            body = draft_path.read_text()
        except OSError as e:
            body = f"<p>Could not read draft: {e}</p>"
    return _tmpl("newsletter.html", request, {
        "week": week,
        "body": body,
        "exists": draft_path.exists(),
    })


@app.post("/newsletter/push", dependencies=[Depends(auth.require_session)])
def newsletter_push(request: Request,
                    week: str = Form(...),
                    confirm: str = Form(...)):
    if confirm != "yes-push-live":
        resp = RedirectResponse(url=f"/newsletter?week={week}", status_code=303)
        _flash(resp, "error", "Push not confirmed — the confirm field must equal 'yes-push-live'.")
        return resp
    rc, out, err = skills.newsletter_push(week)
    resp = RedirectResponse(url=f"/newsletter?week={week}", status_code=303)
    if rc == 0:
        _flash(resp, "ok", f"Newsletter {week} pushed to Listmonk.")
    else:
        hint = ""
        if "LISTMONK" in err.upper() or "listmonk" in err.lower():
            hint = " (LISTMONK_URL/USER/TOKEN missing? Set in .env and recreate.)"
        _flash(resp, "error", f"push failed (rc={rc}){hint}: {err[:200]}")
    return resp


@app.get("/reports", dependencies=[Depends(auth.require_session)])
def reports_index(request: Request):
    files = []
    if _REPORTS_DIR.exists():
        files = sorted(
            (p.name for p in _REPORTS_DIR.iterdir() if p.suffix == ".md"),
            reverse=True,
        )
    return _tmpl("reports.html", request, {
        "date": None,
        "files": files,
        "body": None,
    })


@app.get("/reports/{date}", dependencies=[Depends(auth.require_session)])
def report_view(request: Request, date: str):
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", date):
        raise HTTPException(status_code=400, detail="date must be YYYY-MM-DD")
    path = _REPORTS_DIR / f"{date}.md"
    body = None
    if path.exists():
        try:
            body = md.markdown(path.read_text(), extensions=["tables", "fenced_code"])
        except OSError:
            body = None
    files = []
    if _REPORTS_DIR.exists():
        files = sorted(
            (p.name for p in _REPORTS_DIR.iterdir() if p.suffix == ".md"),
            reverse=True,
        )
    return _tmpl("reports.html", request, {
        "date": date,
        "files": files,
        "body": body,
    })


_SQUID_DENIED_RE = re.compile(
    r"(?P<ts>\d+\.\d+)\s+\d+\s+\S+\s+TCP_DENIED/(?P<code>\d+)\s+\S+\s+"
    r"\S+\s+(?P<host_port>\S+)"
)


@app.get("/blocked", dependencies=[Depends(auth.require_session)])
def blocked_page(request: Request):
    entries: list[dict] = []
    by_host: dict[str, int] = {}
    if _SQUID_LOG_PATH.exists():
        try:
            with _SQUID_LOG_PATH.open("r", errors="ignore") as f:
                # tail: read last 512 KiB — squid entries are ~150 bytes.
                f.seek(0, 2)
                size = f.tell()
                f.seek(max(0, size - 512 * 1024))
                text = f.read()
            for line in text.splitlines()[-2000:]:
                m = _SQUID_DENIED_RE.search(line)
                if not m:
                    continue
                host = m.group("host_port").split(":")[0]
                ts = datetime.fromtimestamp(float(m.group("ts")), tz=timezone.utc)
                entries.append({"host": host, "when": ts.isoformat(timespec="seconds")})
                by_host[host] = by_host.get(host, 0) + 1
        except OSError:
            pass
    entries = entries[-200:]
    entries.reverse()
    grouped = sorted(by_host.items(), key=lambda kv: kv[1], reverse=True)
    return _tmpl("blocked.html", request, {
        "entries": entries,
        "grouped": grouped,
        "log_path": str(_SQUID_LOG_PATH),
        "log_exists": _SQUID_LOG_PATH.exists(),
    })


# --------------------------------------------------------------------------
# Fragments — htmx polling widgets. Each returns HTML for one component,
# no base template. Overview.html embeds these for first paint and htmx
# swaps them independently thereafter. See plan @
# /home/aaron/.claude/plans/buzzing-riding-noodle.md
# --------------------------------------------------------------------------

def _fragment(name: str, request: Request, ctx: dict) -> HTMLResponse:
    """Render a partial from templates/_fragments/. Bypasses _tmpl so we
    don't consume flash cookies on every poll (they'd disappear before
    the operator could read them)."""
    tpl = _TEMPLATES.get_template(f"_fragments/{name}.html")
    return HTMLResponse(tpl.render(request=request, town=_TOWN_SLUG, **ctx))


@app.get("/fragments/live-strip",
         dependencies=[Depends(auth.require_session)])
def frag_live_strip(request: Request):
    return _fragment("live_strip", request, {
        "running": db.orchestrator_running(),
        "next_fires": db.next_cron_fires(limit=1),
    })


@app.get("/fragments/overview-stats",
         dependencies=[Depends(auth.require_session)])
def frag_overview_stats(request: Request):
    llm_health = {}
    if _LLM_HEALTH_PATH.exists():
        try:
            llm_health = json.loads(_LLM_HEALTH_PATH.read_text())
        except (json.JSONDecodeError, OSError):
            llm_health = {"status": "unreadable"}
    return _fragment("overview_stats", request, {
        "state": db.agent_state(),
        "events": db.event_counts(),
        "sources": db.source_counts(),
        "candidates": db.candidate_counts(),
        "entities": db.entity_counts(),
        "llm_health": llm_health,
    })


@app.get("/fragments/last-tick",
         dependencies=[Depends(auth.require_session)])
def frag_last_tick(request: Request):
    return _fragment("last_tick", request, {
        "last_tick": db.last_tick_summary(),
    })


@app.get("/fragments/next-fires",
         dependencies=[Depends(auth.require_session)])
def frag_next_fires(request: Request):
    return _fragment("next_fires", request, {
        "next_fires": db.next_cron_fires(limit=6),
    })


@app.get("/fragments/recent-cron",
         dependencies=[Depends(auth.require_session)])
def frag_recent_cron(request: Request):
    return _fragment("recent_cron", request, {
        "recent_cron": db.cron_output_files(limit=8),
    })


@app.get("/fragments/source-runs",
         dependencies=[Depends(auth.require_session)])
def frag_source_runs(request: Request):
    return _fragment("source_runs", request, {
        "source_runs": db.recent_source_runs(hours=24),
    })


# ----- Band 2 + Band 3 event fragments ----------------------------------

def _empty_state_branch() -> dict:
    """Walk the 8-branch causal cascade and return (branch_key, message,
    action) for the first true branch. Used by the events_empty_state
    template."""
    from datetime import datetime, timezone
    from pathlib import Path
    import os as _os, yaml as _yaml, json as _json

    # 1. Orchestrator stuck
    if db.orchestrator_stuck(min_ago=90):
        state = db.agent_state()
        last = state.get("last_tick_at")
        return {
            "branch": "stuck",
            "message": f"The agent hasn't ticked since {last or 'ever'} — likely broken. Check the tiles above for the last-status pill.",
            "action_label": "Force tick",
            "action_url": "/actions/tick",
        }

    # 2. No candidates yet
    counts = db.candidate_counts()
    total_candidates = sum(r["n"] for r in counts)
    if total_candidates == 0:
        return {
            "branch": "no_candidates",
            "message": "0 discovery candidates so far. Kick a sweep, or add seed URLs to town-profile.yaml.",
            "action_label": "Sweep businesses",
            "action_url": "/actions/sweep",
            "action_kind": "business",
        }

    # 3. Candidates but none verified
    verified = sum(r["n"] for r in counts if r["status"] == "verified")
    if verified == 0:
        return {
            "branch": "no_verified",
            "message": f"{total_candidates} candidates found, 0 verified yet. Force a tick to advance the `verifying` phase.",
            "action_label": "Force tick",
            "action_url": "/actions/tick",
        }

    # 4. Verified but no entities promoted
    entities = db.entity_counts()
    total_entities = sum(e["total"] for e in entities)
    if total_entities == 0:
        return {
            "branch": "no_entities",
            "message": f"{verified} candidates verified, waiting for promotion. Force a tick to advance `researching`.",
            "action_label": "Force tick",
            "action_url": "/actions/tick",
        }

    # 5. Entities but no sources harvested
    sources = db.source_counts()
    if (sources.get("approved") or 0) == 0:
        return {
            "branch": "no_sources",
            "message": f"{total_entities} entities promoted, no event feeds harvested yet.",
            "action_label": "Harvest events",
            "action_url": "/actions/harvest-events",
        }

    # 6. Sources approved but never fetched
    runs = db.recent_source_runs(hours=24 * 30)
    total_runs = sum(r.get("runs", 0) for r in runs)
    if total_runs == 0:
        return {
            "branch": "no_runs",
            "message": f"{sources['approved']} sources approved, waiting on `event-collect` cron.",
            "action_label": "Fetch now",
            "action_url": "/actions/fetch-all",  # wired below
        }

    # 7. Fetched but 0 events (still true because we're in the empty state)
    events = db.event_counts()
    if events.get("total", 0) == 0:
        return {
            "branch": "no_events",
            "message": f"{sources['approved']} sources fetched, 0 events landed. Feeds may be broken (see silent-sources panel) or genuinely quiet.",
            "action_label": None,
            "action_url": None,
        }

    # 8. Events exist but none upcoming
    if events.get("upcoming", 0) == 0:
        return {
            "branch": "no_upcoming",
            "message": f"{events['total']} events exist but all are past. Force a fetch to pull the next window.",
            "action_label": "Fetch now",
            "action_url": "/actions/fetch-all",
        }

    return {"branch": "ok", "message": "", "action_label": None, "action_url": None}


@app.get("/fragments/events/upcoming",
         dependencies=[Depends(auth.require_session)])
def frag_events_upcoming(request: Request, window: int = 30):
    events_list = db.events_upcoming(window_days=window)
    empty_state = _empty_state_branch() if not events_list else None
    return _fragment("events_upcoming", request, {
        "events_list": events_list,
        "window": window,
        "empty_state": empty_state,
    })


@app.get("/fragments/events/heatmap",
         dependencies=[Depends(auth.require_session)])
def frag_events_heatmap(request: Request, window: int = 30):
    days = db.events_by_day(days=window)
    max_count = max((d["count"] for d in days), default=0)
    return _fragment("events_heatmap", request, {
        "days": days,
        "max_count": max_count,
        "window": window,
    })


@app.get("/fragments/events/by-source",
         dependencies=[Depends(auth.require_session)])
def frag_events_by_source(request: Request):
    return _fragment("events_by_source", request, {
        "by_source": db.events_by_source(days=30, limit=10),
    })


@app.get("/fragments/events/silent-sources",
         dependencies=[Depends(auth.require_session)])
def frag_events_silent_sources(request: Request):
    return _fragment("silent_sources", request, {
        "silent": db.silent_sources(days=30),
    })


@app.get("/fragments/events/coverage-gaps",
         dependencies=[Depends(auth.require_session)])
def frag_events_coverage_gaps(request: Request):
    return _fragment("coverage_estimates", request, {
        "gaps": db.venues_without_events(limit=20),
    })


@app.get("/fragments/events/ticker",
         dependencies=[Depends(auth.require_session)])
def frag_events_ticker(request: Request):
    return _fragment("events_ticker", request, {
        "recent": db.events_recent_ingested(limit=20),
    })


@app.get("/fragments/events/hero-count",
         dependencies=[Depends(auth.require_session)])
def frag_events_hero(request: Request):
    return _fragment("events_hero", request, {
        "events": db.event_counts(),
    })


# ----- Row actions on events / sources / entities -----------------------

@app.post("/events/{event_uid}/hide",
          dependencies=[Depends(auth.require_session)])
def event_hide(event_uid: str, request: Request):
    db.set_event_hidden(event_uid, True)
    row = db.event_by_uid(event_uid)
    if not row:
        raise HTTPException(status_code=404, detail="event not found")
    return _fragment("_event_row", request, {"e": row})


@app.post("/events/{event_uid}/unhide",
          dependencies=[Depends(auth.require_session)])
def event_unhide(event_uid: str, request: Request):
    db.set_event_hidden(event_uid, False)
    row = db.event_by_uid(event_uid)
    if not row:
        raise HTTPException(status_code=404, detail="event not found")
    return _fragment("_event_row", request, {"e": row})


@app.post("/sources/{name}/refetch",
          dependencies=[Depends(auth.require_session)])
def source_refetch(name: str, request: Request):
    """Kick event-ingest ingest --only <name>. Fire-and-forget."""
    ok, err = skills.spawn_skill(
        "event-ingest", "ingest.py",
        ["ingest", "--only", name],
    )
    resp = RedirectResponse(url="/", status_code=303)
    if ok:
        _flash(resp, "ok", f"Refetching source '{name}' in background.")
    else:
        _flash(resp, "error", f"could not start refetch: {err}")
    return resp


@app.post("/actions/fetch-all",
          dependencies=[Depends(auth.require_session)])
def action_fetch_all(request: Request):
    """Kick event-ingest ingest across all approved sources. Empty-state
    cascade branches 6 and 8 wire to this."""
    ok, err = skills.spawn_skill("event-ingest", "ingest.py", ["ingest"])
    resp = RedirectResponse(url="/", status_code=303)
    if ok:
        _flash(resp, "ok", "Fetching all approved sources in background.")
    else:
        _flash(resp, "error", f"could not start fetch: {err}")
    return resp


@app.post("/entities/{kind}/{uid}/harvest",
          dependencies=[Depends(auth.require_session)])
def entity_harvest(kind: str, uid: str, request: Request):
    """Targeted harvest against one entity from the coverage-gaps panel."""
    ok, err = skills.spawn_skill(
        "event-source-harvest", "harvest.py",
        ["--only-entity", uid, "--kind", kind if kind in ("business", "community") else "both",
         "--force-rescan"],
    )
    resp = RedirectResponse(url="/", status_code=303)
    if ok:
        _flash(resp, "ok", f"Harvest of {kind}/{uid[:12]}… running in background.")
    else:
        _flash(resp, "error", f"could not start entity harvest: {err}")
    return resp


@app.post("/events/add-manual",
          dependencies=[Depends(auth.require_session)])
def event_add_manual(request: Request,
                     name: str = Form(...),
                     date: str = Form(...),
                     venue_name: str = Form(""),
                     description: str = Form(""),
                     source_url: str = Form(""),
                     hosting_group: str = Form("")):
    """Wraps event-ingest add-manual. Synchronous (add is fast)."""
    args = ["add-manual",
            "--name", name,
            "--date", date]
    if venue_name:
        args += ["--venue-name", venue_name]
    if description:
        args += ["--description", description]
    if source_url:
        args += ["--source-url", source_url]
    if hosting_group:
        args += ["--hosting-group", hosting_group]
    rc, out, err = skills._skill("event-ingest", "ingest.py", args, timeout_s=30)
    resp = RedirectResponse(url="/", status_code=303)
    if rc == 0:
        _flash(resp, "ok", f"Added: {name} on {date}")
    else:
        _flash(resp, "error", f"add-manual failed (rc={rc}): {err[:200]}")
    return resp


# --------------------------------------------------------------------------
# Actions — force ticks, sweeps, summaries.
# --------------------------------------------------------------------------

@app.post("/actions/tick", dependencies=[Depends(auth.require_session)])
def action_tick(request: Request):
    """Fire-and-forget: kick a tick subprocess and redirect immediately.
    Real ticks (with discovery sweeps) take minutes — a synchronous wait
    times out the browser. The Overview live-dot shows progress while the
    subprocess runs; the page auto-refreshes every 10s when the lock is held."""
    ok, err = skills.spawn_skill("onboarding-orchestrator", "orchestrate.py", ["tick"])
    resp = RedirectResponse(url="/", status_code=303)
    if ok:
        _flash(resp, "ok",
               "Tick started. Watch the live indicator on Overview — the "
               "page auto-refreshes while a tick is running.")
    else:
        _flash(resp, "error", f"could not start tick: {err}")
    return resp


@app.post("/actions/sweep", dependencies=[Depends(auth.require_session)])
def action_sweep(request: Request, kind: str = Form(...)):
    ok, err = skills.spawn_skill(
        f"{kind}-discovery", "discover.py",
        ["sweep", "--per-source", "10"],
    )
    resp = RedirectResponse(url="/", status_code=303)
    if ok:
        _flash(resp, "ok",
               f"{kind} sweep started in background. "
               f"Watch the live indicator on Overview.")
    else:
        _flash(resp, "error", f"could not start {kind} sweep: {err}")
    return resp


@app.post("/actions/harvest-events",
          dependencies=[Depends(auth.require_session)])
def action_harvest_events(request: Request):
    ok, err = skills.spawn_skill(
        "event-source-harvest", "harvest.py",
        ["--limit", "20"],
    )
    resp = RedirectResponse(url="/", status_code=303)
    if ok:
        _flash(resp, "ok",
               "Event-source harvest started in background. Takes 1-3 min. "
               "New sources will appear on the Sources page; the Events "
               "count grows after the next event-ingest cron.")
    else:
        _flash(resp, "error", f"could not start harvest: {err}")
    return resp


@app.post("/actions/summary", dependencies=[Depends(auth.require_session)])
def action_summary(request: Request):
    """Digest is cheap — keep it synchronous so the user sees the result
    file linked in the flash message immediately."""
    rc, out, err = skills.orchestrate_summary()
    resp = RedirectResponse(url="/reports", status_code=303)
    if rc == 0:
        _flash(resp, "ok", "Nightly summary generated. See Reports.")
    else:
        _flash(resp, "error", f"summary failed (rc={rc}): {err[:200]}")
    return resp


# ==========================================================================
# /v2 — event-first dashboard (rolls out alongside the legacy dashboard;
# swap `/` to render v2 once operator has verified it).
# ==========================================================================

_V2_FILTERS = ("pending", "approved", "declined", "shipped")


def _render_v2_partial(name: str, request: Request, ctx: dict) -> HTMLResponse:
    """Render templates/v2/_partials/<name>.html."""
    tpl = _TEMPLATES.get_template(f"v2/_partials/{name}.html")
    return HTMLResponse(tpl.render(request=request, town=_TOWN_SLUG, **ctx))


def _render_v2_fragment(name: str, request: Request, ctx: dict) -> HTMLResponse:
    """Render templates/v2/_fragments/<name>.html — sibling of the legacy
    _fragment() helper which hardcodes _fragments/ prefix (used only by
    v1 endpoints)."""
    tpl = _TEMPLATES.get_template(f"v2/_fragments/{name}.html")
    return HTMLResponse(tpl.render(request=request, town=_TOWN_SLUG, **ctx))


def _v2_events_for_filter(filt: str, limit: int = 200) -> list[dict]:
    if filt == "shipped":
        return db_v2.events_recently_shipped(limit=limit)
    if filt in ("pending", "approved", "declined"):
        return db_v2.events_by_approval(filt, limit=limit)
    return []


def _v2_approval_ctx(filt: str) -> dict:
    filt = filt if filt in _V2_FILTERS else "pending"
    events = _v2_events_for_filter(filt)
    empty_state = db_v2.empty_state_branch() if filt == "pending" and not events else None
    return {"events": events, "filter": filt, "empty_state": empty_state}


@app.get("/v2", dependencies=[Depends(auth.require_session)])
def v2_overview(request: Request, filter: str = "pending"):
    filt = filter if filter in _V2_FILTERS else "pending"
    chips = db_v2.headline_chips()
    approval = _v2_approval_ctx(filt)
    seeds_approved = db_v2.event_seeds_by_status("approved")
    seeds_proposed = db_v2.event_seeds_by_status("proposed")
    seed_totals = db_v2.seed_counts()
    wq_counts = db_v2.workqueue_counts_by_action()
    state_counts = db_v2.event_counts_by_state()
    wq_max = max((r["n"] for r in wq_counts), default=1)
    return _tmpl("v2/overview.html", request, {
        "chips": chips,
        "filter": filt,
        "events": approval["events"],
        "empty_state": approval["empty_state"],
        "seeds_approved": seeds_approved,
        "seeds_proposed": seeds_proposed,
        "seed_totals": seed_totals,
        "wq_counts": wq_counts,
        "wq_max": wq_max,
        "state_counts": state_counts,
        "activity": db_v2.recent_activity(limit=20),
        "current_task": db_v2.current_agent_task(),
        "events_in_flight_ctx": db_v2.events_in_flight(),
        "events_shipped_ctx": db_v2.events_recently_shipped(),
        "stuck_ctx": db_v2.workqueue_stuck(),
    })


# ----- /v2 fragments ------------------------------------------------------

@app.get("/v2/fragments/workqueue",
         dependencies=[Depends(auth.require_session)])
def v2_frag_workqueue(request: Request):
    wq = db_v2.workqueue_counts_by_action()
    return _render_v2_fragment("workqueue", request, {
        "wq_counts": wq,
        "wq_max": max((r["n"] for r in wq), default=1),
        "state_counts": db_v2.event_counts_by_state(),
    })


@app.get("/v2/fragments/activity",
         dependencies=[Depends(auth.require_session)])
def v2_frag_activity(request: Request):
    return _render_v2_fragment("activity", request, {
        "activity": db_v2.recent_activity(limit=20),
        "current_task": db_v2.current_agent_task(),
    })


@app.get("/v2/fragments/approval",
         dependencies=[Depends(auth.require_session)])
def v2_frag_approval(request: Request, filter: str = "pending"):
    return _render_v2_fragment("approval", request, _v2_approval_ctx(filter))


@app.get("/v2/fragments/in-flight",
         dependencies=[Depends(auth.require_session)])
def v2_frag_in_flight(request: Request):
    return _render_v2_fragment("in_flight", request, {
        "events": db_v2.events_in_flight(),
    })


@app.get("/v2/fragments/shipped",
         dependencies=[Depends(auth.require_session)])
def v2_frag_shipped(request: Request):
    return _render_v2_fragment("shipped", request, {
        "events": db_v2.events_recently_shipped(),
    })


@app.get("/v2/fragments/stuck",
         dependencies=[Depends(auth.require_session)])
def v2_frag_stuck(request: Request):
    return _render_v2_fragment("stuck", request, {
        "stuck": db_v2.workqueue_stuck(),
    })


@app.get("/v2/fragments/seeds-approved",
         dependencies=[Depends(auth.require_session)])
def v2_frag_seeds_approved(request: Request):
    return _render_v2_fragment("seeds_approved", request, {
        "seeds": db_v2.event_seeds_by_status("approved"),
    })


@app.get("/v2/fragments/seeds-proposed",
         dependencies=[Depends(auth.require_session)])
def v2_frag_seeds_proposed(request: Request):
    return _render_v2_fragment("seeds_proposed", request, {
        "seeds": db_v2.event_seeds_by_status("proposed"),
    })


# ----- /v2 event row actions ---------------------------------------------

@app.post("/v2/events/{event_uid}/approve",
          dependencies=[Depends(auth.require_session)])
def v2_event_approve(event_uid: str, request: Request,
                     note: Optional[str] = Form(None)):
    # set_event_approval flips state→approved AND enqueues the first action
    # atomically (kick-on-approve, no 10-min heartbeat wait).
    db_v2.set_event_approval(event_uid, "approved", note)
    row = db_v2.event_by_uid_v2(event_uid)
    if not row:
        raise HTTPException(status_code=404, detail="event not found")
    return _render_v2_partial("_event_card", request, {"e": row})


@app.post("/v2/events/{event_uid}/decline",
          dependencies=[Depends(auth.require_session)])
def v2_event_decline(event_uid: str, request: Request,
                     note: Optional[str] = Form(None)):
    db_v2.set_event_approval(event_uid, "declined", note)
    row = db_v2.event_by_uid_v2(event_uid)
    if not row:
        raise HTTPException(status_code=404, detail="event not found")
    return _render_v2_partial("_event_card", request, {"e": row})


# ----- /v2 seed actions ---------------------------------------------------

@app.post("/v2/seeds/{seed_id}/approve",
          dependencies=[Depends(auth.require_session)])
def v2_seed_approve(seed_id: int, request: Request):
    db_v2.set_seed_status(seed_id, "approved")
    resp = RedirectResponse(url="/v2#seeds", status_code=303)
    _flash(resp, "ok", f"Seed {seed_id} approved.")
    return resp


@app.post("/v2/seeds/{seed_id}/reject",
          dependencies=[Depends(auth.require_session)])
def v2_seed_reject(seed_id: int, request: Request):
    db_v2.set_seed_status(seed_id, "rejected")
    resp = RedirectResponse(url="/v2#seeds", status_code=303)
    _flash(resp, "ok", f"Seed {seed_id} rejected.")
    return resp


@app.post("/v2/seeds/{seed_id}/ack-health",
          dependencies=[Depends(auth.require_session)])
def v2_seed_ack_health(seed_id: int, request: Request):
    resp = RedirectResponse(url="/v2#seeds", status_code=303)
    if db_v2.ack_seed_health(seed_id):
        _flash(resp, "ok", f"Seed {seed_id}: current event count accepted as normal.")
    else:
        _flash(resp, "error", f"No seed {seed_id}.")
    return resp


# ----- /v2 background actions --------------------------------------------

@app.post("/v2/actions/event-find",
          dependencies=[Depends(auth.require_session)])
def v2_action_event_find(request: Request):
    ok, err = skills.spawn_skill("event-find", "find.py", [])
    resp = RedirectResponse(url="/v2", status_code=303)
    if ok:
        _flash(resp, "ok", "event-find running in the background — watch the live activity band.")
    else:
        _flash(resp, "error", f"could not start event-find: {err}")
    return resp


@app.post("/v2/actions/discover-seeds",
          dependencies=[Depends(auth.require_session)])
def v2_action_discover_seeds(request: Request):
    ok, msg = skills.spawn_skill(
        "event-seed-discovery", "discover.py", ["discover", "--limit", "15"],
    )
    resp = RedirectResponse(url="/v2#seeds", status_code=303)
    if ok:
        # msg is the spawn log path — surface the filename so an operator
        # tailing state/spawn-logs/ can find their run.
        log_name = os.path.basename(msg) if msg else ""
        _flash(resp, "ok",
               f"Seed discovery running (~2 min). Watch Live Activity for the "
               f"skill-invoke summary. Log: {log_name}")
    else:
        _flash(resp, "error", f"could not start seed discovery: {msg}")
    return resp


@app.post("/v2/workqueue/{queue_id}/requeue",
          dependencies=[Depends(auth.require_session)])
def v2_workqueue_requeue(queue_id: int, request: Request):
    n = db_v2.workqueue_requeue(queue_id)
    resp = RedirectResponse(url="/v2", status_code=303)
    if n:
        _flash(resp, "ok", f"Requeued workqueue item {queue_id}.")
    else:
        _flash(resp, "error", f"Requeue failed — item {queue_id} not found or already resolved.")
    return resp


# ==========================================================================
# /v2/chat/* — operator ↔ Hermes chat panel.
#
# v1 slash path only (this file). Natural-language routing to Hermes lands
# in M6 (chat_worker.py). Every route auth-gated; the dashboard is
# Tailscale-reachable and unauthed chat = unauthed DB + eventually unauthed
# LLM call.
# ==========================================================================

@app.get("/v2/chat/history", dependencies=[Depends(auth.require_session)])
def v2_chat_history(request: Request,
                    thread_id: str = "default",
                    since_id: Optional[int] = None,
                    limit: int = 100):
    """Incremental poll. since_id is exclusive."""
    msgs = chat_db.history(thread_id=thread_id, since_id=since_id, limit=limit)
    return JSONResponse({"messages": msgs,
                         "latest_id": chat_db.latest_message_id(thread_id)})


@app.post("/v2/chat/send", dependencies=[Depends(auth.require_session)])
def v2_chat_send(request: Request,
                 text: str = Form(...),
                 thread_id: str = Form("default")):
    """Write the operator's message. If it's a slash command, execute
    inline and write the agent reply in the same request — instant path.

    Non-slash messages ALSO write the operator row here, but return a
    stub agent reply directing the operator to try `/help` until M6
    lands the Hermes worker."""
    text = (text or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="empty message")
    if len(text) > 4000:
        raise HTTPException(status_code=400, detail="message too long (max 4000 chars)")

    if chat.is_slash(text):
        # Slash commands are always allowed — they're instant, don't
        # touch Hermes, and thus don't stack on any in-flight LLM turn.
        op_id = chat_db.insert_message("operator", text, thread_id=thread_id)
        reply_body = chat.handle_slash(text)
        agent_id = chat_db.insert_message(
            "agent", reply_body, thread_id=thread_id, status="delivered",
        )
    else:
        # Single-flight guard: if the last agent row on this thread is
        # still 'processing', don't stack another Hermes call — Codex
        # rate limits punish parallel bursts. The operator still gets
        # both messages recorded so history is coherent.
        last_agent = chat_db.latest_by_role("agent", thread_id=thread_id)
        if last_agent and last_agent["status"] == "processing":
            op_id = chat_db.insert_message("operator", text, thread_id=thread_id)
            agent_id = chat_db.insert_message(
                "agent",
                "Still processing your previous message. I'll pick this "
                "up as soon as Hermes replies.",
                thread_id=thread_id, status="delivered",
                error="single-flight guard: prior placeholder still processing",
            )
        else:
            # Session_id is per-thread; stamp both the operator row and
            # the placeholder so future turns can look up the session
            # from either.
            session_id = chat_db.thread_session_id(thread_id)
            op_id = chat_db.insert_message(
                "operator", text,
                thread_id=thread_id, hermes_session_id=session_id,
            )
            agent_id = chat_db.insert_message(
                "agent", "",
                thread_id=thread_id, hermes_session_id=session_id,
                status="processing",
            )
            ok, log_or_err = skills.spawn_chat_worker(op_id, agent_id)
            if not ok:
                # Worker didn't even spawn — fail the placeholder now so
                # the panel doesn't sit at "thinking…" forever.
                chat_db.update_message_status(
                    agent_id,
                    body=f"Couldn't start Hermes worker: {log_or_err}",
                    status="failed",
                    error=log_or_err[:1000],
                    expect_status="processing",
                )

    # HX-Trigger fires a client-side event that the chat panel's message
    # list listens for (hx-trigger="chat-updated from:body") so the new
    # row appears immediately, without waiting for the 2s poll.
    resp = JSONResponse({
        "operator_message_id": op_id,
        "agent_message_id": agent_id,
        "latest_id": agent_id,
    })
    resp.headers["HX-Trigger"] = "chat-updated"
    return resp


@app.get("/v2/fragments/chat-history",
         dependencies=[Depends(auth.require_session)])
def v2_frag_chat_history(request: Request,
                         thread_id: str = "default",
                         since_id: Optional[int] = None,
                         limit: int = 100):
    return _render_v2_fragment("chat_history", request, {
        "messages": chat_db.history(thread_id=thread_id,
                                    since_id=since_id, limit=limit),
    })


@app.get("/v2/fragments/chat-badge",
         dependencies=[Depends(auth.require_session)])
def v2_frag_chat_badge(request: Request, thread_id: str = "default"):
    return _render_v2_fragment("chat_badge", request, {
        "unread": chat_db.unread_count(thread_id=thread_id),
    })


@app.get("/v2/chat/badge", dependencies=[Depends(auth.require_session)])
def v2_chat_badge(request: Request, thread_id: str = "default"):
    return JSONResponse({"unread": chat_db.unread_count(thread_id=thread_id)})


@app.post("/v2/chat/mark-seen", dependencies=[Depends(auth.require_session)])
def v2_chat_mark_seen(request: Request,
                      thread_id: str = Form("default"),
                      up_to_message_id: Optional[int] = Form(None)):
    chat_db.mark_seen(thread_id=thread_id, up_to_message_id=up_to_message_id)
    return JSONResponse({"ok": True,
                         "unread": chat_db.unread_count(thread_id=thread_id)})


# /v2/chat/confirm and /v2/chat/cancel — real destructive-op execution.
# Path an M7-v1 conversational contract couldn't safely take: Hermes runs
# under YOLO mode and once observed bypassing SOUL.md guardrails when the
# operator said "just do it." The widget-based flow puts execution behind
# the operator's actual click, and the executed handlers live in
# `dashboard/intent_handlers.py` — a module NOT mounted into the agent
# container, so Hermes physically cannot reach it.

def _load_proposal(message_id: int) -> tuple[Optional[dict], Optional[dict]]:
    """Return (row, proposal_dict) or (row, None) if the row doesn't
    hold a resolvable proposal. Confirm/cancel share this lookup."""
    row = chat_db.get_message(message_id)
    if not row:
        return (None, None)
    raw = row.get("tool_call_json")
    if not raw:
        return (row, None)
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return (row, None)
    return (row, payload)


@app.post("/v2/chat/confirm", dependencies=[Depends(auth.require_session)])
def v2_chat_confirm(request: Request,
                    message_id: int = Form(...),
                    thread_id: str = Form("default")):
    row, payload = _load_proposal(message_id)
    if not row:
        raise HTTPException(status_code=404, detail="message not found")
    if not payload or payload.get("state") != "proposed":
        raise HTTPException(status_code=409,
                            detail="no proposal is awaiting confirmation on this message")

    proposal = payload.get("proposal") or {}
    ok, err = intent_handlers.validate_proposal(proposal)
    if not ok:
        # Mark the proposal failed so the widget disappears + operator
        # sees why. Do NOT execute.
        payload["state"] = "failed"
        payload["error"] = err
        chat_db.update_message_status(
            message_id, tool_call_json=json.dumps(payload),
        )
        chat_db.insert_message(
            "agent",
            f"Couldn't execute the proposed action — invalid proposal: {err}",
            thread_id=thread_id, status="failed", error=err,
        )
        return JSONResponse({"ok": False, "error": err},
                            status_code=422,
                            headers={"HX-Trigger": "chat-updated"})

    # Optimistic-lock: only proceed if the tool_call_json still shows
    # state='proposed'. A racing second click sees state=confirmed and
    # bails with a 409 before touching the DB.
    payload["state"] = "confirmed"
    payload["confirmed_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    updated = chat_db.update_message_status(
        message_id,
        tool_call_json=json.dumps(payload),
    )
    if not updated:
        raise HTTPException(status_code=409, detail="confirm race — row already updated")

    result = intent_handlers.execute(proposal)

    payload["state"] = "executed"
    payload["result"] = result
    chat_db.update_message_status(
        message_id, tool_call_json=json.dumps(payload),
    )

    # Formulate the follow-up agent message in pure Python — NOT via
    # Hermes. Deterministic, cheap, and honest about what happened.
    kind = proposal.get("kind")
    if kind == "decline_events":
        body = f"Declined {result['declined']} events."
        if result.get("skipped"):
            body += f" Skipped {result['skipped']} (already in a terminal state)."
    elif kind == "approve_events":
        body = (f"Approved {result['approved']} events; "
                f"queued {result['enqueued']} for the pipeline.")
        if result.get("skipped"):
            body += f" Skipped {result['skipped']} (already in a terminal state)."
    elif kind == "promote_businesses":
        body = (f"Approved {result['approved']} business candidates — the next "
                "business-discovery promote run checks live, enriches and ships them.")
        if result.get("skipped"):
            body += f" Skipped {result['skipped']} (no longer verified)."
    elif kind == "decline_businesses":
        body = f"Declined {result['declined']} business candidates; they won't be proposed again."
        if result.get("skipped"):
            body += f" Skipped {result['skipped']} (already decided)."
    elif kind == "confirm_cancellations":
        body = (f"Confirmed {result['confirmed']} cancellations — the next event-cancellations "
                "apply run marks them cancelled on TownLoops.")
        if result.get("skipped"):
            body += f" Skipped {result['skipped']} (no longer flagged)."
    elif kind == "dismiss_cancellations":
        body = f"Dismissed {result['dismissed']} cancellation flags; they won't be flagged again."
        if result.get("skipped"):
            body += f" Skipped {result['skipped']} (no longer flagged)."
    elif kind == "clear_listing_websites":
        body = (f"Confirmed {result['confirmed']} listing-website clears — the next "
                "listing-website-cleanup apply (or the daily entity-live-sync run) clears "
                "them on TownLoops, then locally, and re-enriches them.")
        if result.get("skipped"):
            body += f" Skipped {result['skipped']} (already confirmed or unknown)."
    else:
        body = f"Executed intent {kind}: {result}"
    chat_db.insert_message("agent", body, thread_id=thread_id)

    return JSONResponse({"ok": True, "result": result},
                        headers={"HX-Trigger": "chat-updated"})


@app.post("/v2/chat/cancel", dependencies=[Depends(auth.require_session)])
def v2_chat_cancel(request: Request,
                   message_id: int = Form(...),
                   thread_id: str = Form("default")):
    row, payload = _load_proposal(message_id)
    if not row:
        raise HTTPException(status_code=404, detail="message not found")
    if not payload or payload.get("state") != "proposed":
        raise HTTPException(status_code=409, detail="no proposal to cancel here")
    payload["state"] = "cancelled"
    payload["cancelled_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    updated = chat_db.update_message_status(
        message_id, tool_call_json=json.dumps(payload),
    )
    if not updated:
        raise HTTPException(status_code=409, detail="cancel race")

    proposal = payload.get("proposal") or {}
    kind = str(proposal.get("kind", ""))
    noun = ("business candidates" if kind.endswith("_businesses")
            else "entities" if kind == "clear_listing_websites" else "events")
    chat_db.insert_message(
        "system",
        f"Cancelled — nothing changed. ({proposal.get('count', 0)} {noun} would have been affected.)",
        thread_id=thread_id,
    )
    return JSONResponse({"ok": True},
                        headers={"HX-Trigger": "chat-updated"})
