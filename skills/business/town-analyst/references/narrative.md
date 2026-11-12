# The narrative and the notebook (run order steps 1 and 8)

`town-report build` writes the counts, Needs you, holds and failures from
the DB itself. My narrative (`--narrative FILE`, included verbatim under
"Analyst notes") says what the numbers can't. Short — the operator reads it
on a phone.

## Narrative shape

```markdown
**Today:** one or two sentences — what I worked on and the headline result.

**Sources:** adopted / rejected today and why (seed names), adapters set,
hosts opened.

**Events:** holds I resolved (accepted/declined, why) and the ones I left;
stuck causes fixed.

**Organisations:** discovered, matched to live (slug), submitted.

**Enrichment:** what got images/descriptions; what search couldn't find.

**Moderation:** what TownLoops published / rejected (reasons) and what I
change because of it.

**Circuit / stalls** (only when submissions are stopped): the reason and
evidence from `town-report alerts`, what caused it, the fix I propose, and
that `autonomy resume` is the operator's step once it's fixed.

**Needs you:** anything beyond the report's own list — a duplicate on live,
an identity only the operator can settle. Say for each whether it is live,
local or both.
```

## Notebook (`$HERMES_HOME/analyst/notebook.md`)

Read at step 1, rewritten at step 8. Keep it under ~150 lines: drop what's
done, keep what's open.

```markdown
# Analyst notebook — <town>

## Leads            (sources or organisations to look at, with the evidence)
- <date> <url> — <why it may be a source>; next: <step>

## Retry            (blocked on something that may clear)
- <url or uid> — <blocker>; retry after <date>

## Open questions   (for the operator or for a later run)
- <question> — <evidence>

## Last run
- <date> run #<id>: <status>, <one-line result>
```
