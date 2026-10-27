---
name: egress-hosts
description: Open an outbound host this town needs (an event API, a feed, a calendar CDN) on the Squid egress allowlist, or close one again — with a reason, for this town only. Reachable after the host-side sync, typically within a few minutes.
---

# egress-hosts

My HTTP goes through a shared Squid allowlist; a host not on it answers 403
from the proxy. This skill lets me add a host **my town** needs, myself:
`$HERMES_HOME/egress/allowed_hosts.yaml`, merged with `town-profile.yaml`'s
`allowed_hosts` into my town's acl only (engine/egress.py). The operator's
hosts stay in the profile; mine stay in my file.

## When to use

A fetch I need fails with a proxy 403 / `ERR_ACCESS_DENIED` / "Forbidden"
from `squid` — typically while building or trialling a source adapter whose
API lives on another host than the seed page (e.g. a Modern Campus calendar at
`api.calendar.moderncampus.net`). Not for Firecrawl reads: Firecrawl is local
and doesn't go through Squid.

## CLI

```bash
$PYTHON $HERMES_HOME/skills/business/egress-hosts/hosts.py add api.calendar.moderncampus.net \
  --reason "Modern Campus calendar API behind the college's events page" \
  --seed-id 12 --evidence https://college.example/events
$PYTHON $HERMES_HOME/skills/business/egress-hosts/hosts.py remove api.calendar.moderncampus.net
$PYTHON $HERMES_HOME/skills/business/egress-hosts/hosts.py list
```

`$PYTHON` is `/usr/bin/python3`, the system Python that has the skill's
dependencies; there is no bare `python` on the container's PATH.

- `add HOST --reason TEXT [--seed-id N] [--evidence URL]` — checks HOST with
  the compiler's own rules and writes it with the reason, the time, and the
  seed/evidence. A bare hostname only: no scheme, path, port or `*.`
  wildcard, no IP address, no public suffix (`github.io`, `co.uk`, …). Name
  the narrowest host that works (`api.example.org`, not `example.org`) — an
  entry also allows its subdomains. A host the profile already allows is a
  no-op (`added: false`).
- `remove HOST` — drops a host I added (a profile host is the operator's).
- `list` — my hosts with their reasons, and the profile's.

Every add/remove is an `agent_activity` row (worker `egress-hosts`) and is
listed in the nightly digest under "Egress hosts opened".

## After adding

The host is **not** reachable yet: `bin/townloops-egress-sync` on the host
(the operator's crontab, every few minutes) recompiles the allowlist and
reloads Squid. Retry the fetch after ~5–10 minutes; if it still gets the
proxy 403, the sync isn't running — tell the operator, don't add the host
again. When an adapter or seed that needed a host is retired, `remove` it.
