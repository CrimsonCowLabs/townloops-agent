# Specs

One file per spec, mirroring a GitHub issue in this repo.

**The issue is canonical.** These files exist so a spec survives in the repo
and is readable offline, next to the code it describes. When the two disagree,
the issue wins — it carries the comment thread, the labels, and whatever was
decided after the spec was written.

Naming: `NNN-short-slug.md`, where `NNN` is the issue number. A spec with no
issue does not belong here; file the issue first.

Specs are written by `/to-spec` and carry the sections that skill's template
defines: Problem Statement, Solution, User Stories, Implementation Decisions,
Testing Decisions, Out of Scope, Further Notes.

A spec here may be constrained by an ADR in the **TownLoops platform** repo —
this repo has no `docs/adr/` of its own, and every "ADR NNNN" reference in
`AGENTS.md` and the skill docs points there (see
[`../agents/domain.md`](../agents/domain.md)). A spec that reverses one of
those decisions needs the ADR updated on the platform side too.
