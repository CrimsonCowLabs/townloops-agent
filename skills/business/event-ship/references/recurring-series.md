# Recurring-series normalisation

From the CMO's `townloops-repeated-event-normalization` note. Use when several
events are occurrences of the same show / class / meeting.

1. Pick the canonical occurrence that already has the right enrichment.
2. Select the run by a stable series signal — same `source_url` branch/prefix,
   same organiser + recurring slug pattern, or same title family. Title-only
   matching misses variants; prefer the source branch.
3. Copy only **shared** fields (description, categories, tags, venue when the
   series has one venue). Never overwrite per-occurrence identity: date,
   start/end, slug.
4. Title rules (e.g. a `TRPA: ` prefix on agency meetings) are idempotent —
   add only when absent, never double-prefix.
5. Ship each occurrence as a full-body PATCH (engine/live_patch) and verify:
   row count matches, no unsynced rows remain, each title has the prefix once.

A destination calendar's series title can map to a more specific live
occurrence (a "Music on the Beach" listing ↔ that night's named-performer
card) — match by date, source URL and venue, not title, before calling it
missing. Reusing a sibling's verified description is fine only when it is
clearly the same event family.
