"""The shared Squid egress allowlist, compiled from every town's `allowed_hosts`.

`render_squid_conf(towns)` is the core: a list of `(slug, allowed_hosts)` in,
the full `squid.conf` text out, no I/O. `towns_from(base_dir)` reads every
town's `town-profile.yaml` under $TOWNLOOPS_AGENTS_ROOT (a folder whose name
isn't a slug — `slt.bak`, `_egress` — is skipped with a note, not fatal), and
`main()` is what `bin/townloops-agent-add --egress` and
`bin/townloops-egress-sync` run: compile, then write `<base_dir>/_egress/squid.conf`
in place.

A town's agent opens hosts for its own town itself (the `egress-hosts` skill):
`$HERMES_HOME/egress/allowed_hosts.yaml` (`AGENT_HOSTS`; on the host
`<base_dir>/<slug>/hermes-home/egress/allowed_hosts.yaml`), each entry a host
with its reason, when, and the seed or evidence. `towns_from` merges it into
that town's acl under the same rules — but a bad agent entry, or an unreadable
file, is skipped with a note naming town, file and entry: the agent writes it
unattended, so one bad line must never fail every town's allowlist. A bad
*profile* entry stays an error (the operator typed it and is there to fix it).

The platform block (the LLM, TownLoops, Telegram, Listmonk) is always emitted,
so regenerating can never cut an agent off from what it ships to. Each town
adds one `acl town_<slug> dstdomain …` and its `http_access allow town_<slug>`,
emitted together. Entries are normalized to Squid's leading-dot dstdomain form
(the host and its subdomains); anything that isn't a bare hostname — a scheme,
a path, a port, a wildcard, an IP literal, or a public suffix that would open a
whole registry (`co.uk`, `github.io`, …) — is an error naming the town and the
entry, so a typo can't silently widen the boundary. Output is sorted: the same profiles
give byte-identical text.

Firecrawl is not here: agents reach the local instance in-network (NO_PROXY),
and the cloud service is never allowed (engine/firecrawl_env.py).
"""

from __future__ import annotations

import os
import re
import sys
from collections.abc import Callable, Iterable
from pathlib import Path

GENERATOR = "bin/townloops-agent-add --egress (engine/egress.py)"

_SLUG = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")
_LABEL = r"[a-z0-9]([a-z0-9-]*[a-z0-9])?"
_HOST = re.compile(rf"^{_LABEL}(\.{_LABEL})+$")


# Where --egress writes, relative to $TOWNLOOPS_AGENTS_ROOT — outside the repo,
# so a generated, host-specific file never dirties the checkout.
OUT_DIR = "_egress"
OUT_NAME = "squid.conf"
# The agent's own hosts, relative to $HERMES_HOME — which is the town folder's
# `hermes-home` on the host (the compose bind mount).
AGENT_HOSTS = Path("egress") / "allowed_hosts.yaml"
HERMES_DIR = "hermes-home"

# Public suffixes a town might plausibly type: allowing one would allow every
# site registered under it. Not the full Public Suffix List — the shared
# hosting platforms and the second-level ccTLD shapes (_CC_SECOND_LEVEL).
PUBLIC_SUFFIXES = frozenset({
    "co.uk", "org.uk", "ac.uk", "gov.uk", "com.au", "net.au", "org.au", "co.nz",
    "github.io", "gitlab.io", "herokuapp.com", "appspot.com", "vercel.app",
    "netlify.app", "pages.dev", "workers.dev", "web.app", "firebaseapp.com",
    "blogspot.com", "wordpress.com", "wixsite.com", "squarespace.com",
    "weebly.com", "azurewebsites.net", "cloudfront.net", "amazonaws.com",
})
_CC_SECOND_LEVEL = {"co", "com", "net", "org", "gov", "govt", "edu", "ac", "mil", "nic", "ltd", "plc", "sch"}


class EgressError(ValueError):
    """A town's allowed_hosts can't be compiled into the allowlist."""


# (acl name, dstdomain values, why) — the hosts every agent needs.
PLATFORM: tuple[tuple[str, tuple[str, ...], str], ...] = (
    ("openai", (".openai.com",), "LLM: OpenAI Codex CLI (config.yaml provider: openai-codex)"),
    ("chatgpt", (".chatgpt.com",), "LLM: ChatGPT login behind the Codex CLI"),
    ("townloops", (".townloops.com",), "the TownLoops API every town ships to"),
    ("telegram", (".telegram.org",), "operator alerts"),
    ("listmonk_lan", ("host.docker.internal",), "self-hosted Listmonk on the host"),
)

_HEAD = """\
http_port 3128
"""

_PORTS = """\
# --- CONNECT (HTTPS) restricted to safe-ish ports ----------------------
acl SSL_ports     port 443
acl Safe_ports    port 80 443 8080
acl CONNECT method CONNECT

http_access deny  !Safe_ports
http_access deny  CONNECT !SSL_ports
"""

_TAIL = """\
http_access deny  all

# --- Access logging ----------------------------------------------------
# Default path is inside the container at /var/log/squid/access.log —
# tail with `docker exec squid-egress tail -f /var/log/squid/access.log`.
# The dashboard's Blocked page reads it to show denied hosts.
access_log daemon:/var/log/squid/access.log squid
cache deny all

# Don't retain X-Forwarded-For (agents behind this proxy should be opaque
# to the destination).
forwarded_for delete
via off
"""


def _normalize(slug: str, entry: object) -> str:
    host = str(entry).strip().lower() if entry is not None else ""
    bare = host[1:] if host.startswith(".") else host
    if not _HOST.match(bare):
        raise EgressError(
            f"town {slug!r}: allowed_hosts entry {entry!r} is not a bare hostname "
            f"(write it like 'example.org' — no scheme, path, port or wildcard)")
    labels = bare.split(".")
    if labels[-1].isdigit():
        raise EgressError(
            f"town {slug!r}: allowed_hosts entry {entry!r} is an IP address — "
            f"name the site's hostname instead")
    if _is_public_suffix(labels):
        raise EgressError(
            f"town {slug!r}: allowed_hosts entry {entry!r} is a public suffix — it would "
            f"allow every site under it; name the site itself (e.g. 'mysite.{bare}')")
    return "." + bare


def check_host(slug: str, entry: object) -> str:
    """`entry` as a bare lowercase hostname, or an `EgressError` naming town and
    entry — the compiler's own rules, for the agent to check before it writes."""
    return _normalize(slug, entry)[1:]


def _is_public_suffix(labels: list[str]) -> bool:
    if len(labels) < 2:  # a bare TLD (the hostname pattern already refuses one)
        return True
    if ".".join(labels) in PUBLIC_SUFFIXES:
        return True
    if len(labels) == 2 and len(labels[1]) == 2:  # a ccTLD's registry level
        # co.uk, com.au, … and the US state level (ca.us, nv.us).
        return labels[0] in _CC_SECOND_LEVEL or (labels[1] == "us" and len(labels[0]) == 2)
    return False


def _collapse(hosts: set[str]) -> list[str]:
    """Sorted, without entries a broader one already covers (.b.com covers .x.b.com)."""
    return sorted(h for h in hosts if not any(h != o and h.endswith(o) for o in hosts))


def _acl(name: str, hosts: list[str]) -> str:
    if len(hosts) == 1:
        return f"acl {name} dstdomain {hosts[0]}\n"
    return f"acl {name} dstdomain \\\n" + " \\\n".join(f"    {h}" for h in hosts) + "\n"


def render_squid_conf(towns: Iterable[tuple[str, Iterable[str]]]) -> str:
    """The full squid.conf for these towns' `allowed_hosts`."""
    compiled: dict[str, list[str]] = {}
    for slug, hosts in towns:
        if not isinstance(slug, str) or not _SLUG.match(slug):
            raise EgressError(f"town slug {slug!r} is not a TownLoops slug")
        if slug in compiled:
            raise EgressError(f"town {slug!r} given twice")
        compiled[slug] = _collapse({_normalize(slug, h) for h in (hosts or [])})

    slugs = sorted(compiled)
    out = [
        "# GENERATED by " + GENERATOR + " — do not edit by hand.\n"
        "# Squid egress allowlist shared by every per-town townloops-agent.\n"
        "# Inputs: allowed_hosts in each town's town-profile.yaml under\n"
        "# $TOWNLOOPS_AGENTS_ROOT and the hosts its agent added in\n"
        "# hermes-home/" + AGENT_HOSTS.as_posix() + ", for: "
        + (", ".join(slugs) or "(no towns)") + ".\n"
        "# To allow a host: add it to that town's allowed_hosts, re-run the\n"
        "# generator, then reload Squid (bin/townloops-egress-sync does both):\n"
        "#   docker exec squid-egress squid -k reconfigure\n",
        "\n",
        _HEAD,
        "\n" + _PORTS,
        "\n# --- Deny by default; only the dstdomains below pass. -----------------\n",
        "# --- platform: every agent needs these -------------------------------\n",
    ]
    for name, hosts, why in PLATFORM:
        out.append(f"# {why}\n" + _allow(name, list(hosts)))

    out.append("\n# --- towns: compiled from allowed_hosts ------------------------------\n")
    for slug in slugs:
        if compiled[slug]:
            out.append(f"# town {slug}\n" + _allow(_town_acl(slug), compiled[slug]))
        else:
            out.append(f"# town {slug}: no allowed_hosts — platform hosts only\n")

    out.append("\n" + _TAIL)
    return "".join(out)


def _allow(name: str, hosts: list[str]) -> str:
    """An acl and its http_access allow — always together."""
    return _acl(name, hosts) + f"http_access allow {name}\n"


def _town_acl(slug: str) -> str:
    # Slugs never hold '_', so swapping '-' for it can't make two towns collide.
    return "town_" + slug.replace("-", "_")


# --------------------------------------------------------------------------
# Reading the towns + writing the file (bin/townloops-agent-add --egress)
# --------------------------------------------------------------------------

def _stderr(msg: str) -> None:
    print(msg, file=sys.stderr)


def towns_from(base_dir: Path | str,
               note: Callable[[str], None] = _stderr) -> list[tuple[str, list]]:
    """`(slug, allowed_hosts)` for every `<base_dir>/<slug>/town-profile.yaml`:
    the profile's hosts, then the valid ones its agent added
    (`<slug>/hermes-home/egress/allowed_hosts.yaml`).

    A folder whose name isn't a slug (a backup like `slt.bak`, this module's own
    `_egress`) is skipped with a `note`, so one stray copy can't fail every
    town. A profile that isn't a YAML mapping, or whose allowed_hosts isn't a
    list, is an `EgressError` naming the file; a bad agent file or entry is
    only a `note` (`agent_hosts`).
    """
    import yaml

    towns = []
    for profile in sorted(Path(base_dir).glob("*/town-profile.yaml")):
        slug = profile.parent.name
        if not _SLUG.match(slug):
            note(f"skipping {profile.parent}: {slug!r} is not a town slug (not a town folder)")
            continue
        try:
            data = yaml.safe_load(profile.read_text())
        except (OSError, yaml.YAMLError) as e:
            raise EgressError(f"{profile}: not readable YAML: {e}") from None
        if data is None:
            data = {}
        if not isinstance(data, dict):
            raise EgressError(f"{profile}: not a YAML mapping (got {type(data).__name__})")
        hosts = data.get("allowed_hosts") or []
        if not isinstance(hosts, list):
            raise EgressError(f"{profile}: allowed_hosts must be a list of hostnames")
        agent_file = profile.parent / HERMES_DIR / AGENT_HOSTS
        towns.append((slug, hosts + agent_hosts(agent_file, slug, note)))
    return towns


# --------------------------------------------------------------------------
# The agent's own hosts ($HERMES_HOME/egress/allowed_hosts.yaml)
# --------------------------------------------------------------------------

_AGENT_HEADER = (
    "# Egress hosts this town's agent opened for itself (the egress-hosts skill).\n"
    "# Merged into the town's acl by engine/egress.py; applied on the host by\n"
    "# bin/townloops-egress-sync. Written by the skill — don't hand-edit.\n")


def agent_hosts_path() -> Path:
    """This town's agent file, as the container sees it ($HERMES_HOME)."""
    return Path(os.environ.get("HERMES_HOME", "/home/agent/.hermes")) / AGENT_HOSTS


def read_agent_hosts(path: Path | str) -> list:
    """The file's entries as written; [] when there is none. An unreadable or
    misshapen file is an `EgressError` naming it."""
    import yaml

    path = Path(path)
    if not path.exists():
        return []
    try:
        data = yaml.safe_load(path.read_text())
    except (OSError, yaml.YAMLError) as e:
        raise EgressError(f"{path}: not readable YAML: {e}") from None
    if data is None:
        return []
    entries = data.get("hosts") if isinstance(data, dict) else None
    if not isinstance(entries, list):
        raise EgressError(f"{path}: expected a mapping with a `hosts:` list")
    return entries


def agent_hosts(path: Path | str, slug: str,
                note: Callable[[str], None] = _stderr) -> list[str]:
    """The valid hosts in a town's agent file; anything else is a `note`."""
    try:
        entries = read_agent_hosts(path)
    except EgressError as e:
        note(f"town {slug!r}: skipping its agent-added hosts — {e}")
        return []
    hosts = []
    for entry in entries:
        try:
            if not isinstance(entry, dict) or "host" not in entry:
                raise EgressError(f"town {slug!r}: entry {entry!r} has no `host:`")
            hosts.append(check_host(slug, entry["host"]))
        except EgressError as e:
            note(f"skipping an agent-added host in {path}: {e}")
    return hosts


def _same(entry: object, bare: str) -> bool:
    return isinstance(entry, dict) and str(entry.get("host", "")).strip().lower().lstrip(".") == bare


def _write_agent_hosts(path: Path, entries: list) -> None:
    """Sorted by host, atomically (temp + rename — this file is read through
    the hermes-home folder mount, never bind-mounted on its own)."""
    import yaml

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(_AGENT_HEADER + yaml.safe_dump(
        {"hosts": sorted(entries, key=lambda e: str(e.get("host")) if isinstance(e, dict) else "")},
        sort_keys=False, allow_unicode=True))
    os.replace(tmp, path)


def add_agent_host(path: Path | str, slug: str, host: str, *, reason: str, now: str,
                   seed_id: int | None = None,
                   evidence: str | None = None) -> tuple[dict, bool]:
    """Add `host` to a town's agent file: `(entry, added)`. Already there →
    its existing entry and False, nothing written."""
    path = Path(path)
    bare = check_host(slug, host)
    if not (reason or "").strip():
        raise EgressError(f"town {slug!r}: say why {bare!r} is needed (a reason)")
    entries = read_agent_hosts(path)
    for e in entries:
        if _same(e, bare):
            return e, False
    entry = {"host": bare, "reason": reason.strip(), "added_at": now}
    if seed_id is not None:
        entry["seed_id"] = seed_id
    if evidence:
        entry["evidence"] = evidence
    _write_agent_hosts(path, [*entries, entry])
    return entry, True


def remove_agent_host(path: Path | str, slug: str, host: str) -> dict | None:
    """Drop `host` from a town's agent file: its entry, or None if it wasn't there."""
    path = Path(path)
    bare = str(host).strip().lower().lstrip(".")
    entries = read_agent_hosts(path)
    gone = next((e for e in entries if _same(e, bare)), None)
    if gone is not None:
        _write_agent_hosts(path, [e for e in entries if not _same(e, bare)])
    return gone


# The skill records each add/remove as an agent_activity row under this worker;
# the nightly digest lists them (`recent_changes`).
AGENT_WORKER = "egress-hosts"


def recent_changes(conn, since: str) -> list[dict]:
    """The agent's egress adds/removes since `since`, oldest first:
    `{ts, action, host, reason, seed_id}`."""
    import json

    out = []
    for ts, action, host, detail in conn.execute(
            "SELECT ts, action, target, detail FROM agent_activity "
            "WHERE worker = ? AND ts >= ? ORDER BY id", (AGENT_WORKER, since)).fetchall():
        try:
            d = json.loads(detail or "{}")
        except ValueError:
            d = {}
        d = d if isinstance(d, dict) else {}
        out.append({"ts": ts, "action": action, "host": host,
                    "reason": d.get("reason"), "seed_id": d.get("seed_id")})
    return out


def compile_from(base_dir: Path | str, note: Callable[[str], None] = _stderr) -> str:
    """The squid.conf for every town under `base_dir`."""
    return render_squid_conf(towns_from(base_dir, note))


def main(argv: list[str] | None = None) -> int:
    """`python -m engine.egress BASE_DIR` — write BASE_DIR/_egress/squid.conf."""
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        _stderr("usage: python -m engine.egress BASE_DIR")
        return 2
    base = Path(args[0])
    out = base / OUT_DIR / OUT_NAME
    try:
        towns = towns_from(base)
        text = render_squid_conf(towns)
    except EgressError as e:
        _stderr(f"{e} — {out} unchanged")
        return 1
    names = ", ".join(slug for slug, _ in towns) or "none"
    if out.exists() and out.read_text() == text:
        print(f"{out} unchanged (towns: {names})")
        return 0
    out.parent.mkdir(parents=True, exist_ok=True)
    # In place: the shared Squid bind-mounts this one file, and a new inode
    # (write-and-rename) would leave the running container on the old copy.
    with open(out, "r+" if out.exists() else "w") as f:
        f.write(text)
        f.truncate()
    print(f"wrote {out} (towns: {names})")
    print("Reload the shared Squid (every town's egress goes through it):")
    print("  docker exec squid-egress squid -k reconfigure")
    return 0


if __name__ == "__main__":
    sys.exit(main())
