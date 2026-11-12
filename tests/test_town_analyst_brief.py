"""The town-analyst brief names only commands that exist (#64, seam 2): every
`<skill> <verb> [--flag …]` in its SKILL.md and references — inline in
backticks or a `python $S/<skill>/<script>.py …` line in a code block —
resolves to a real CLI verb chain whose `--help` lists each flag; every
`/home/agent/.hermes/scripts/<x>.py` it names exists and takes its flags."""

from __future__ import annotations

import functools
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SKILLS = ROOT / "skills" / "business"
BRIEF = SKILLS / "town-analyst"
ENTRIES = {p.parent.name: p for p in SKILLS.glob("*/*.py")}
_FENCE = re.compile(r"```.*?\n(.*?)```", re.DOTALL)
_SKILL_LINE = re.compile(r"python \$S/([a-z0-9-]+)/[a-z0-9_]+\.py(.*)")
_SCRIPT = re.compile(r"/home/agent/\.hermes/scripts/([a-z0-9_]+\.py)((?: --?[a-z][a-z0-9-]*)*)")


def brief_files() -> list[Path]:
    return [BRIEF / "SKILL.md", *sorted((BRIEF / "references").glob("*.md"))]


def commands(text: str) -> list[tuple[str, list[str]]]:
    """(skill, argv tokens) of every command the text names."""
    out = []
    for block in _FENCE.findall(text):
        for line in block.splitlines():
            m = _SKILL_LINE.search(line.split("  #")[0])
            if m:
                out.append((m.group(1), m.group(2).split()))
    prose = _FENCE.sub("", text)
    for span in re.findall(r"`([^`]+)`", prose):
        tokens = span.split()
        if tokens and tokens[0] in ENTRIES:
            out.append((tokens[0], tokens[1:]))
    return out


def scripts(text: str) -> list[tuple[str, list[str]]]:
    return [(m.group(1), m.group(2).split()) for m in _SCRIPT.finditer(text)]


@functools.lru_cache(maxsize=None)
def help_text(skill: str, chain: tuple[str, ...]) -> str:
    env = {**os.environ, "TOWNLOOPS_TOWN_SLUG": "slt", "HERMES_HOME": "/nonexistent",
           "TOWN_PROFILE_PATH": "/nonexistent/town-profile.yaml",
           "TOWNLOOPS_DB_PATH": "/nonexistent/townloops.db", "PYTHONPATH": str(ROOT)
           + os.pathsep + os.environ.get("PYTHONPATH", "")}
    proc = subprocess.run([sys.executable, str(ENTRIES[skill]), *chain, "--help"],
                          capture_output=True, text=True, timeout=120, env=env, cwd="/")
    assert proc.returncode == 0, f"{skill} {' '.join(chain)} --help failed: {proc.stderr[-800:]}"
    return proc.stdout


def subcommands(help_: str) -> set[str]:
    m = re.search(r"\{([a-z0-9,-]+)\}", help_)
    return set(m.group(1).split(",")) if m else set()


def has_flag(help_: str, flag: str) -> bool:
    return re.search(rf"(?<![\w-]){re.escape(flag)}(?![\w-])", help_) is not None


def resolve(skill: str, tokens: list[str]) -> list[str]:
    """Walk the tokens down the CLI's verbs; the problems found."""
    chain: list[str] = []
    positional = False
    problems = []
    for t in tokens:
        if t.startswith("--"):
            flag = t.split("=")[0]
            if not has_flag(help_text(skill, tuple(chain)), flag):
                problems.append(f"{skill} {' '.join(chain)}: no {flag}")
            positional = True     # what follows a flag is its value
            continue
        if positional:
            continue
        subs = subcommands(help_text(skill, tuple(chain)))
        if subs and re.fullmatch(r"[a-z][a-z0-9-]*", t):
            if t not in subs:
                problems.append(f"{skill} {' '.join(chain)}: no verb {t!r} (has {sorted(subs)})")
                return problems
            chain.append(t)
            continue
        positional = True         # a positional value or a placeholder
    if not chain and subcommands(help_text(skill, ())):
        problems.append(f"{skill}: named without a verb")
    return problems


CITED = sorted({(f.name, skill, " ".join(tokens))
                for f in brief_files() for skill, tokens in commands(f.read_text())})


def test_the_brief_names_commands():
    assert len(CITED) >= 25
    named = {skill for _, skill, _ in CITED}
    assert {"town-report", "event-seed-discovery", "event-find", "autonomy",
            "business-discovery", "entity-live-sync", "performer-research",
            "local-data"} <= named


@pytest.mark.parametrize("where,skill,args", CITED)
def test_every_cited_command_resolves_to_a_real_cli_verb(where, skill, args):
    assert not resolve(skill, args.split())


@pytest.mark.parametrize("path", brief_files(), ids=lambda p: p.name)
def test_every_cited_cron_wrapper_exists_and_takes_its_flags(path):
    for script, flags in scripts(path.read_text()):
        src = (ROOT / "scripts" / script).read_text()
        assert all(f'"{flag}"' in src for flag in flags), (script, flags)


def test_every_reference_the_brief_points_at_exists():
    text = (BRIEF / "SKILL.md").read_text()
    for ref in re.findall(r"references/([a-z0-9-]+\.md)", text):
        assert (BRIEF / "references" / ref).is_file() or \
            (SKILLS / "local-data" / "references" / ref).is_file(), ref
