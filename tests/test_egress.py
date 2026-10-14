"""engine/egress.py: every town's allowed_hosts compiles into the shared Squid
allowlist. Pure function — plain lists in, squid.conf text out."""

from __future__ import annotations

import pytest

from engine.egress import EgressError, render_squid_conf


def _lines(conf: str) -> list[str]:
    """Directives, whitespace-collapsed."""
    return [" ".join(line.split()) for line in conf.splitlines()
            if line.strip() and not line.lstrip().startswith("#")]


def _acl(conf: str, name: str) -> list[str]:
    """The dstdomain values of acl `name` (continuation lines joined)."""
    text = conf.replace("\\\n", " ")
    for line in text.splitlines():
        words = line.split()
        if words[:3] == ["acl", name, "dstdomain"]:
            return words[3:]
    raise AssertionError(f"no acl {name}")


PLATFORM = {
    "openai": [".openai.com"],
    "chatgpt": [".chatgpt.com"],
    "townloops": [".townloops.com"],
    "telegram": [".telegram.org"],
    "listmonk_lan": ["host.docker.internal"],
}


@pytest.mark.parametrize("towns", [[], [("slt", ["cityofslt.us"])]])
def test_platform_block_is_always_allowed(towns):
    conf = render_squid_conf(towns)
    lines = _lines(conf)
    for name, hosts in PLATFORM.items():
        assert _acl(conf, name) == hosts
        assert f"http_access allow {name}" in lines


def test_deny_all_is_the_last_access_rule():
    conf = render_squid_conf([("slt", ["cityofslt.us"]), ("north-shore", ["sierrasun.com"])])
    access = [line for line in _lines(conf) if line.startswith("http_access")]
    assert access[-1] == "http_access deny all"
    assert access.count("http_access deny all") == 1


def test_each_town_gets_an_acl_and_its_allow_as_a_pair():
    conf = render_squid_conf([("slt", ["cityofslt.us", "ltcc.edu"]), ("north-shore", ["sierrasun.com"])])
    lines = _lines(conf)
    assert _acl(conf, "town_slt") == [".cityofslt.us", ".ltcc.edu"]
    assert _acl(conf, "town_north_shore") == [".sierrasun.com"]
    joined = _lines(conf.replace("\\\n", " "))
    for name in ("town_slt", "town_north_shore"):
        allow = joined.index(f"http_access allow {name}")
        assert joined[allow - 1].startswith(f"acl {name} dstdomain ")
        assert allow < joined.index("http_access deny all")
        assert lines.count(f"http_access allow {name}") == 1


def test_towns_hosts_never_bleed_across():
    conf = render_squid_conf([("a", ["one.com"]), ("b", ["two.com"])])
    assert _acl(conf, "town_a") == [".one.com"]
    assert _acl(conf, "town_b") == [".two.com"]


@pytest.mark.parametrize("entry,expected", [
    ("example.org", ".example.org"),
    (".example.org", ".example.org"),
    ("  Example.ORG ", ".example.org"),
    ("www.example.org", ".www.example.org"),
])
def test_bare_hostnames_are_normalized_to_dstdomain_form(entry, expected):
    assert _acl(render_squid_conf([("t", [entry])]), "town_t") == [expected]


def test_duplicates_and_covered_subdomains_collapse():
    conf = render_squid_conf([("t", ["b.com", "events.b.com", ".b.com", "a.com"])])
    assert _acl(conf, "town_t") == [".a.com", ".b.com"]


@pytest.mark.parametrize("entry", [
    "https://example.org",
    "http://example.org/",
    "example.org/events",
    "example.org:8080",
    "exa mple.org",
    "*.example.org",
    "",
    "localhost",
])
def test_entries_that_are_not_a_hostname_are_rejected_naming_town_and_entry(entry):
    with pytest.raises(EgressError) as e:
        render_squid_conf([("good", ["fine.com"]), ("slt", ["cityofslt.us", entry])])
    assert "slt" in str(e.value) and repr(entry) in str(e.value)


def test_a_town_with_no_hosts_gets_no_acl():
    conf = render_squid_conf([("empty", []), ("t", ["one.com"])])
    assert "town_empty" not in "\n".join(_lines(conf))
    assert "empty" in conf  # named in the header/comment, so its absence is visible


@pytest.mark.parametrize("towns", [
    [("Bad_Slug", ["one.com"])],
    [("t", ["one.com"]), ("t", ["two.com"])],
])
def test_bad_or_repeated_slugs_are_rejected(towns):
    with pytest.raises(EgressError):
        render_squid_conf(towns)


def test_output_is_deterministic_whatever_the_input_order():
    a = render_squid_conf([("slt", ["b.com", "a.com"]), ("north-shore", ["c.com"])])
    b = render_squid_conf([("north-shore", ["c.com"]), ("slt", ["a.com", "b.com"])])
    assert a == b
    assert a == render_squid_conf([("slt", ["b.com", "a.com"]), ("north-shore", ["c.com"])])
    assert _lines(a).index("http_access allow town_north_shore") < _lines(a).index("http_access allow town_slt")


def test_generated_header_names_the_generator_and_inputs():
    conf = render_squid_conf([("slt", ["a.com"]), ("north-shore", ["c.com"])])
    header = conf.split("\n\n", 1)[0]
    assert header.startswith("#")
    assert "GENERATED" in header and "do not edit" in header.lower()
    assert "bin/townloops-agent-add --egress" in header
    assert "allowed_hosts" in header and "north-shore" in header and "slt" in header


def test_hardening_and_access_log_are_preserved():
    lines = _lines(render_squid_conf([]))
    for line in ("http_port 3128",
                 "access_log daemon:/var/log/squid/access.log squid",
                 "cache deny all",
                 "forwarded_for delete",
                 "via off",
                 "http_access deny !Safe_ports",
                 "http_access deny CONNECT !SSL_ports"):
        assert line in lines, line
