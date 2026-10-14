"""bin/townloops-agent-add --rerender: re-rendering an existing town's compose
from the template keeps the stack bootable, or refuses without touching it."""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def env(tmp_path):
    """The tool run out of a tmp copy of the repo by a user whose HOME holds a
    stub Codex login, so the real template renders against the tmp fixture and
    the tests never depend on this host's paths."""
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex" / "auth.json").write_text("{}")
    repo = tmp_path / "repo"
    for d in ("bin", "templates", "cron", "engine"):
        shutil.copytree(ROOT / d, repo / d)
    base = {k: v for k, v in os.environ.items() if k != "CODEX_HOME"}
    return {**base, "HOME": str(home), "TOWNLOOPS_AGENTS_ROOT": str(tmp_path / "towns"),
            "TOWNLOOPS_CONTAINER_UID": str(os.getuid()), "ADD": str(repo / "bin" / "townloops-agent-add")}


def run(env, *args):
    return subprocess.run([env["ADD"], *args], env=env, capture_output=True, text=True)


@pytest.fixture
def town(env, tmp_path):
    """An existing town that has logged in (hermes-home/auth.json present)."""
    assert run(env, "testtown").returncode == 0
    target = tmp_path / "towns" / "testtown"
    (target / "hermes-home" / "auth.json").write_text("{}")
    return target


def test_up_to_date_town_is_left_alone(env, town):
    r = run(env, "testtown", "--rerender")
    assert r.returncode == 0, r.stderr
    assert "up to date" in r.stdout
    assert not list(town.glob("docker-compose.yml.bak-*"))


def test_rerender_backs_up_old_compose_and_shows_the_diff(env, town):
    compose = town / "docker-compose.yml"
    compose.write_text(compose.read_text().replace("127.0.0.1:8090:8090", "0.0.0.0:8090:8090"))
    r = run(env, "testtown", "--rerender")
    assert r.returncode == 0, r.stderr
    assert "127.0.0.1:8090:8090" in compose.read_text()
    [backup] = town.glob("docker-compose.yml.bak-*")
    assert "0.0.0.0:8090:8090" in backup.read_text()
    assert "-      - \"0.0.0.0:8090:8090\"" in r.stdout


def test_old_town_without_hermes_home_gets_it_but_must_log_in_first(env, town):
    shutil.rmtree(town / "hermes-home")
    before = (town / "docker-compose.yml").read_text().replace("hermes-home", "old")
    (town / "docker-compose.yml").write_text(before)

    r = run(env, "testtown", "--rerender")

    assert r.returncode != 0
    assert "auth add openai-codex" in r.stderr
    assert (town / "docker-compose.yml").read_text() == before
    home = town / "hermes-home"
    assert home.is_dir() and stat.S_IMODE(home.stat().st_mode) == 0o700


def test_hermes_home_not_owned_by_container_user_is_refused(env, town):
    before = (town / "docker-compose.yml").read_text().replace("hermes-home", "old")
    (town / "docker-compose.yml").write_text(before)
    r = run({**env, "TOWNLOOPS_CONTAINER_UID": "4242"}, "testtown", "--rerender")
    assert r.returncode != 0
    assert "chown" in r.stderr and "4242" in r.stderr
    assert (town / "docker-compose.yml").read_text() == before


def test_missing_codex_cli_login_is_refused(env, town):
    (Path(env["HOME"]) / ".codex" / "auth.json").unlink()
    r = run(env, "testtown", "--rerender")
    assert r.returncode != 0 and "codex login" in r.stderr


def test_rerender_of_unknown_town_is_refused(env):
    r = run(env, "nosuchtown", "--rerender")
    assert r.returncode != 0 and "no town" in r.stderr


def test_template_gives_the_dashboard_the_codex_login(env, town):
    """Dashboard-spawned skills (event-find, classify) call Codex too."""
    compose = (town / "docker-compose.yml").read_text()
    dashboard = compose[compose.index("\n  dashboard:"):]
    assert f"{env['HOME']}/.codex:/home/agent/.codex:rw" in dashboard


# ---- autonomous towns: the dashboard service is kept from starting ---------

OVERRIDE_LINE = 'services: {dashboard: {profiles: ["supervised-dashboard"]}}'


def test_supervised_town_gets_no_override(env, tmp_path):
    assert run(env, "testtown").returncode == 0
    target = tmp_path / "towns" / "testtown"
    assert not (target / "docker-compose.override.yml").exists()
    assert "\noperating_mode: autonomous" not in (target / "town-profile.yaml").read_text()


def test_autonomous_town_is_scaffolded_without_the_dashboard(env, tmp_path):
    r = run(env, "testtown", "--autonomous")
    assert r.returncode == 0, r.stderr
    target = tmp_path / "towns" / "testtown"
    assert OVERRIDE_LINE in (target / "docker-compose.override.yml").read_text()
    import yaml
    profile = yaml.safe_load((target / "town-profile.yaml").read_text())
    assert profile["operating_mode"] == "autonomous"
    assert "dashboard" in r.stdout and "autonomous" in r.stdout


def test_rerender_of_autonomous_town_writes_a_missing_override(env, town):
    profile = town / "town-profile.yaml"
    profile.write_text(profile.read_text() + "\noperating_mode: autonomous\n")
    r = run(env, "testtown", "--rerender")
    assert r.returncode == 0, r.stderr
    assert OVERRIDE_LINE in (town / "docker-compose.override.yml").read_text()


def test_rerender_keeps_an_override_that_already_disables_the_dashboard(env, town):
    profile = town / "town-profile.yaml"
    profile.write_text(profile.read_text() + "\noperating_mode: autonomous\n")
    mine = ("services:\n  agent:\n    environment:\n      FOO: bar\n"
            "  dashboard:\n    profiles: [\"supervised-dashboard\"]\n")
    (town / "docker-compose.override.yml").write_text(mine)
    r = run(env, "testtown", "--rerender")
    assert r.returncode == 0, r.stderr
    assert (town / "docker-compose.override.yml").read_text() == mine


def test_rerender_refuses_to_clobber_an_operator_override(env, town):
    profile = town / "town-profile.yaml"
    profile.write_text(profile.read_text() + "\noperating_mode: autonomous\n")
    mine = "services:\n  agent:\n    environment:\n      FOO: bar\n"
    (town / "docker-compose.override.yml").write_text(mine)
    compose = town / "docker-compose.yml"
    before = compose.read_text().replace("127.0.0.1:8090:8090", "0.0.0.0:8090:8090")
    compose.write_text(before)

    r = run(env, "testtown", "--rerender")

    assert r.returncode != 0
    assert "supervised-dashboard" in r.stderr
    assert (town / "docker-compose.override.yml").read_text() == mine
    assert compose.read_text() == before


def test_autonomous_flag_on_rerender_is_refused(env, town):
    """--rerender reads the mode from the town's own profile; the flag would
    silently disagree with it."""
    r = run(env, "testtown", "--rerender", "--autonomous")
    assert r.returncode != 0 and "town-profile.yaml" in r.stderr


# ---- what a scaffolded town gets is the repo's own, and portable ------------

SKILLS = {p.name for p in (ROOT / "skills" / "business").iterdir() if (p / "SKILL.md").exists()}
SCRIPTS = {p.name for p in (ROOT / "scripts").iterdir() if p.is_file()}
SCRIPTS_IN_CONTAINER = "/home/agent/.hermes/scripts/"
SKILLS_IN_CONTAINER = "/home/agent/.hermes/skills/business/"


def _scheduled(jobs: list[dict]) -> tuple[set[str], set[str], set[str]]:
    """(skills, scripts, skill files) a cron schedule runs."""
    skills, scripts, files = set(), set(), set()
    for job in jobs:
        skills.update(job.get("skills") or [])
        if job.get("script"):
            scripts.add(job["script"])
        for word in (job.get("prompt") or "").split():
            if word.startswith(SCRIPTS_IN_CONTAINER):
                scripts.add(word[len(SCRIPTS_IN_CONTAINER):])
            elif word.startswith(SKILLS_IN_CONTAINER):
                files.add(word[len(SKILLS_IN_CONTAINER):])
    return skills, scripts, files


def test_scaffolded_town_schedules_only_what_the_repo_has(env, tmp_path):
    assert run(env, "testtown").returncode == 0
    jobs = json.loads((tmp_path / "towns" / "testtown" / "cron" / "jobs.json").read_text())["jobs"]
    skills, scripts, files = _scheduled(jobs)
    assert skills and scripts
    assert skills <= SKILLS, f"scheduled skills not in skills/business/: {sorted(skills - SKILLS)}"
    assert scripts <= SCRIPTS, f"scheduled scripts not in scripts/: {sorted(scripts - SCRIPTS)}"
    missing = sorted(f for f in files if not (ROOT / "skills" / "business" / f).is_file())
    assert not missing, f"scheduled skill files not in skills/business/: {missing}"


def test_scaffolded_town_installs_the_repos_schedule(env, tmp_path):
    assert run(env, "testtown").returncode == 0
    cron = tmp_path / "towns" / "testtown" / "cron"
    assert (cron / "jobs.json").read_text() == (ROOT / "cron" / "jobs.json").read_text()
    assert json.loads((cron / "jobs.empty.json").read_text()) == {"version": 1, "jobs": []}


def _host_paths(compose: str) -> list[str]:
    """Every absolute host path the compose bind-mounts or builds from."""
    paths = []
    for line in compose.splitlines():
        line = line.split("#", 1)[0].strip()
        host, _, inside = line[2:].partition(":")
        if line.startswith("- /") and inside.startswith("/"):  # a bind mount, not a tmpfs
            paths.append(host)
        elif line.startswith("context:"):
            paths.append(line.split(":", 1)[1].strip())
    return paths


def test_rendered_compose_mounts_only_the_running_repo_and_users_codex(env, town, tmp_path):
    compose = (town / "docker-compose.yml").read_text()
    paths = _host_paths(compose)
    assert paths
    outside = [p for p in paths if not p.startswith(str(tmp_path) + "/")]
    assert not outside, outside
    assert "{{" not in compose
    repo = tmp_path / "repo"
    assert f"context: {repo}\n" in compose
    assert f"- {repo}/engine:/app/engine:ro" in compose
    assert f"- {env['HOME']}/.codex:/home/agent/.codex:rw" in compose


def test_no_template_names_a_host_path():
    for tmpl in (ROOT / "templates").rglob("*"):
        if tmpl.is_file():
            assert "/home/aaron" not in tmpl.read_text(), tmpl


def test_rerender_picks_up_host_paths(env, town, tmp_path):
    """A town rendered when the template hardcoded the author's paths is
    recovered by --rerender."""
    compose = town / "docker-compose.yml"
    compose.write_text(compose.read_text().replace(str(tmp_path / "repo"), "/home/aaron/workspace/townloops-agent"))
    r = run(env, "testtown", "--rerender")
    assert r.returncode == 0, r.stderr
    assert "/home/aaron" not in compose.read_text()


# ---- --egress: the shared Squid allowlist compiled from every town ---------

def _towns_with_hosts(env, tmp_path, towns: dict[str, list[str]]) -> Path:
    for slug, hosts in towns.items():
        assert run(env, slug).returncode == 0
        profile = tmp_path / "towns" / slug / "town-profile.yaml"
        data = yaml.safe_load(profile.read_text())
        data["allowed_hosts"] = hosts
        profile.write_text(yaml.safe_dump(data))
    return tmp_path / "towns" / "_egress" / "squid.conf"


def test_egress_compiles_every_towns_allowed_hosts(env, tmp_path):
    conf = _towns_with_hosts(env, tmp_path, {"slt": ["cityofslt.us"], "north-shore": ["sierrasun.com"]})
    r = run(env, "--egress")
    assert r.returncode == 0, r.stderr
    text = conf.read_text()
    assert "acl town_slt dstdomain .cityofslt.us" in text
    assert "acl town_north_shore dstdomain .sierrasun.com" in text
    assert "http_access allow townloops" in text
    assert "squid -k reconfigure" in r.stdout  # the reload is the operator's step


def test_egress_rerun_is_a_byte_identical_noop_written_in_place(env, tmp_path):
    conf = _towns_with_hosts(env, tmp_path, {"slt": ["cityofslt.us"]})
    assert run(env, "--egress").returncode == 0
    before, inode = conf.read_bytes(), conf.stat().st_ino
    r = run(env, "--egress")
    assert r.returncode == 0, r.stderr
    assert conf.read_bytes() == before
    assert "unchanged" in r.stdout
    _towns_with_hosts(env, tmp_path, {"other": ["two.com"]})
    assert run(env, "--egress").returncode == 0
    assert conf.stat().st_ino == inode  # Squid's single-file bind mount keeps seeing it
    assert "town_other" in conf.read_text()


def test_egress_refuses_a_bad_entry_and_leaves_the_file(env, tmp_path):
    conf = _towns_with_hosts(env, tmp_path, {"slt": ["cityofslt.us"]})
    assert run(env, "--egress").returncode == 0
    before = conf.read_text()
    _towns_with_hosts(env, tmp_path, {"other": ["https://cityofslt.us/events"]})
    r = run(env, "--egress")
    assert r.returncode != 0
    assert "other" in r.stderr and "https://cityofslt.us/events" in r.stderr
    assert "Traceback" not in r.stderr
    assert conf.read_text() == before


def test_egress_writes_outside_the_repo_and_leaves_the_tracked_conf_alone(env, tmp_path):
    tracked = tmp_path / "repo" / "templates" / "_shared" / "egress" / "squid.conf"
    before = tracked.read_bytes()
    conf = _towns_with_hosts(env, tmp_path, {"slt": ["cityofslt.us"]})
    r = run(env, "--egress")
    assert r.returncode == 0, r.stderr
    assert "town_slt" in conf.read_text()
    assert tracked.read_bytes() == before


def test_egress_target_dir_moves_the_output_with_the_towns(env, tmp_path):
    _towns_with_hosts(env, tmp_path, {"slt": ["cityofslt.us"]})
    r = run(env, "--egress", "--target-dir", str(tmp_path / "towns"))
    assert r.returncode == 0, r.stderr
    assert (tmp_path / "towns" / "_egress" / "squid.conf").is_file()


def test_egress_skips_folders_that_are_not_towns(env, tmp_path):
    conf = _towns_with_hosts(env, tmp_path, {"slt": ["cityofslt.us"]})
    shutil.copytree(tmp_path / "towns" / "slt", tmp_path / "towns" / "slt.bak")
    r = run(env, "--egress")
    assert r.returncode == 0, r.stderr
    assert "slt.bak" in r.stderr
    assert run(env, "--egress").returncode == 0  # its own _egress folder is no town either
    assert "town_slt" in conf.read_text()


def test_egress_takes_no_slug(env):
    r = run(env, "slt", "--egress")
    assert r.returncode != 0 and "--egress" in r.stderr


# ---- --rerender refreshes a leader's cron; host-path hygiene ---------------

def test_rerender_refreshes_a_leader_towns_cron_from_the_repo(env, town, tmp_path):
    jobs = town / "cron" / "jobs.json"
    jobs.write_text('{"version": 1, "jobs": [{"id": "old-job"}]}\n')
    r = run(env, "testtown", "--rerender")
    assert r.returncode == 0, r.stderr
    assert jobs.read_text() == (tmp_path / "repo" / "cron" / "jobs.json").read_text()
    [backup] = (town / "cron").glob("jobs.json.bak-*")
    assert "old-job" in backup.read_text()
    assert "restart agent" in r.stdout


def test_rerender_leaves_an_emptied_follower_schedule(env, town):
    cron = town / "cron"
    (cron / "jobs.empty.json").rename(cron / "jobs.json")
    before = (cron / "jobs.json").read_text()
    r = run(env, "testtown", "--rerender")
    assert r.returncode == 0, r.stderr
    assert (cron / "jobs.json").read_text() == before
    assert "follower" in r.stdout


def test_rerender_leaves_a_follower_bound_by_override(env, town):
    (town / "docker-compose.override.yml").write_text(
        "services:\n  agent:\n    volumes:\n"
        "      - ./cron/jobs.empty.json:/home/agent/.hermes/cron/jobs.json:ro\n")
    jobs = town / "cron" / "jobs.json"
    jobs.write_text('{"version": 1, "jobs": [{"id": "old-job"}]}\n')
    r = run(env, "testtown", "--rerender")
    assert r.returncode == 0, r.stderr
    assert "old-job" in jobs.read_text()
    assert not list((town / "cron").glob("jobs.json.bak-*"))


def test_an_explicit_codex_home_is_mounted_instead_of_home_codex(env, tmp_path):
    codex = tmp_path / "elsewhere" / "codex"
    codex.mkdir(parents=True)
    (codex / "auth.json").write_text("{}")
    assert run({**env, "CODEX_HOME": str(codex)}, "testtown").returncode == 0
    compose = (tmp_path / "towns" / "testtown" / "docker-compose.yml").read_text()
    assert f"- {codex}:/home/agent/.codex:rw" in compose
    assert f"{env['HOME']}/.codex" not in compose


def test_a_repo_path_with_a_colon_is_refused(env, tmp_path):
    colon = tmp_path / "re:po"
    shutil.copytree(tmp_path / "repo", colon)
    r = run({**env, "ADD": str(colon / "bin" / "townloops-agent-add")}, "testtown")
    assert r.returncode != 0
    assert "':'" in r.stderr
    assert not (tmp_path / "towns" / "testtown").exists()
