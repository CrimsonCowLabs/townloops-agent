# Turnkey onboarding: a scaffolded town that actually works, and one preflight that says so

> Mirrors [issue #14](https://github.com/CrimsonCowLabs/townloops-agent/issues/14) — that issue is canonical.
> Written 2026-10-12. Companion spec on the platform side: [`CrimsonCowLabs/townloops#31`](https://github.com/CrimsonCowLabs/townloops/issues/31).

## Problem Statement

A town Operator is handed this repo and told it will fill their Town. Following the documented path does not produce a working agent, and nothing tells them so.

The artifacts the handoff actually hands over are wrong:

- **`bin/townloops-agent-add` writes a cron schedule from before the event-first rewrite.** Its `cron/jobs.json` heredoc omits `workqueue-heartbeat`, `event-find-am`/`-pm` and `event-seed-discovery-weekly` — the three jobs that drive the current pipeline. It schedules five skills that no longer exist (`venue-enrichment`, `business-enrichment`, `performer-enrichment`, `community-discovery`, `event-source-harvest`) and two scripts that are not in `scripts/` at all (`enrichment_sweep.sh`, `discovery_resweep.sh`). A freshly scaffolded town **never drains its workqueue and never finds an event.** The repo's own `cron/jobs.json` is the correct event-first schedule; only the scaffolder's frozen copy is wrong, so the defect is invisible to anyone reading the repo rather than the output.

- **`templates/per-town-agent.docker-compose.yml.tmpl` hardcodes `/home/aaron/…` at nine bind mounts** — seven at `/home/aaron/workspace/townloops-agent`, two at `/home/aaron/.codex`, plus the build context. On any other host the stack cannot start. `tests/test_townloops_agent_add.py` is green because its fixture rewrites the template string first, so the suite certifies a file nobody else can use.

- **`town-init setup` never reads `town-profile.yaml`.** It verifies `/healthz`, proves the bearer works for the Town, best-effort checks Listmonk, snapshots the Category taxonomy and migrates the state DB — and then exits `ok: true` with the profile entirely unexamined. The one correctness check that exists for it, `policy.has_category`, fires deep inside a later cron run, hours after the Operator walked away believing setup passed.

- **`allowed_hosts` is read by no code in this repo.** `town-profile.yaml` documents it as feeding both discovery auto-approve and the Squid allowlist; `docs/vps-quickstart.md` tells the Operator each entry "is a website the agent trusts"; `SOUL.md` says egress honours it. Grep finds it in documentation and templates only. The same is true of `AUTO_APPROVE_EVENT_SOURCES`, which appears in `templates/env.example` and nowhere else. The Operator is asked to curate a trust boundary that is inert.

- **Egress is actually two hand-maintained lists that must agree.** `templates/_shared/egress/squid.conf` carries hand-written `acl tahoe_north` / `acl tahoe_south` blocks, each needing a matching `http_access allow` line, in a *shared* stack serving every town. Its own header promises "re-generating this file via a follow-up tool"; that tool does not exist. Adding one event source means editing a shared config in another directory and restarting a shared container — and the dashboard ships a "Blocked" panel whose entire job is tailing Squid 403s so a human can reconcile the two lists after the fact.

- **`docs/vps-quickstart.md` tells the Operator to buy an Anthropic key and load $5 of credit.** There is no Anthropic path in this repo; the LLM is ChatGPT through the Codex CLI (`config.yaml` → `provider: openai-codex`). The same doc describes "the agent's current phase" — the phase machine is gone — and documents the shared-Firecrawl topology while `README.md` documents the bundled one, so two readers of two first-party docs stand up two different stacks.

- **The publish-Role footgun lands after everything is up.** `autonomy.GatedClient.create` correctly detects a key that can publish, opens the circuit with an exact remedy ("the API key can publish — switch the town to an `api_ingest` key without publish permission"), and stops all submissions. But it can only learn this on the first ship, and the channels it has are Telegram (optional) and the dashboard (which an autonomous town deliberately does not run). The Operator's experience is a quiet agent.

The repo is not the problem. It has ~250 in-process tests, a fail-closed cross-town guard, a deterministic town-scoped `Idempotency-Key`, an OpenAPI drift detector that freezes writes, and an autonomy policy with a rejection-rate breaker. What it lacks is a handoff that produces a working town and says so out loud.

## Solution

Two things, plus the doc corrections that follow from them.

**1. The scaffolder produces a correct, portable, bootable town.**

The cron schedule stops being a frozen copy and becomes the repo's own `cron/jobs.json`, so there is one schedule and it cannot drift from the skills and wrappers that exist. Every host path in the compose template becomes a substitution like `{{SLUG}}` already is, resolved from where the repo actually sits and where the invoking user's Codex login actually is. An invariant test asserts that every skill and script a scaffolded town schedules is present in the repo — the assertion that would have caught this.

**2. `town-init preflight` gives one verdict before `docker compose up`.**

One command, one exit code, one list of findings an Operator can act on without reading code. It reads the profile that nothing reads today and checks it against the live Town: that every `category_slug` in `discovery.community.query_groups` exists in the taxonomy we just snapshotted, that the credential authenticates and is scoped to this slug, that `operating_mode` and the dashboard configuration agree, that Firecrawl is reachable and local, that the Codex login is present, that `AGENT_TZ` is a real zone, and that no autonomy circuit is already open — including the publish-key circuit, which is recorded in `autonomy_state` and is therefore reportable by a command the Operator can run at any time rather than only discoverable from a Telegram message they may not have configured.

Findings are **errors** (the agent cannot work) or **warnings** (it will work with a piece missing — no Listmonk, no Telegram). Each names the file and the key, and states the consequence. `town-init setup` runs preflight at the end, so a first boot that would be useless says so instead of printing `ok: true`.

**3. `allowed_hosts` gets its first real consumer: the Squid ACL is compiled from the profiles.**

Every town's `allowed_hosts` compiles into the shared `squid.conf` — one generated `acl town_<slug>` plus its `http_access allow`, over a fixed platform block (OpenAI/ChatGPT, Townloops, Telegram, Listmonk, Firecrawl). Adding an event source becomes one edit in the one file the quickstart already tells the Operator to edit, then regenerate and reload. The two-lists-must-agree class disappears, the key stops being documentation, and the dashboard's "Blocked" panel becomes a diagnostic rather than the reconciliation mechanism.

## User Stories

1. As an Operator, I want a scaffolded town to schedule the pipeline that actually exists, so that my agent drains its workqueue instead of doing nothing.
2. As an Operator, I want a scaffolded town to run event discovery and find, so that events arrive without me learning which cron jobs were missing.
3. As an Operator, I want the scaffolder to schedule only skills present in the repo, so that a renamed or deleted skill can't leave me with a silently dead job.
4. As an Operator, I want the scaffolder to schedule only wrapper scripts present in `scripts/`, so that a cron entry can't point at a file that was never written.
5. As a maintainer, I want one cron schedule in the repo rather than a copy inside a bash heredoc, so that the next pipeline rewrite updates the scaffolder by construction.
6. As a maintainer, I want a test that fails when a scaffolded town schedules something that doesn't exist, so that this class of defect is caught by the suite rather than by an Operator.
7. As an Operator on my own machine, I want the generated compose file to reference the repo where it actually is, so that `docker compose up` works on a host that isn't the author's.
8. As an Operator, I want the generated compose file to mount my own Codex login, so that the LLM steps authenticate as me.
9. As a maintainer, I want the compose template's host paths to be substitutions like `{{SLUG}}` already is, so that no test needs to rewrite the template to make it usable.
10. As a maintainer, I want the scaffolder test to run against the real template rather than a patched copy, so that a green suite means a bootable town.
11. As an Operator, I want one command that tells me whether my town will work, so that I learn about a misconfiguration before I walk away.
12. As an Operator, I want that command to exit non-zero when the agent cannot work, so that I can put it in a script or a checklist.
13. As an Operator, I want each finding to name the file and the key it concerns, so that fixing it doesn't require reading Python.
14. As an Operator, I want each finding to state the consequence, so that I can tell a blocker from a missing optional feature.
15. As an Operator, I want findings split into errors and warnings, so that "no Listmonk yet" doesn't read like "your agent is broken".
16. As an Operator, I want preflight to verify my API Key authenticates, so that a mistyped key is caught at setup rather than at the first ship.
17. As an Operator, I want preflight to verify my key is scoped to the Town slug I configured, so that a key for the wrong Town is caught before it writes anywhere.
18. As an Operator, I want preflight to check every `category_slug` in my profile against the live taxonomy, so that a community query group doesn't silently enqueue nothing for weeks.
19. As an Operator, I want preflight to name which slugs were unknown and which ones exist, so that correcting the profile is a copy-paste.
20. As an Operator, I want preflight to tell me when a configured Category exists but my snapshot is stale, so that I re-run setup instead of editing a correct profile.
21. As an Operator, I want preflight to report an open autonomy circuit and its reason, so that "my agent went quiet" has an answer from a command rather than from a Telegram history.
22. As an Operator in autonomous mode, I want preflight to report the publish-key circuit in plain terms, so that I know to re-mint the credential without publish.
23. As an Operator in supervised mode, I want preflight to check `DASHBOARD_PASSWORD` is set, so that I don't bring up a dashboard every login fails against.
24. As an Operator in autonomous mode, I want preflight to check the dashboard really is disabled, so that the mode I asked for is the mode I got.
25. As an Operator, I want preflight to check `operating_mode` against the compose override, so that an autonomous town with a stale override is caught rather than quietly running a review surface nobody reads.
26. As an Operator, I want preflight to verify Firecrawl is reachable, so that a down scrape stack isn't diagnosed as an empty town.
27. As an Operator, I want preflight to confirm Firecrawl is local and not cloud, so that the repo's no-cloud rule is checked rather than assumed.
28. As an Operator, I want preflight to check the Codex login is present, so that the LLM steps aren't discovered to be no-ops a day later.
29. As an Operator, I want preflight to validate `AGENT_TZ` is a real zone, so that every date the agent computes isn't quietly wrong.
30. As an Operator, I want preflight to warn when `AGENT_TZ` is the fallback and my town is nowhere near Pacific, so that a non-Pacific town gets a prompt rather than a surprise.
31. As an Operator, I want preflight to tell me the profile fields it cannot verify, so that I know `name` and `region` are on my honour rather than assuming they were checked.
32. As an Operator, I want preflight to warn when Listmonk is configured but unreachable, so that a newsletter failure is foreseen rather than discovered on a Sunday.
33. As an Operator, I want preflight to warn when Listmonk is unconfigured, so that I know the newsletter is off rather than broken.
34. As an Operator, I want preflight to warn when Telegram is unconfigured in autonomous mode, so that I understand I have given up my only push notification.
35. As an Operator, I want `town-init setup` to run preflight at the end, so that a first boot cannot print `ok: true` over a town that will do nothing.
36. As an Operator, I want to re-run preflight any time, so that it doubles as the first diagnostic when something stops.
37. As an Operator, I want preflight to be read-only, so that running it to diagnose a problem cannot make one.
38. As an Operator, I want preflight to be cheap, so that running it is never a reason to wait or to burn my rate limit.
39. As an Operator, I want to add an event source by editing one file, so that curating my agent's trust boundary doesn't require touching a shared stack.
40. As an Operator, I want `allowed_hosts` to actually control egress, so that the list I was told to curate carefully does what I was told it does.
41. As an Operator, I want the shared Squid config generated from every town's profile, so that adding my host can't break another town's.
42. As an Operator, I want regenerating the ACL to be one command, so that the loop from "blocked host" to "allowed host" is short.
43. As an Operator, I want the generated config to keep the platform hosts the agent always needs, so that regenerating can't cut off Townloops or the LLM.
44. As an Operator, I want the generated file to say it is generated and name its inputs, so that nobody hand-edits a file that will be overwritten.
45. As an Operator, I want regeneration to be idempotent, so that running it twice is safe and a no-op diff is proof nothing changed.
46. As an Operator, I want a host that doesn't belong in an allowlist rejected when the ACL is compiled, so that a typo or a bare path doesn't silently widen the boundary.
47. As an Operator, I want the dashboard's Blocked panel to keep pointing at the one file I now edit, so that the help text matches the new mechanism.
48. As an Operator reading the quickstart, I want it to name the LLM the repo actually uses, so that I don't buy credit on a service this agent never calls.
49. As an Operator reading the quickstart, I want the steps to match the pipeline that exists, so that I'm not watching for a phase the agent no longer has.
50. As an Operator, I want one documented topology rather than two, so that the README and the quickstart stand up the same stack.
51. As an Operator, I want the quickstart to tell me to run preflight before watching logs, so that the first thing I do is the thing that answers whether it worked.
52. As a maintainer, I want dead configuration keys either wired up or removed, so that the Operator's attention goes to settings that do something.
53. As the platform owner, I want onboarding a second town to be the same short path as the first, so that handing an agent to an Operator stops being a bespoke engagement.

## Implementation Decisions

**Scope is this repo only.** Nothing here requires a Townloops change. Two platform gaps are noted below as dependencies and designed around, not waited on.

**One cron schedule, not two.** `bin/townloops-agent-add` stops embedding a schedule and installs the repo's own `cron/jobs.json` — the file `AGENTS.md` already calls the source of truth and that `tests/test_cron_migrate.py` already constrains. A scaffolded town is therefore scheduled exactly as the repo says, and the next pipeline change updates every future town for free. The follower-mode `cron/jobs.empty.json` keeps being written as it is.

Per-town divergence, if a town ever needs it, belongs in that town's `docker-compose.override.yml` — the file the template already designates for host- and town-specific changes — not in a second copy of the schedule.

**Host paths become substitutions.** The template gains `{{REPO}}` and `{{CODEX_HOME}}` alongside `{{SLUG}}`, resolved by the scaffolder from the repo it is running out of (it already computes `REPO`) and from the invoking user's home. This is not a new mechanism — `render()` already does `sed` substitution, and `{{SLUG}}` proves the shape. `--rerender` picks them up by definition, which is what makes existing towns recoverable.

`tests/test_townloops_agent_add.py`'s fixture currently rewrites `/home/aaron/.codex:` out of the template. That rewrite is deleted, not updated: a fixture that patches the artifact under test is how this defect survived, and the test is only meaningful once it runs the real template.

**Preflight is one function in `engine/`, exposed as a verb on an existing skill.** `engine/preflight.py` holds the checks and returns a list of findings; `town-init preflight` is a thin CLI over it. This follows the repo's stated layering — "`engine/` is the shared library, the single source of truth; no skill logic here" — and keeps the count at one new module and one new verb rather than a new skill with its own `SKILL.md` and its own drift.

A finding is a small record: a severity (`error` / `warning`), a stable machine-readable code, the file-and-key it concerns, and a one-line consequence. The CLI prints them human-first and `--json` for the dashboard and for scripting. Exit code is `0` with no errors, non-zero with any — warnings never fail the run, because "no Listmonk yet" is a normal state for a town mid-onboarding.

**Preflight is read-only and cheap.** It performs no writes and no probe-by-attempting. Concretely it must not try to learn the key's Role by creating a throwaway record (that leaves a row an `api_ingest` key cannot clean up) nor by attempting a forbidden action. It also must not repeat `town-init status`'s mistake of fully paginating six kinds — the one-row `list(kind, limit=1)` reads `town-init setup` already does are enough to prove auth and scope.

**The checks, and what each can actually establish:**

- *Credential* — the existing `healthz` + one-row read already in `cmd_setup`, promoted into preflight so one component owns it. Proves the key authenticates and is scoped to `TOWNLOOPS_TOWN_SLUG`. `verify_town()` is the stronger form and already fails closed on snapshot drift; preflight reports its error rather than reimplementing it.
- *Profile Categories* — every `discovery.community.query_groups[].category_slug` checked with `policy.has_category` against the snapshot `setup` just wrote. This is the one check the repo already has a function for and never runs early; preflight is where it belongs.
- *Operating mode coherence* — `autonomy.settings()` is already the one reader of `operating_mode`. Autonomous requires the dashboard disabled; supervised requires `DASHBOARD_PASSWORD`. The scaffolder's `--autonomous` path and the `is_autonomous_town` grep already encode this pairing; preflight asserts it rather than trusting the scaffolder was used.
- *Open circuit* — `autonomy_state` key `circuit` holds `{reason, evidence, at}`. Preflight reports it with the reason verbatim. This is what makes the publish-key failure diagnosable from a command: `GatedClient` already writes the exact remedy into `evidence` ("switch the town to an `api_ingest` key without publish permission"), and preflight surfaces it without needing to know what a Role is.
- *Firecrawl* — reachability plus the existing `engine/firecrawl_env.py` rule that a cloud URL is refused. Reuse that module; do not re-derive the rule.
- *Codex login* — presence of the mounted login the compose template already bind-mounts. Presence only; a token's validity is the CLI's business and an expired one is a throttle, which the pipeline already treats as a no-op-and-retry.
- *Timezone* — `AGENT_TZ` resolves through `zoneinfo`. An invalid zone is an error; the Pacific fallback is a warning, because a non-Pacific town silently computing Pacific dates is the failure this catches.
- *Listmonk / Telegram* — warnings, never errors. Listmonk configured-but-unreachable and Telegram absent in autonomous mode are both worth saying and neither stops ingest.

**Two platform gaps, designed around.** The ingest API exposes no Town read (no `GET /towns/{slug}`), so `name`, `region` and the Town's own timezone cannot be verified — preflight reports them as unverifiable rather than silently implying it checked. The API also does not report an API Key's own Role, which is why the publish footgun is caught via the circuit after the fact rather than before. Both are small additive reads worth filing against `townloops`; neither blocks anything here, and if either lands, preflight gains a check without changing shape.

**The Squid ACL is compiled, not edited.** `engine/egress.py` holds one pure function: a list of `(slug, allowed_hosts)` in, the full `squid.conf` text out. No I/O, no network, no DB — which is what makes it trivially testable and what keeps the generator from growing a second responsibility.

- A fixed platform block (OpenAI, ChatGPT, Townloops, Telegram, Listmonk, Firecrawl) is emitted unconditionally, so regenerating can never cut the agent off from the API it ships to.
- Each town contributes one `acl town_<slug> dstdomain …` and one `http_access allow town_<slug>`, emitted as a pair. Emitting them together is the point — the current file needs both and they are maintained separately, which is the actual bug.
- `http_access deny all` stays last, and the access-log and `forwarded_for delete` / `via off` hardening is preserved verbatim.
- Entries are normalized to the leading-dot `dstdomain` form Squid wants and validated: a bare hostname is normalized, a URL with a scheme or path is an error naming the town and the offending entry. A typo must not silently widen a trust boundary.
- Output is deterministic and ordered, so regenerating with no profile change is a byte-identical no-op and `git diff` is proof.
- The file is written with a generated-by header naming its inputs, so nobody hand-edits it.

The scaffolder is the invoker, via a new mode — it already knows `TOWNLOOPS_AGENTS_ROOT` and therefore where every town's profile is, and making it the single entry point avoids a second tool that needs its own discovery logic. Reloading Squid stays an explicit documented step, not something a generator does implicitly to a shared container other towns depend on.

The hand-written `tahoe_north` / `tahoe_south` ACLs are migrated by moving their hosts into the two towns' `allowed_hosts`, which is where the quickstart already says they go. After that, `squid.conf` is generated and the per-town lists live in one place each.

**Dead keys resolved, not documented.** `allowed_hosts` gains its consumer. `AUTO_APPROVE_EVENT_SOURCES` is read by nothing and describes a skill that no longer exists (`event-source-discovery`); it comes out of `templates/env.example`. Seed approval in the current pipeline is `autonomy`'s seed trial/trust decision, which is configured in `town-profile.yaml`, not by an env flag.

**Docs follow the code, in the three places that currently mislead.** `docs/vps-quickstart.md`: the Anthropic key and its $5 of credit become the Codex CLI login; the phase-watching step becomes the workqueue and a preflight run; the topology section picks one stack and says which. `README.md` and `docs/skill-inventory.md` already carry `AGENTS.md`'s warning that they describe superseded skill names — the quickstart carries no such warning and is the one an Operator is pointed at first, so it is the one that has to be right. `dashboard/templates/blocked.html` points at the one file an Operator now edits.

## Testing Decisions

A good test here asserts what the Operator gets: the files the scaffolder writes, the verdict preflight returns, the text the compiler emits. It does not assert that a particular helper was called or that a message has particular wording. The strongest tests are invariants over the artifacts — "everything a scaffolded town schedules exists" is worth more than any number of example-based checks, because it keeps holding as skills are renamed.

**Existing seams, extended:**

- **`tests/test_townloops_agent_add.py`** — already subprocess-runs the real `bin/townloops-agent-add` against a tmp `TOWNLOOPS_AGENTS_ROOT` with a stub `HOME`, and already covers `--rerender`. This is the seam for everything scaffolder-shaped: the installed schedule matches the repo's, the rendered compose contains no path outside the tmp fixture, `--autonomous` writes the override and flips `operating_mode`, re-running refuses to overwrite. The fixture's template rewrite is removed as part of the work, which makes every assertion in the file mean more than it does today.

- **`tests/test_cron_migrate.py`** — already enforces an invariant across `cron/jobs.json` and `scripts/` by globbing the repo, which is exactly the shape the new invariant needs. The "every skill and script a scaffolded town schedules exists" assertion joins this family, globbing `skills/business/` and `scripts/` the same way rather than listing names a rename would strand.

- **`tests/fake_townloops.py:FakeAPI` + the `conftest.py` fixtures** (`tmp_state_dir`, `fresh_db`) — the established seam for anything that talks to Townloops, injected in place of `Client`. Every preflight check that reads the API goes through it: a wrong-slug key, a key that fails auth, a Category snapshot that disagrees with live. `FakeAPI` already models the error shapes (`TownloopsError`, cross-town 404) and `verify_town` is already covered in `tests/test_townloops_api.py` in all four regimes — preflight's job is to report those outcomes, and the tests assert the finding, not the mechanism.

- **`tests/test_autonomy_*.py`** — prior art for `autonomy_state`, `settings()` and the circuit. The circuit-reporting check is tested here-adjacent: open the circuit the way `GatedClient` does, then assert preflight reports it as an error carrying the reason.

**One new test file for the compiler.** `engine/egress.py` is a pure function and gets a focused file in the same style as `tests/test_policy.py`: the platform block always present, `deny all` last, a town's `acl` and its `http_access allow` always emitted as a pair, two towns' hosts never bleeding across, an entry with a scheme or path rejected naming the town, normalization of a bare hostname, and byte-identical output for the same input. No fixtures beyond plain dicts — no I/O is the reason it is a separate module.

**Preflight's own tests live with the checks, driven by `FakeAPI` and `tmp_state_dir`.** The assertions are on findings: severity, code, and that an error is present or absent. One test per failure mode, plus one that a fully-configured town returns no errors — the baseline that keeps the checks from drifting into false positives.

**The test to write first is the scaffolded-cron invariant.** It is the one that fails today, it is the defect with the largest blast radius, and writing it first means the fix is verified rather than asserted.

## Out of Scope

- **Anything in the `townloops` repo.** The Town read endpoint and a key-introspection read are both noted as dependencies and designed around; they want their own issues there. `townloops#31` covers the platform-side half of agent legibility (Event provenance, an Ingest health panel, honest key-mint copy) and is independent of this.

- **A hosted or managed fleet.** Running the agents ourselves so an Operator never touches Docker would dissolve most of this, and it is a business-model and infrastructure decision, not an onboarding fix. This spec makes the self-hosted path work.

- **A full interactive wizard.** No prompting walkthrough that performs the Codex device-flow login, generates `API_SERVER_KEY`, or bcrypts the dashboard password. Those are steps only a human can take (a browser device flow) or one-line commands the docs already give; the value here is a correct artifact and a verdict, and a wizard that wraps steps it cannot complete adds surface without removing work. Worth revisiting once preflight has told us which steps Operators actually get wrong.

- **Changing the pipeline, the autonomy policy, or any thresholds.** `operating_mode`, seed trust, the circuit breaker and the submission caps are all unchanged. Preflight reads them; it never adjusts them.

- **Probing the API Key's Role.** Explicitly rejected above: no throwaway create, no attempt-a-forbidden-action probe. The circuit report covers the case after one ship, and a clean check needs the platform read.

- **Automatic Squid reload.** The generator writes the file; reloading a shared container that other towns' egress depends on stays an explicit step with an explicit blast radius.

- **Consolidating the four ad-hoc profile loaders.** `engine/derive_helpers.load_town_profile` exists, and `event-find`, `event-seed-discovery` and `event-reconcile` each reimplement it locally. That is a real drift hotspot and a separate, mechanical change; preflight uses the `engine/` one and does not add a fifth.

- **Retiring the legacy `event-ingest` path or the entity-first dashboard views.** Both are deliberately kept for compatibility per `AGENTS.md`. Nothing here removes them; the scaffolder simply stops scheduling the skills that were actually deleted.

- **`docs/STATUS.md`.** Already marked superseded at the top. It stays a historical document.

## Further Notes

**Why the frozen-heredoc defect survived.** Everything that would have caught it looks at the repo: `AGENTS.md` names `cron/jobs.json` the source of truth, `tests/test_cron_migrate.py` constrains it, and both are correct. The scaffolder's copy is the only wrong one, and the only reader is a new town. That is the general lesson for this work — **the handoff artifacts need invariants of their own**, because the repo being right is not evidence that what it hands out is.

**The same pattern explains the compose paths.** `tests/test_townloops_agent_add.py` is a good test with a fixture that rewrites the thing under test. The hardcoded path was known well enough to be worked around and never fixed, which is the most expensive kind of known issue: it looks handled.

**`allowed_hosts` is the clearest case for building the generator rather than reporting drift.** Three first-party documents tell the Operator that list controls what the agent may reach. Code reads it nowhere. Having preflight report that `squid.conf` is missing some of its entries would make the key half-real — still two lists, still a shared file, still a manual reconciliation, but now with a nag. Compiling the ACL is what makes the documentation true, and it is the cheaper end state.

**Preflight is also the first diagnostic, not only a setup gate.** Most of what it checks can break later: a key gets revoked, a Category gets renamed, Firecrawl falls over, the Codex login expires, the circuit opens. Designing it as read-only and cheap is what lets "run `town-init preflight`" be the first line of every troubleshooting section — which is most of what the quickstart's troubleshooting chapter is currently doing by hand, one `docker compose exec` at a time.

**The circuit is already well-designed; it just needs a second channel.** `open_circuit` writes a reason and evidence, alerts once, and lets collecting and enriching continue while submissions stop. For a supervised town the dashboard shows it. For an autonomous town — the mode we recommend to Operators, and the one where the Townloops queue is deliberately the only human gate — the alert is Telegram or nothing. A command that reports it closes that gap from this side, and `townloops#31`'s Ingest panel closes it from the other: the Operator sees the submissions stop, and preflight says why.
