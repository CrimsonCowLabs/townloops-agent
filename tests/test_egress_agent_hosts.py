"""Agent-added egress hosts: a town's agent opens a host for its own town in
$HERMES_HOME/egress/allowed_hosts.yaml (the `egress-hosts` skill), engine/egress
merges it with the profile's allowed_hosts, and bin/townloops-egress-sync
recompiles + reloads Squid on the host."""

from __future__ import annotations

import contextlib
import io
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from engine import egress
from engine.egress import EgressError, compile_from, main, towns_from
from tests.autonomy_town import SKILLS, load

ROOT = Path(__file__).resolve().parent.parent
NOW = "2026-10-27T12:00:00+00:00"


def _town(base: Path, slug: str, profile: str = "name: T\n") -> Path:
    (base / slug).mkdir(parents=True, exist_ok=True)
    (base / slug / "town-profile.yaml").write_text(profile)
    return base / slug


def _agent_file(base: Path, slug: str) -> Path:
    return base / slug / egress.HERMES_DIR / egress.AGENT_HOSTS


def _write_agent(base: Path, slug: str, text: str) -> Path:
    path = _agent_file(base, slug)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


def _acl(conf: str, name: str) -> list[str]:
    for line in conf.replace("\\\n", " ").splitlines():
        words = line.split()
        if words[:3] == ["acl", name, "dstdomain"]:
            return words[3:]
    raise AssertionError(f"no acl {name}")


# --------------------------------------------------------------------------
# Compiling: the agent file merges into its own town's acl
# --------------------------------------------------------------------------

def test_agent_hosts_merge_into_their_own_towns_acl(tmp_path):
    _town(tmp_path, "slt", "allowed_hosts: [cityofslt.us]\n")
    _town(tmp_path, "north-shore", "allowed_hosts: [sierrasun.com]\n")
    _write_agent(tmp_path, "slt", "hosts:\n- host: api.calendar.moderncampus.net\n"
                                  "  reason: Modern Campus calendar API\n")
    conf = compile_from(tmp_path)
    assert _acl(conf, "town_slt") == [".api.calendar.moderncampus.net", ".cityofslt.us"]
    assert _acl(conf, "town_north_shore") == [".sierrasun.com"]


def test_a_town_with_only_agent_hosts_gets_an_acl(tmp_path):
    _town(tmp_path, "slt")
    _write_agent(tmp_path, "slt", "hosts:\n- host: feeds.example.org\n  reason: r\n")
    assert towns_from(tmp_path) == [("slt", ["feeds.example.org"])]


def test_a_host_in_both_collapses_to_one(tmp_path):
    _town(tmp_path, "slt", "allowed_hosts: [example.org]\n")
    _write_agent(tmp_path, "slt", "hosts:\n- host: api.example.org\n  reason: r\n"
                                  "- host: example.org\n  reason: r\n")
    assert _acl(compile_from(tmp_path), "town_slt") == [".example.org"]


@pytest.mark.parametrize("entry", [
    "https://api.example.org", "api.example.org/v1", "api.example.org:8443",
    "*.example.org", "10.0.0.1", "co.uk", "github.io", "", "localhost"])
def test_a_bad_agent_entry_is_skipped_with_a_note_not_an_error(tmp_path, entry):
    _town(tmp_path, "slt", "allowed_hosts: [cityofslt.us]\n")
    _town(tmp_path, "north-shore", "allowed_hosts: [sierrasun.com]\n")
    path = _write_agent(tmp_path, "slt", yaml.safe_dump(
        {"hosts": [{"host": entry, "reason": "r"}, {"host": "good.example.org", "reason": "r"}]}))
    notes: list[str] = []
    conf = egress.render_squid_conf(towns_from(tmp_path, notes.append))
    assert _acl(conf, "town_slt") == [".cityofslt.us", ".good.example.org"]
    assert _acl(conf, "town_north_shore") == [".sierrasun.com"]
    assert len(notes) == 1
    assert "slt" in notes[0] and str(path) in notes[0] and repr(entry) in notes[0]


@pytest.mark.parametrize("text", [
    "hosts: [unclosed\n", "- a list\n", "hosts: example.org\n", "42\n",
    "hosts:\n- just-a-string.org\n", "hosts:\n- reason: no host\n"])
def test_a_malformed_agent_file_or_entry_never_breaks_compilation(tmp_path, text):
    _town(tmp_path, "slt", "allowed_hosts: [cityofslt.us]\n")
    _write_agent(tmp_path, "slt", text)
    notes: list[str] = []
    conf = egress.render_squid_conf(towns_from(tmp_path, notes.append))
    assert _acl(conf, "town_slt") == [".cityofslt.us"]
    assert notes and "slt" in notes[0] and egress.AGENT_HOSTS.name in notes[0]


def test_an_empty_agent_file_is_no_hosts_and_no_note(tmp_path):
    _town(tmp_path, "slt")
    _write_agent(tmp_path, "slt", "")
    notes: list[str] = []
    assert towns_from(tmp_path, notes.append) == [("slt", [])]
    assert notes == []


def test_a_bad_profile_entry_is_still_an_error(tmp_path):
    _town(tmp_path, "slt", "allowed_hosts: ['https://cityofslt.us']\n")
    _write_agent(tmp_path, "slt", "hosts:\n- host: fine.org\n  reason: r\n")
    with pytest.raises(EgressError):
        compile_from(tmp_path)


def test_an_agent_file_without_a_town_profile_is_ignored(tmp_path):
    _write_agent(tmp_path, "ghost", "hosts:\n- host: ghost.org\n  reason: r\n")
    _town(tmp_path, "slt.bak")
    _write_agent(tmp_path, "slt.bak", "hosts:\n- host: bak.org\n  reason: r\n")
    conf = compile_from(tmp_path, lambda _: None)
    assert "ghost.org" not in conf and "bak.org" not in conf


def test_rerun_is_byte_identical_and_main_reports_unchanged(tmp_path, capsys):
    _town(tmp_path, "slt", "allowed_hosts: [cityofslt.us]\n")
    _write_agent(tmp_path, "slt", "hosts:\n- host: b.example.org\n  reason: r\n"
                                  "- host: a.example.org\n  reason: r\n")
    assert main([str(tmp_path)]) == 0
    out = tmp_path / "_egress" / "squid.conf"
    first, inode = out.read_text(), out.stat().st_ino
    assert main([str(tmp_path)]) == 0
    assert "unchanged" in capsys.readouterr().out
    assert out.read_text() == first == compile_from(tmp_path)
    assert out.stat().st_ino == inode


def test_the_header_names_the_agent_file_as_an_input():
    header = egress.render_squid_conf([]).split("\n\n", 1)[0]
    assert "egress/allowed_hosts.yaml" in header


# --------------------------------------------------------------------------
# The agent-side file: add / remove / read
# --------------------------------------------------------------------------

def test_add_writes_a_documented_entry_and_is_idempotent(tmp_path):
    path = tmp_path / "egress" / "allowed_hosts.yaml"
    entry, added = egress.add_agent_host(path, "slt", " API.Calendar.ModernCampus.net ",
                                         reason="Modern Campus calendar API", seed_id=12,
                                         evidence="https://x.example/events", now=NOW)
    assert added
    assert entry == {"host": "api.calendar.moderncampus.net",
                     "reason": "Modern Campus calendar API", "added_at": NOW,
                     "seed_id": 12, "evidence": "https://x.example/events"}
    assert egress.read_agent_hosts(path) == [entry]
    again, added = egress.add_agent_host(path, "slt", "api.calendar.moderncampus.net",
                                         reason="other", now="later")
    assert not added and again == entry
    assert egress.read_agent_hosts(path) == [entry]
    assert [p.name for p in path.parent.iterdir()] == ["allowed_hosts.yaml"]  # no temp left


def test_entries_are_kept_sorted_by_host(tmp_path):
    path = tmp_path / "allowed_hosts.yaml"
    for h in ("c.example.org", "a.example.org", "b.example.org"):
        egress.add_agent_host(path, "slt", h, reason="r", now=NOW)
    assert [e["host"] for e in egress.read_agent_hosts(path)] == [
        "a.example.org", "b.example.org", "c.example.org"]


@pytest.mark.parametrize("entry", ["https://x.org", "*.x.org", "1.2.3.4", "co.uk", "x.org:81"])
def test_add_refuses_what_the_compiler_refuses(tmp_path, entry):
    path = tmp_path / "allowed_hosts.yaml"
    with pytest.raises(EgressError) as e:
        egress.add_agent_host(path, "slt", entry, reason="r", now=NOW)
    assert repr(entry) in str(e.value)
    assert not path.exists()


def test_add_needs_a_reason(tmp_path):
    with pytest.raises(EgressError):
        egress.add_agent_host(tmp_path / "f.yaml", "slt", "x.org", reason="  ", now=NOW)


def test_remove_drops_only_that_host(tmp_path):
    path = tmp_path / "allowed_hosts.yaml"
    egress.add_agent_host(path, "slt", "a.example.org", reason="r", now=NOW)
    egress.add_agent_host(path, "slt", "b.example.org", reason="r", now=NOW)
    gone = egress.remove_agent_host(path, "slt", "A.example.org")
    assert gone["host"] == "a.example.org"
    assert [e["host"] for e in egress.read_agent_hosts(path)] == ["b.example.org"]
    assert egress.remove_agent_host(path, "slt", "a.example.org") is None


def test_the_written_file_compiles(tmp_path):
    _town(tmp_path, "slt")
    egress.add_agent_host(_agent_file(tmp_path, "slt"), "slt", "feeds.example.org",
                          reason="r", now=NOW)
    assert _acl(compile_from(tmp_path), "town_slt") == [".feeds.example.org"]


# --------------------------------------------------------------------------
# The egress-hosts skill
# --------------------------------------------------------------------------

@pytest.fixture
def hosts_cli(fresh_db, tmp_state_dir, monkeypatch):
    monkeypatch.setenv("TOWNLOOPS_TOWN_SLUG", "slt")
    profile = tmp_state_dir.parent / "town-profile.yaml"
    profile.write_text("name: SLT\nallowed_hosts: [cityofslt.us]\n")
    monkeypatch.setenv("TOWN_PROFILE_PATH", str(profile))
    mod = load(SKILLS / "egress-hosts" / "hosts.py", "egress_hosts_cli")

    def run(*argv: str) -> tuple[int, dict]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = mod.main(list(argv))
        return rc, json.loads(out.getvalue())
    run.conn = fresh_db
    run.file = tmp_state_dir.parent / "egress" / "allowed_hosts.yaml"
    return run


def _activity(conn) -> list[tuple]:
    return conn.execute("SELECT worker, action, target, detail FROM agent_activity "
                        "WHERE worker = 'egress-hosts' ORDER BY id").fetchall()


def test_cli_add_writes_the_file_records_activity_and_says_when_it_applies(hosts_cli):
    rc, out = hosts_cli("add", "api.calendar.moderncampus.net", "--reason",
                        "Modern Campus calendar API for the college seed", "--seed-id", "12")
    assert rc == 0 and out["added"] is True
    assert out["entry"]["host"] == "api.calendar.moderncampus.net"
    assert out["entry"]["seed_id"] == 12
    assert "sync" in out["note"] and "minutes" in out["note"]
    assert egress.read_agent_hosts(hosts_cli.file)[0]["host"] == "api.calendar.moderncampus.net"
    [(worker, action, target, detail)] = _activity(hosts_cli.conn)
    assert (action, target) == ("add", "api.calendar.moderncampus.net")
    assert "Modern Campus calendar API" in detail


def test_cli_add_refuses_a_bad_host_and_writes_nothing(hosts_cli):
    rc, out = hosts_cli("add", "*.example.org", "--reason", "r")
    assert rc == 1 and "error" in out
    assert not hosts_cli.file.exists()
    assert _activity(hosts_cli.conn) == []


def test_cli_add_of_a_profile_host_is_a_no_op(hosts_cli):
    rc, out = hosts_cli("add", "events.cityofslt.us", "--reason", "r")
    assert rc == 0 and out["added"] is False and "town-profile.yaml" in out["note"]
    assert not hosts_cli.file.exists()


def test_cli_remove_and_list(hosts_cli):
    hosts_cli("add", "a.example.org", "--reason", "r1")
    hosts_cli("add", "b.example.org", "--reason", "r2", "--evidence", "https://b.example.org/x")
    rc, out = hosts_cli("remove", "a.example.org")
    assert rc == 0 and out["removed"]["host"] == "a.example.org"
    rc, out = hosts_cli("list")
    assert rc == 0
    assert [e["host"] for e in out["agent_hosts"]] == ["b.example.org"]
    assert out["profile_hosts"] == ["cityofslt.us"]
    rc, out = hosts_cli("remove", "nope.example.org")
    assert rc == 1
    assert [a[1] for a in _activity(hosts_cli.conn)] == ["add", "add", "remove"]


def test_cli_remove_cannot_touch_a_profile_host(hosts_cli):
    rc, out = hosts_cli("remove", "cityofslt.us")
    assert rc == 1 and "town-profile.yaml" in out["error"]


def test_the_skill_documents_its_cli():
    text = (SKILLS / "egress-hosts" / "SKILL.md").read_text()
    assert text.startswith("---\nname: egress-hosts\n")
    for verb in ("add", "remove", "list"):
        assert f"hosts.py {verb}" in text


# --------------------------------------------------------------------------
# bin/townloops-egress-sync (docker faked on PATH)
# --------------------------------------------------------------------------

SYNC = ROOT / "bin" / "townloops-egress-sync"


@pytest.fixture
def sync(tmp_path):
    base = tmp_path / "agents"
    _town(base, "slt", "allowed_hosts: [cityofslt.us]\n")
    fake = tmp_path / "fakebin"
    fake.mkdir()
    calls = tmp_path / "docker-calls"
    (fake / "docker").write_text(
        "#!/bin/sh\n"
        f'echo "$*" >> "{calls}"\n'
        'case "$*" in *"-k parse"*) exit "${FAKE_PARSE_RC:-0}";; esac\n'
        "exit 0\n")
    (fake / "docker").chmod(0o755)

    def run(parse_rc: int = 0) -> subprocess.CompletedProcess:
        env = {**os.environ, "PATH": f"{fake}:{os.environ['PATH']}",
               "TOWNLOOPS_AGENTS_ROOT": str(base), "FAKE_PARSE_RC": str(parse_rc),
               "PYTHON": sys.executable}
        return subprocess.run([str(SYNC)], env=env, capture_output=True, text=True)

    def docker() -> list[str]:
        return calls.read_text().splitlines() if calls.exists() else []
    run.base, run.docker, run.conf = base, docker, base / "_egress" / "squid.conf"
    return run


def test_sync_applies_a_change_then_is_quiet(sync):
    r = sync()
    assert r.returncode == 0, r.stderr
    assert sync.docker() == ["exec squid-egress squid -k parse",
                             "exec squid-egress squid -k reconfigure"]
    assert "applied" in r.stdout and r.stdout.count("\n") == 1
    r = sync()
    assert r.returncode == 0 and r.stdout == "" and r.stderr == ""
    assert len(sync.docker()) == 2


def test_sync_picks_up_an_agent_added_host_in_place(sync):
    sync()
    inode = sync.conf.stat().st_ino
    egress.add_agent_host(_agent_file(sync.base, "slt"), "slt", "feeds.example.org",
                          reason="r", now=NOW)
    r = sync()
    assert r.returncode == 0 and "applied" in r.stdout
    assert ".feeds.example.org" in sync.conf.read_text()
    assert sync.conf.stat().st_ino == inode
    assert len(sync.docker()) == 4


def test_sync_restores_the_old_file_when_squid_will_not_parse_it(sync):
    sync()
    before = sync.conf.read_text()
    egress.add_agent_host(_agent_file(sync.base, "slt"), "slt", "feeds.example.org",
                          reason="r", now=NOW)
    r = sync(parse_rc=1)
    assert r.returncode != 0 and "parse" in r.stderr
    assert sync.conf.read_text() == before
    assert sync.docker()[-1] == "exec squid-egress squid -k parse"  # no reconfigure
    r = sync()  # the next run tries again
    assert r.returncode == 0 and "applied" in r.stdout
    assert ".feeds.example.org" in sync.conf.read_text()


def test_sync_reloads_a_file_compiled_by_hand_but_never_applied(sync):
    sync()
    egress.add_agent_host(_agent_file(sync.base, "slt"), "slt", "feeds.example.org",
                          reason="r", now=NOW)
    assert egress.main([str(sync.base)]) == 0  # townloops-agent-add --egress, no reload
    r = sync()
    assert r.returncode == 0 and "applied" in r.stdout
    assert sync.docker()[-1] == "exec squid-egress squid -k reconfigure"


def test_sync_reports_a_compile_error_and_touches_nothing(sync):
    sync()
    before = sync.conf.read_text()
    (sync.base / "slt" / "town-profile.yaml").write_text("allowed_hosts: ['https://x.org']\n")
    r = sync()
    assert r.returncode != 0 and "slt" in r.stderr
    assert sync.conf.read_text() == before
    assert len(sync.docker()) == 2


# --------------------------------------------------------------------------
# What the operator sees: the nightly digest and preflight
# --------------------------------------------------------------------------

@pytest.mark.parametrize("mode", ["supervised", "autonomous"])
def test_hosts_the_agent_opened_or_closed_are_in_the_nightly_digest(
        fresh_db, tmp_state_dir, monkeypatch, mode):
    from tests.autonomy_town import PROFILE, Town
    town = Town(tmp_state_dir, monkeypatch, fresh_db, profile={**PROFILE, "operating_mode": mode})
    mod = load(SKILLS / "egress-hosts" / "hosts.py", "egress_hosts_digest")
    with contextlib.redirect_stdout(io.StringIO()):
        assert mod.main(["add", "api.calendar.moderncampus.net", "--reason",
                         "college calendar API", "--seed-id", "12"]) == 0
        assert mod.main(["add", "old.example.org", "--reason", "r"]) == 0
        assert mod.main(["remove", "old.example.org"]) == 0
    out, md, tg = town.digest()
    assert "## Egress hosts opened" in md
    assert "- added api.calendar.moderncampus.net — college calendar API (seed #12)" in md
    assert "- removed old.example.org" in md
    assert "egress hosts: +2 added, 1 removed" in tg
    assert [(c["action"], c["host"]) for c in out["egress_changes"]] == [
        ("add", "api.calendar.moderncampus.net"), ("add", "old.example.org"),
        ("remove", "old.example.org")]


def test_no_egress_section_when_the_agent_changed_nothing(fresh_db, tmp_state_dir, monkeypatch):
    from tests.autonomy_town import Town
    out, md, tg = Town(tmp_state_dir, monkeypatch, fresh_db).digest()
    assert "Egress hosts" not in md and "egress hosts" not in tg
    assert out["egress_changes"] == []


def test_preflight_lists_agent_added_hosts_as_info_and_bad_ones_as_a_warning(
        tmp_state_dir, monkeypatch):
    from engine import preflight
    monkeypatch.setenv("TOWNLOOPS_TOWN_SLUG", "slt")
    path = tmp_state_dir.parent / "egress" / "allowed_hosts.yaml"
    findings: list = []
    preflight._agent_egress(findings)
    assert findings == []
    egress.add_agent_host(path, "slt", "feeds.example.org", reason="r", now=NOW)
    preflight._agent_egress(findings)
    [f] = findings
    assert (f.severity, f.code) == ("info", "egress-agent-hosts")
    assert "feeds.example.org" in f.message and "allowed_hosts.yaml" in f.where
    path.write_text(path.read_text() + "- host: '*.bad.org'\n  reason: r\n")
    findings.clear()
    preflight._agent_egress(findings)
    assert {f.code: f.severity for f in findings} == {
        "egress-agent-hosts": "info", "egress-agent-host-invalid": "warning"}
