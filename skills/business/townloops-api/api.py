#!/usr/bin/env python3
"""townloops-api — the agent's contract-of-record with TownLoops's REST API.

Owns three concerns the rest of the agent shouldn't:

  1. Snapshot the API's OpenAPI spec locally so the agent has a stable
     reference for endpoints, filters, ordering, and pagination shapes.
  2. Detect breaking changes across snapshots and freeze the affected
     resources — engine/townloops_api.Client refuses to POST/PATCH/DELETE
     on a frozen resource until the operator clears the freeze.
  3. Provide a `query` command that issues paginated, filtered, ordered GETs
     against any resource — the general-purpose read path, contrasted with
     the write-heavy ingest / enrichment flow that has its own client
     methods.

Snapshot state:
  state/openapi.json           — current live snapshot (byte-identical to server)
  state/openapi.previous.json  — the snapshot before the last `snapshot` run
  state/api_freeze.json        — {frozen_at, reason, resources: [...]}

Subcommands:
  snapshot [--url URL]
  diff [--against PATH]
  check [--url URL]                # snapshot + diff + freeze/alert
  describe <resource>
  query <resource> [--filter K=V ...] [--from ISO] [--to ISO]
                   [--order-by KEY] [--limit N] [--offset N] [--all]

Env:
  TOWNLOOPS_OPENAPI_URL   default https://townloops.com/api/v1/openapi.json
  TOWNLOOPS_SWAGGER_URL   default https://townloops.com/api/v1/docs
  TOWNLOOPS_API_URL       used by `query` (base URL for GETs)
  TOWNLOOPS_API_KEY       used by `query`
  TOWNLOOPS_TOWN_SLUG     used by `query`
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

import requests


DEFAULT_OPENAPI_URL = "https://townloops.com/api/v1/openapi.json"
DEFAULT_SWAGGER_URL = "https://townloops.com/api/v1/docs"

HERMES_HOME = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes")))
STATE_DIR = HERMES_HOME / "state"
OPENAPI_PATH = Path(os.environ.get("TOWNLOOPS_OPENAPI_SNAPSHOT",
                                   STATE_DIR / "openapi.json"))
PREVIOUS_PATH = Path(str(OPENAPI_PATH).replace(".json", ".previous.json"))
FREEZE_PATH = Path(os.environ.get("TOWNLOOPS_API_FREEZE_PATH",
                                  STATE_DIR / "api_freeze.json"))


def _iso_utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# Snapshot
# --------------------------------------------------------------------------

def _openapi_url(cli_arg: Optional[str] = None) -> str:
    return (cli_arg or os.environ.get("TOWNLOOPS_OPENAPI_URL")
            or DEFAULT_OPENAPI_URL)


def _swagger_url() -> str:
    return os.environ.get("TOWNLOOPS_SWAGGER_URL") or DEFAULT_SWAGGER_URL


def _fetch_openapi(url: str) -> dict:
    r = requests.get(url, timeout=20, headers={"Accept": "application/json"})
    r.raise_for_status()
    return r.json()


def _hash_spec(spec: dict) -> str:
    return hashlib.sha256(
        json.dumps(spec, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()


def cmd_snapshot(args) -> int:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    url = _openapi_url(args.url)
    spec = _fetch_openapi(url)
    new_hash = _hash_spec(spec)

    prev_hash: Optional[str] = None
    if OPENAPI_PATH.exists():
        try:
            prev_hash = _hash_spec(json.loads(OPENAPI_PATH.read_text()))
        except (OSError, json.JSONDecodeError):
            prev_hash = None
        # Rotate current to previous.
        PREVIOUS_PATH.write_text(OPENAPI_PATH.read_text())

    OPENAPI_PATH.write_text(json.dumps(spec, indent=2, ensure_ascii=False))
    changed = (prev_hash != new_hash)

    print(json.dumps({
        "url": url,
        "openapi_version": spec.get("openapi"),
        "title": (spec.get("info") or {}).get("title"),
        "spec_hash": new_hash,
        "previous_hash": prev_hash,
        "changed": changed,
        "paths": len(spec.get("paths") or {}),
        "schemas": len(((spec.get("components") or {}).get("schemas") or {})),
        "snapshot_path": str(OPENAPI_PATH),
    }, ensure_ascii=False))
    return 0


# --------------------------------------------------------------------------
# Diff — structured, breaking-aware
# --------------------------------------------------------------------------

_RESOURCE_RE = re.compile(r"/api/v\d+/towns/\{town_slug\}/(?P<resource>[a-z0-9\-]+)")


def _resource_of_path(path: str) -> Optional[str]:
    m = _RESOURCE_RE.match(path)
    return m.group("resource") if m else None


def _spec_paths(spec: dict) -> dict[str, dict]:
    return spec.get("paths") or {}


def _spec_schemas(spec: dict) -> dict[str, dict]:
    return ((spec.get("components") or {}).get("schemas") or {})


def _required_fields(schema: dict) -> set[str]:
    req = schema.get("required")
    return set(req) if isinstance(req, list) else set()


def _property_types(schema: dict) -> dict[str, Any]:
    props = schema.get("properties") or {}
    out: dict[str, Any] = {}
    for name, sub in props.items():
        if not isinstance(sub, dict):
            continue
        # Type may be str, list (nullable), or absent (composite/ref).
        t = sub.get("type")
        if isinstance(t, list):
            out[name] = tuple(sorted(t))
        elif t:
            out[name] = t
        elif "$ref" in sub:
            out[name] = f"$ref:{sub['$ref']}"
        elif sub.get("anyOf") or sub.get("oneOf") or sub.get("allOf"):
            out[name] = "composite"
        else:
            out[name] = "unknown"
    return out


def _diff_schema(name: str, old: dict, new: dict) -> list[dict]:
    """Return a list of change records for one schema. Each record carries
    `breaking: bool` so callers can decide freezing."""
    out: list[dict] = []
    old_req, new_req = _required_fields(old), _required_fields(new)
    added_required = new_req - old_req
    removed_required = old_req - new_req
    if added_required:
        out.append({
            "schema": name,
            "kind": "required_added",
            "fields": sorted(added_required),
            "breaking": True,
        })
    if removed_required:
        out.append({
            "schema": name,
            "kind": "required_removed",
            "fields": sorted(removed_required),
            "breaking": False,
        })
    old_props, new_props = _property_types(old), _property_types(new)
    added_props = set(new_props) - set(old_props)
    removed_props = set(old_props) - set(new_props)
    if added_props:
        out.append({
            "schema": name, "kind": "property_added",
            "fields": sorted(added_props), "breaking": False,
        })
    if removed_props:
        out.append({
            "schema": name, "kind": "property_removed",
            "fields": sorted(removed_props), "breaking": True,
        })
    for name_prop in sorted(set(old_props) & set(new_props)):
        if old_props[name_prop] != new_props[name_prop]:
            out.append({
                "schema": name, "kind": "property_type_changed",
                "field": name_prop,
                "old_type": str(old_props[name_prop]),
                "new_type": str(new_props[name_prop]),
                "breaking": True,
            })
    return out


def diff_specs(old: dict, new: dict) -> dict:
    """Structured OpenAPI diff. Breaking-aware; keys grouped for readability."""
    old_paths, new_paths = _spec_paths(old), _spec_paths(new)
    added_paths = sorted(set(new_paths) - set(old_paths))
    removed_paths = sorted(set(old_paths) - set(new_paths))

    changed_ops: list[dict] = []
    for path in sorted(set(old_paths) & set(new_paths)):
        old_ops = {m for m in old_paths[path] if m in
                   ("get", "post", "patch", "put", "delete")}
        new_ops = {m for m in new_paths[path] if m in
                   ("get", "post", "patch", "put", "delete")}
        for op in sorted(old_ops - new_ops):
            changed_ops.append({
                "path": path, "kind": "operation_removed", "method": op,
                "breaking": True,
            })
        for op in sorted(new_ops - old_ops):
            changed_ops.append({
                "path": path, "kind": "operation_added", "method": op,
                "breaking": False,
            })

    schema_changes: list[dict] = []
    old_schemas, new_schemas = _spec_schemas(old), _spec_schemas(new)
    for name in sorted(set(old_schemas) - set(new_schemas)):
        schema_changes.append({
            "schema": name, "kind": "schema_removed", "breaking": True,
        })
    for name in sorted(set(new_schemas) - set(old_schemas)):
        schema_changes.append({
            "schema": name, "kind": "schema_added", "breaking": False,
        })
    for name in sorted(set(old_schemas) & set(new_schemas)):
        schema_changes.extend(_diff_schema(name, old_schemas[name], new_schemas[name]))

    breaking = (
        [{"path": p, "kind": "path_removed", "breaking": True} for p in removed_paths]
        + [c for c in changed_ops if c["breaking"]]
        + [c for c in schema_changes if c["breaking"]]
    )
    non_breaking = (
        [{"path": p, "kind": "path_added", "breaking": False} for p in added_paths]
        + [c for c in changed_ops if not c["breaking"]]
        + [c for c in schema_changes if not c["breaking"]]
    )

    # Map breaking changes → affected resources (best-effort).
    frozen_resources: set[str] = set()
    for p in removed_paths:
        r = _resource_of_path(p)
        if r:
            frozen_resources.add(r)
    for c in changed_ops:
        if c["breaking"]:
            r = _resource_of_path(c["path"])
            if r:
                frozen_resources.add(r)
    for c in schema_changes:
        if c["breaking"]:
            # Map schema → resource via heuristic on the name
            #   BusinessPayload → businesses, EventPayload → events, ...
            slug = _schema_name_to_resource(c["schema"])
            if slug:
                frozen_resources.add(slug)

    return {
        "breaking": breaking,
        "non_breaking": non_breaking,
        "resources_to_freeze": sorted(frozen_resources),
        "counts": {
            "breaking": len(breaking),
            "non_breaking": len(non_breaking),
            "paths_added": len(added_paths),
            "paths_removed": len(removed_paths),
        },
    }


_SCHEMA_TO_RESOURCE = {
    "eventpayload":         "events",
    "venuepayload":         "venues",
    "businesspayload":      "businesses",
    "communitypayload":     "communities",
    "performerpayload":     "performers",
    "creatorpayload":       "creators",
    "sponsorpayload":       "sponsors",
    "adslotpayload":        "ad-slots",
    "categorypayload":      "categories",
}


def _schema_name_to_resource(name: str) -> Optional[str]:
    return _SCHEMA_TO_RESOURCE.get(name.lower().replace("_", ""))


def cmd_diff(args) -> int:
    if not OPENAPI_PATH.exists():
        print(json.dumps({"error": f"no snapshot at {OPENAPI_PATH}"}), file=sys.stderr)
        return 2
    current = json.loads(OPENAPI_PATH.read_text())
    against_path = Path(args.against) if args.against else PREVIOUS_PATH
    if not against_path.exists():
        print(json.dumps({"error": f"no previous snapshot at {against_path}"}),
              file=sys.stderr)
        return 3
    previous = json.loads(against_path.read_text())
    result = diff_specs(previous, current)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0


# --------------------------------------------------------------------------
# check — snapshot + diff + freeze + Telegram
# --------------------------------------------------------------------------

def _telegram_notify(text: str) -> None:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    if not (token and chat):
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat, "text": text},
            timeout=10,
        )
    except requests.RequestException:
        pass


def _write_freeze(resources: list[str], reason: str) -> None:
    FREEZE_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "frozen_at": _iso_utc_now(),
        "reason": reason,
        "resources": sorted(set(resources)),
    }
    # Merge with existing freeze if present — don't clear other resources.
    if FREEZE_PATH.exists():
        try:
            prev = json.loads(FREEZE_PATH.read_text())
            payload["resources"] = sorted(set(payload["resources"])
                                           | set(prev.get("resources") or []))
        except (OSError, json.JSONDecodeError):
            pass
    FREEZE_PATH.write_text(json.dumps(payload, indent=2, ensure_ascii=False))


def cmd_check(args) -> int:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    url = _openapi_url(args.url)
    town = os.environ.get("TOWNLOOPS_TOWN_SLUG", "?")

    new_spec = _fetch_openapi(url)
    new_hash = _hash_spec(new_spec)

    previous_spec: Optional[dict] = None
    prev_hash: Optional[str] = None
    if OPENAPI_PATH.exists():
        try:
            previous_spec = json.loads(OPENAPI_PATH.read_text())
            prev_hash = _hash_spec(previous_spec)
        except (OSError, json.JSONDecodeError):
            previous_spec = None
        # rotate
        PREVIOUS_PATH.write_text(OPENAPI_PATH.read_text())

    OPENAPI_PATH.write_text(json.dumps(new_spec, indent=2, ensure_ascii=False))

    if not previous_spec:
        print(json.dumps({
            "town": town, "first_snapshot": True,
            "spec_hash": new_hash,
            "paths": len(_spec_paths(new_spec)),
        }))
        return 0

    if new_hash == prev_hash:
        print(json.dumps({"town": town, "unchanged": True, "spec_hash": new_hash}))
        return 0

    result = diff_specs(previous_spec, new_spec)
    breaking = result["breaking"]
    to_freeze = result["resources_to_freeze"]

    frozen_now: list[str] = []
    if breaking and to_freeze:
        _write_freeze(to_freeze, reason=f"breaking change vs {prev_hash[:12]}")
        frozen_now = to_freeze
        _telegram_notify(
            f"[{town}] TownLoops API breaking change — freezing writes on: "
            f"{', '.join(to_freeze)}. Run "
            "`townloops-api describe <resource>` and clear "
            "state/api_freeze.json when payloads are updated."
        )
    elif not breaking:
        _telegram_notify(
            f"[{town}] TownLoops API updated (additive): "
            f"+{len(result['non_breaking'])} non-breaking changes. No freeze."
        )

    print(json.dumps({
        "town": town,
        "spec_hash": new_hash,
        "previous_hash": prev_hash,
        "counts": result["counts"],
        "breaking": breaking,
        "non_breaking": result["non_breaking"][:10],
        "frozen_resources": frozen_now,
        "swagger_url": _swagger_url(),
    }, ensure_ascii=False))
    return 0


# --------------------------------------------------------------------------
# describe — dump filter/order/pagination shape for a resource
# --------------------------------------------------------------------------

def cmd_describe(args) -> int:
    if not OPENAPI_PATH.exists():
        print(json.dumps({"error": "no snapshot; run `townloops-api snapshot` first"}),
              file=sys.stderr)
        return 2
    spec = json.loads(OPENAPI_PATH.read_text())
    paths = _spec_paths(spec)

    resource = args.resource.strip("/")
    matches: list[dict] = []
    for path, ops in paths.items():
        r = _resource_of_path(path)
        if r != resource:
            continue
        for method, op in ops.items():
            if method not in ("get", "post", "patch", "put", "delete"):
                continue
            params = []
            for prm in (op.get("parameters") or []):
                sch = prm.get("schema") or {}
                params.append({
                    "name": prm.get("name"),
                    "in": prm.get("in"),
                    "required": prm.get("required", False),
                    "type": sch.get("type"),
                    "enum": sch.get("enum"),
                    "description": prm.get("description"),
                })
            req_body_schema = None
            body = op.get("requestBody") or {}
            content = (body.get("content") or {}).get("application/json") or {}
            if "schema" in content:
                req_body_schema = content["schema"].get("$ref") or content["schema"]
            matches.append({
                "path": path, "method": method.upper(),
                "operationId": op.get("operationId"),
                "summary": op.get("summary"),
                "parameters": params,
                "request_body_schema": req_body_schema,
            })
    if not matches:
        print(json.dumps({"error": f"no operations found for resource {resource!r}"}),
              file=sys.stderr)
        return 4
    print(json.dumps({
        "resource": resource,
        "swagger_url": _swagger_url(),
        "operations": matches,
    }, indent=2, ensure_ascii=False))
    return 0


# --------------------------------------------------------------------------
# query — general-purpose read helper (filter / order / pagination / time range)
# --------------------------------------------------------------------------

def _parse_filter(kv: str) -> tuple[str, str]:
    if "=" not in kv:
        raise argparse.ArgumentTypeError(f"expected K=V, got {kv!r}")
    k, v = kv.split("=", 1)
    return (k.strip(), v.strip())


def cmd_query(args) -> int:
    base = (os.environ.get("TOWNLOOPS_API_URL") or "").rstrip("/")
    key = os.environ.get("TOWNLOOPS_API_KEY", "").strip()
    slug = os.environ.get("TOWNLOOPS_TOWN_SLUG", "").strip()
    if not (base and key and slug):
        print(json.dumps({"error": "TOWNLOOPS_API_URL / _KEY / _TOWN_SLUG must all be set"}),
              file=sys.stderr)
        return 2

    endpoint = f"{base}/towns/{slug}/{args.resource.strip('/')}"
    params: dict[str, Any] = {}
    for k, v in (args.filter or []):
        params[k] = v
    if args.from_:
        params["from"] = args.from_
    if args.to:
        params["to"] = args.to
    if args.order_by:
        params["order_by"] = args.order_by
    limit = args.limit or 50
    offset = args.offset or 0
    params["limit"] = limit
    params["offset"] = offset

    headers = {"Authorization": f"Bearer {key}", "Accept": "application/json"}

    def one_page(p_offset: int) -> dict:
        p = dict(params, offset=p_offset)
        r = requests.get(endpoint, headers=headers, params=p, timeout=30)
        r.raise_for_status()
        return r.json()

    if not args.all:
        body = one_page(offset)
        print(json.dumps(body, indent=2, ensure_ascii=False))
        return 0

    all_rows: list = []
    p_offset = offset
    total_pages = 0
    while True:
        body = one_page(p_offset)
        page = body.get("data") if isinstance(body, dict) else body
        if not isinstance(page, list):
            print(json.dumps({"error": "response is not a list envelope",
                              "sample_body_type": type(body).__name__}),
                  file=sys.stderr)
            return 5
        all_rows.extend(page)
        total_pages += 1
        if len(page) < limit:
            break
        p_offset += limit
        if total_pages > 200:  # safety brake
            print(json.dumps({"error": "pagination cap (200 pages) hit; refine filters"}),
                  file=sys.stderr)
            return 6
    print(json.dumps({
        "count": len(all_rows), "pages_fetched": total_pages, "data": all_rows,
    }, ensure_ascii=False))
    return 0


# --------------------------------------------------------------------------
# CLI wiring
# --------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="townloops-api")
    sub = p.add_subparsers(dest="cmd", required=True)

    sn = sub.add_parser("snapshot", help="Fetch OpenAPI JSON and write local snapshot")
    sn.add_argument("--url", help="Override TOWNLOOPS_OPENAPI_URL")
    sn.set_defaults(func=cmd_snapshot)

    df = sub.add_parser("diff", help="Structured diff of state/openapi.json vs a prior snapshot")
    df.add_argument("--against", help="Path to a specific previous snapshot (default: state/openapi.previous.json)")
    df.set_defaults(func=cmd_diff)

    ck = sub.add_parser("check",
        help="snapshot + diff + freeze breaking resources + Telegram alert")
    ck.add_argument("--url", help="Override TOWNLOOPS_OPENAPI_URL")
    ck.set_defaults(func=cmd_check)

    ds = sub.add_parser("describe",
        help="Print methods + filter/order/pagination params for a resource")
    ds.add_argument("resource",
        help="e.g. events, venues, businesses, communities, sponsors, ad-slots")
    ds.set_defaults(func=cmd_describe)

    qy = sub.add_parser("query", help="Paginated + filtered GET on a resource")
    qy.add_argument("resource")
    qy.add_argument("--filter", action="append", type=_parse_filter, default=[],
        help="K=V; may repeat. Passed as query params.")
    qy.add_argument("--from", dest="from_",
        help="ISO date/time; passed as `from` query param (time-range filter).")
    qy.add_argument("--to", help="ISO date/time; passed as `to`.")
    qy.add_argument("--order-by", dest="order_by",
        help="Passed as `order_by` query param when the endpoint honors it.")
    qy.add_argument("--limit", type=int, default=None)
    qy.add_argument("--offset", type=int, default=None)
    qy.add_argument("--all", action="store_true",
        help="Follow pagination until short page; capped at 200 pages.")
    qy.set_defaults(func=cmd_query)

    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
