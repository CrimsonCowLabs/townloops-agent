"""bin/townloops-agent-add --rerender: re-rendering an existing town's compose
from the template keeps the stack bootable, or refuses without touching it."""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def env(tmp_path):
    """A copy of the tool whose template mounts a stub Codex login, so the
    tests never depend on this host's ~/.codex."""
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    (home / ".codex" / "auth.json").write_text("{}")
    repo = tmp_path / "repo"
    shutil.copytree(ROOT / "bin", repo / "bin")
    shutil.copytree(ROOT / "templates", repo / "templates")
    tmpl = repo / "templates" / "per-town-agent.docker-compose.yml.tmpl"
    tmpl.write_text(tmpl.read_text().replace("/home/aaron/.codex:", f"{home}/.codex:"))
    return {**os.environ, "HOME": str(home), "TOWNLOOPS_AGENTS_ROOT": str(tmp_path / "towns"),
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
