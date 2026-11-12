# Local data — schema reference

Generated from `engine/migrations/` by `local_data.py schema` — never edit by hand;
`tests/test_town_report.py` fails when it no longer matches the migrations.

The town's state DB is `$HERMES_HOME/state/townloops.db` (SQLite, WAL). Timestamps
are ISO-8601 UTC text unless a column says otherwise; `*_json` / `extra_json` columns
hold JSON text. Log a direct edit with `local_data.py log --reason "…"`.
A note is the comment the table's first migration gave the column; later
migrations may have widened it (e.g. more status values) — `engine/migrations/` says.

Tables: `ad_spots`, `agent_activity`, `agent_state`, `analyst_runs`, `autonomy_state`, `businesses`, `chat_messages`, `chat_read_state`, `communities`, `creators`, `discovery_candidates`, `enrichment_queue`, `event_seeds`, `event_workqueue`, `events`, `job_health`, `linked_events`, `listing_website_clears`, `newsletters`, `performers`, `post_counters`, `schema_version`, `source_runs`, `sponsors`, `submissions`, `venues`

## ad_spots

| column | type | not null | default | pk | note |
|---|---|---|---|---|---|
| ad_spot_uid | TEXT |  |  | 1 | sha1(sponsor_uid\|placement\|starts_at) |
| sponsor_uid | TEXT | yes |  |  |  |
| placement | TEXT | yes |  |  | 'newsletter-header' \| ... (see docs/api-sponsor-schema-proposal.md) |
| asset_url | TEXT | yes |  |  |  |
| click_url | TEXT | yes |  |  |  |
| alt_text | TEXT |  |  |  |  |
| starts_at | TEXT |  |  |  |  |
| ends_at | TEXT |  |  |  |  |
| weight | INTEGER | yes | 1 |  |  |
| content_hash | TEXT | yes |  |  |  |
| townloops_id | TEXT |  |  |  |  |
| pushed_at | TEXT |  |  |  |  |
| pushed_hash | TEXT |  |  |  |  |

Index `idx_ad_spots_placement`: (placement, starts_at)

Unique `sqlite_autoindex_ad_spots_1`: (ad_spot_uid)

## agent_activity

| column | type | not null | default | pk | note |
|---|---|---|---|---|---|
| id | INTEGER |  |  | 1 |  |
| ts | TEXT | yes |  |  |  |
| worker | TEXT |  |  |  | skill name or 'orchestrator' |
| action | TEXT | yes |  |  | 'llm-call' \| 'firecrawl' \| 'sql-mutation' \| 'skill-invoke' |
| target | TEXT |  |  |  | event_uid or URL or query |
| detail | TEXT |  |  |  | first 200 chars of prompt/URL |
| duration_ms | INTEGER |  |  |  |  |
| ok | INTEGER |  |  |  | 0 or 1 |
| error | TEXT |  |  |  |  |

Index `idx_activity_ts`: (ts)

Index `idx_activity_worker_target`: (worker, target)

## agent_state

| column | type | not null | default | pk | note |
|---|---|---|---|---|---|
| id | INTEGER |  |  | 1 |  |
| phase | TEXT | yes | 'bootstrapping' |  |  |
| entered_at | TEXT | yes |  |  |  |
| last_tick_at | TEXT |  |  |  |  |
| tick_count | INTEGER | yes | 0 |  |  |
| phase_ticks | INTEGER | yes | 0 |  |  |
| quiet_ticks | INTEGER | yes | 0 |  |  |
| notes | TEXT |  |  |  |  |

## analyst_runs

| column | type | not null | default | pk | note |
|---|---|---|---|---|---|
| id | INTEGER |  |  | 1 |  |
| started_at | TEXT | yes |  |  |  |
| ended_at | TEXT |  |  |  |  |
| status | TEXT | yes | 'running' |  |  |
| budget_json | TEXT | yes |  |  |  |
| used_json | TEXT |  |  |  |  |
| report_path | TEXT |  |  |  |  |
| backup_json | TEXT |  |  |  |  |
| notes | TEXT |  |  |  |  |

## autonomy_state

| column | type | not null | default | pk | note |
|---|---|---|---|---|---|
| key | TEXT |  |  | 1 |  |
| value | TEXT |  |  |  |  |
| updated_at | TEXT | yes |  |  |  |

Unique `sqlite_autoindex_autonomy_state_1`: (key)

## businesses

| column | type | not null | default | pk | note |
|---|---|---|---|---|---|
| entity_uid | TEXT |  |  | 1 |  |
| source | TEXT | yes |  |  |  |
| source_kind | TEXT | yes |  |  |  |
| name | TEXT | yes |  |  |  |
| description | TEXT |  |  |  |  |
| address | TEXT |  |  |  |  |
| website | TEXT |  |  |  |  |
| phone | TEXT |  |  |  |  |
| category_slug | TEXT |  |  |  |  |
| source_url | TEXT |  |  |  |  |
| extra_json | TEXT |  |  |  |  |
| first_seen_at | TEXT | yes |  |  |  |
| last_seen_at | TEXT | yes |  |  |  |
| content_hash | TEXT | yes |  |  |  |
| last_modified_at | TEXT | yes |  |  |  |
| pushed_at | TEXT |  |  |  |  |
| pushed_hash | TEXT |  |  |  |  |
| townloops_slug | TEXT |  |  |  |  |
| enrichment_status | TEXT | yes | 'pending' |  |  |
| enriched_at | TEXT |  |  |  |  |
| image_path | TEXT |  |  |  |  |
| image_alt | TEXT |  |  |  |  |
| instagram | TEXT |  |  |  |  |
| facebook | TEXT |  |  |  |  |
| tiktok | TEXT |  |  |  |  |
| twitter | TEXT |  |  |  |  |
| youtube | TEXT |  |  |  |  |
| linkedin | TEXT |  |  |  |  |
| last_harvested_at | TEXT |  |  |  |  |
| derived_from_event_uid | TEXT |  |  |  |  |
| derivation_confidence | INTEGER |  |  |  |  |
| linked_event_count | INTEGER | yes | 0 |  |  |
| ingest_ref | TEXT |  |  |  |  |

Index `idx_businesses_enrich`: (enrichment_status)

Index `idx_businesses_source`: (source)

Index `idx_businesses_unpushed`: (pushed_at)

Unique `sqlite_autoindex_businesses_1`: (entity_uid)

## chat_messages

| column | type | not null | default | pk | note |
|---|---|---|---|---|---|
| message_id | INTEGER |  |  | 1 |  |
| thread_id | TEXT | yes |  |  | 'default' in v1 (single operator) |
| hermes_session_id | TEXT |  |  |  | X-Hermes-Session-Id per thread |
| ts | TEXT | yes |  |  | ISO UTC, seconds |
| role | TEXT | yes |  |  | 'operator' \| 'agent' \| 'system' \| 'tool' |
| body | TEXT | yes |  |  |  |
| hermes_run_id | TEXT |  |  |  | when SSE /v1/runs is used (v2) |
| tool_call_json | TEXT |  |  |  | Hermes surfacing a proposed action |
| status | TEXT | yes | 'delivered' |  |  |
| error | TEXT |  |  |  |  |

Index `ix_chat_messages_thread_id`: (thread_id, message_id)

## chat_read_state

| column | type | not null | default | pk | note |
|---|---|---|---|---|---|
| thread_id | TEXT |  |  | 1 |  |
| last_seen_message_id | INTEGER | yes |  |  |  |

Unique `sqlite_autoindex_chat_read_state_1`: (thread_id)

## communities

| column | type | not null | default | pk | note |
|---|---|---|---|---|---|
| entity_uid | TEXT |  |  | 1 |  |
| source | TEXT | yes |  |  |  |
| source_kind | TEXT | yes |  |  |  |
| name | TEXT | yes |  |  |  |
| kind | TEXT |  |  |  | Townloops community.kind (default 'group') |
| description | TEXT |  |  |  |  |
| website | TEXT |  |  |  |  |
| meeting_info | TEXT |  |  |  |  |
| category_slug | TEXT |  |  |  |  |
| source_url | TEXT |  |  |  |  |
| extra_json | TEXT |  |  |  |  |
| first_seen_at | TEXT | yes |  |  |  |
| last_seen_at | TEXT | yes |  |  |  |
| content_hash | TEXT | yes |  |  |  |
| last_modified_at | TEXT | yes |  |  |  |
| pushed_at | TEXT |  |  |  |  |
| pushed_hash | TEXT |  |  |  |  |
| townloops_slug | TEXT |  |  |  |  |
| image_path | TEXT |  |  |  |  |
| image_alt | TEXT |  |  |  |  |
| instagram | TEXT |  |  |  |  |
| facebook | TEXT |  |  |  |  |
| tiktok | TEXT |  |  |  |  |
| twitter | TEXT |  |  |  |  |
| youtube | TEXT |  |  |  |  |
| linkedin | TEXT |  |  |  |  |
| last_harvested_at | TEXT |  |  |  |  |
| derived_from_event_uid | TEXT |  |  |  |  |
| derivation_confidence | INTEGER |  |  |  |  |
| linked_event_count | INTEGER | yes | 0 |  |  |
| enrichment_status | TEXT | yes | 'pending' |  |  |
| enriched_at | TEXT |  |  |  |  |
| ingest_ref | TEXT |  |  |  |  |

Index `idx_communities_enrich`: (enrichment_status)

Index `idx_communities_source`: (source)

Index `idx_communities_unpushed`: (pushed_at)

Unique `sqlite_autoindex_communities_1`: (entity_uid)

## creators

| column | type | not null | default | pk | note |
|---|---|---|---|---|---|
| entity_uid | TEXT |  |  | 1 |  |
| source | TEXT | yes |  |  |  |
| source_kind | TEXT | yes |  |  |  |
| name | TEXT | yes |  |  |  |
| kind | TEXT |  |  |  | Townloops creator.kind (default 'other') |
| bio | TEXT |  |  |  |  |
| website | TEXT |  |  |  |  |
| instagram | TEXT |  |  |  |  |
| youtube_url | TEXT |  |  |  |  |
| image_path | TEXT |  |  |  |  |
| image_alt | TEXT |  |  |  |  |
| source_url | TEXT |  |  |  |  |
| extra_json | TEXT |  |  |  |  |
| first_seen_at | TEXT | yes |  |  |  |
| last_seen_at | TEXT | yes |  |  |  |
| content_hash | TEXT | yes |  |  |  |
| last_modified_at | TEXT | yes |  |  |  |
| pushed_at | TEXT |  |  |  |  |
| pushed_hash | TEXT |  |  |  |  |
| townloops_slug | TEXT |  |  |  |  |
| facebook | TEXT |  |  |  |  |
| tiktok | TEXT |  |  |  |  |
| twitter | TEXT |  |  |  |  |
| linkedin | TEXT |  |  |  |  |
| derived_from_event_uid | TEXT |  |  |  |  |
| derivation_confidence | INTEGER |  |  |  |  |
| linked_event_count | INTEGER | yes | 0 |  |  |
| enrichment_status | TEXT | yes | 'pending' |  |  |
| enriched_at | TEXT |  |  |  |  |
| ingest_ref | TEXT |  |  |  |  |

Index `idx_creators_enrich`: (enrichment_status)

Index `idx_creators_source`: (source)

Index `idx_creators_unpushed`: (pushed_at)

Unique `sqlite_autoindex_creators_1`: (entity_uid)

## discovery_candidates

| column | type | not null | default | pk | note |
|---|---|---|---|---|---|
| candidate_uid | TEXT |  |  | 1 |  |
| town_slug | TEXT | yes |  |  |  |
| kind | TEXT | yes |  |  |  |
| target_category_slug | TEXT |  |  |  |  |
| canonical_url | TEXT | yes |  |  |  |
| display_name | TEXT |  |  |  |  |
| status | TEXT | yes | 'new' |  |  |
| source_signal | TEXT |  |  |  |  |
| reject_reason | TEXT |  |  |  |  |
| first_seen_at | TEXT | yes |  |  |  |
| last_seen_at | TEXT | yes |  |  |  |
| verify_json | TEXT |  |  |  |  |
| research_entity_uid | TEXT |  |  |  |  |
| live_slug | TEXT |  |  |  |  |
| promote_attempts | INTEGER | yes | 0 |  |  |
| promote_error | TEXT |  |  |  |  |

Index `idx_discovery_new`: (kind, first_seen_at)

Index `idx_discovery_status`: (status, kind)

Index `idx_discovery_verified`: (kind, first_seen_at)

Unique `sqlite_autoindex_discovery_candidates_1`: (candidate_uid)

Unique `sqlite_autoindex_discovery_candidates_2`: (town_slug, canonical_url, kind)

## enrichment_queue

| column | type | not null | default | pk | note |
|---|---|---|---|---|---|
| id | INTEGER |  |  | 1 |  |
| kind | TEXT | yes |  |  | 'venue' \| 'business' \| 'performer' |
| entity_uid | TEXT | yes |  |  |  |
| queued_at | TEXT | yes |  |  |  |
| claimed_by | TEXT |  |  |  |  |
| claimed_at | TEXT |  |  |  |  |
| attempts | INTEGER | yes | 0 |  |  |
| last_error | TEXT |  |  |  |  |
| resolved_at | TEXT |  |  |  |  |

Index `idx_queue_unclaimed`: (kind, queued_at)

Unique `sqlite_autoindex_enrichment_queue_1`: (kind, entity_uid)

## event_seeds

| column | type | not null | default | pk | note |
|---|---|---|---|---|---|
| seed_id | INTEGER |  |  | 1 |  |
| name | TEXT | yes |  |  |  |
| url | TEXT | yes |  |  |  |
| kind | TEXT | yes |  |  | 'chamber' \| 'government' \| 'tourism' \| 'library' \| 'parks' \| 'newspaper' \| 'venue' \| 'other' |
| status | TEXT | yes | 'proposed' |  | 'proposed' \| 'approved' \| 'rejected' |
| discovered_by | TEXT | yes | 'llm' |  | 'llm' \| 'operator' |
| trust_auto_approve | INTEGER | yes | 0 |  | 0/1 — approve events from this source without operator gate (still runs confidence filters) |
| trust_up_to_events_per_day | INTEGER | yes | 20 |  |  |
| last_scraped_at | TEXT |  |  |  |  |
| last_event_count | INTEGER |  |  |  |  |
| consecutive_empty_scrapes | INTEGER | yes | 0 |  |  |
| notes | TEXT |  |  |  |  |
| first_seen_at | TEXT | yes |  |  |  |
| last_seen_at | TEXT | yes |  |  |  |
| source_role | TEXT |  |  |  |  |
| feed_type | TEXT |  |  |  |  |
| feed_url | TEXT |  |  |  |  |
| feed_detail | TEXT |  |  |  |  |
| feed_probed_at | TEXT |  |  |  |  |
| health_status | TEXT |  |  |  |  |
| health_note | TEXT |  |  |  |  |
| health_baseline | REAL |  |  |  |  |
| health_flagged_at | TEXT |  |  |  |  |
| health_since | TEXT |  |  |  |  |
| trust_score | REAL |  |  |  |  |
| status_note | TEXT |  |  |  |  |
| status_changed_at | TEXT |  |  |  |  |
| previous_url | TEXT |  |  |  |  |
| trial_at | TEXT |  |  |  |  |
| adapter | TEXT |  |  |  |  |
| adapter_config | TEXT |  |  |  |  |
| hold_reason | TEXT |  |  |  |  |

Index `idx_seeds_last_scraped`: (last_scraped_at)

Index `idx_seeds_status`: (status)

Unique `sqlite_autoindex_event_seeds_1`: (url)

## event_workqueue

| column | type | not null | default | pk | note |
|---|---|---|---|---|---|
| queue_id | INTEGER |  |  | 1 |  |
| event_uid | TEXT | yes |  |  | FK to events.event_uid |
| next_action | TEXT | yes |  |  | 'extract-metadata' \| 'extract-venue' \| 'extract-hosts' \| 'extract-people' \| 'enrich-entities' \| 'ship' |
| attempts | INTEGER | yes | 0 |  |  |
| last_error | TEXT |  |  |  |  |
| last_attempt_at | TEXT |  |  |  |  |
| enqueued_at | TEXT | yes |  |  |  |
| claimed_by | TEXT |  |  |  |  |
| claimed_at | TEXT |  |  |  |  |
| resolved_at | TEXT |  |  |  |  |

Index `idx_workqueue_event`: (event_uid)

Index `idx_workqueue_pending`: (next_action, enqueued_at)

Unique `sqlite_autoindex_event_workqueue_1`: (event_uid, next_action)

## events

| column | type | not null | default | pk | note |
|---|---|---|---|---|---|
| event_uid | TEXT |  |  | 1 |  |
| source | TEXT | yes |  |  |  |
| source_kind | TEXT | yes |  |  |  |
| name | TEXT | yes |  |  |  |
| location | TEXT |  |  |  |  |
| date | TEXT | yes |  |  |  |
| date_day | TEXT | yes |  |  |  |
| description | TEXT |  |  |  |  |
| hosting_group | TEXT |  |  |  |  |
| source_url | TEXT |  |  |  |  |
| extra_json | TEXT |  |  |  |  |
| first_seen_at | TEXT | yes |  |  |  |
| last_seen_at | TEXT | yes |  |  |  |
| content_hash | TEXT | yes |  |  |  |
| last_modified_at | TEXT | yes |  |  |  |
| pushed_at | TEXT |  |  |  |  |
| pushed_hash | TEXT |  |  |  |  |
| status | TEXT | yes | 'active' |  |  |
| venue_name | TEXT |  |  |  |  |
| end_at | TEXT |  |  |  |  |
| category_slug | TEXT |  |  |  |  |
| townloops_slug | TEXT |  |  |  |  |
| hidden | INTEGER | yes | 0 |  |  |
| approval_status | TEXT | yes | 'pending' |  |  |
| approval_note | TEXT |  |  |  |  |
| approval_at | TEXT |  |  |  |  |
| state | TEXT | yes | 'found' |  |  |
| state_updated_at | TEXT |  |  |  |  |
| canonical_event_uid | TEXT |  |  |  |  |
| metadata_confidence | INTEGER |  |  |  |  |
| in_town_confidence | INTEGER |  |  |  |  |
| discovered_via | TEXT |  |  |  |  |
| title | TEXT |  |  |  |  |
| venue_raw | TEXT |  |  |  |  |
| location_raw | TEXT |  |  |  |  |
| image_url | TEXT |  |  |  |  |
| image_local_path | TEXT |  |  |  |  |
| price | TEXT |  |  |  |  |
| rsvp_url | TEXT |  |  |  |  |
| tags_json | TEXT |  |  |  |  |
| date_utc | TEXT |  |  |  |  |
| date_local | TEXT |  |  |  |  |
| end_at_utc | TEXT |  |  |  |  |
| end_at_local | TEXT |  |  |  |  |
| derived_venue_uid | TEXT |  |  |  |  |
| derived_host_uid | TEXT |  |  |  |  |
| derived_host_kind | TEXT |  |  |  |  |
| category_slugs | TEXT |  |  |  |  |
| tags | TEXT |  |  |  |  |
| missed_finds | INTEGER | yes | 0 |  |  |
| cancel_status | TEXT |  |  |  |  |
| cancel_flagged_at | TEXT |  |  |  |  |
| cancel_note | TEXT |  |  |  |  |
| accessibility_notes | TEXT |  |  |  |  |
| detail_read_status | TEXT |  |  |  |  |
| detail_read_url | TEXT |  |  |  |  |
| detail_read_at | TEXT |  |  |  |  |
| series_slug | TEXT |  |  |  |  |
| series_id | INTEGER |  |  |  |  |
| ingest_ref | TEXT |  |  |  |  |

Index `idx_events_approval`: (approval_status)

Index `idx_events_cancel`: (cancel_status)

Index `idx_events_canonical`: (canonical_event_uid)

Index `idx_events_date_day`: (date_day)

Index `idx_events_series_slug`: (series_slug)

Index `idx_events_source`: (source)

Index `idx_events_state`: (state)

Index `idx_events_unpushed`: (pushed_at)

Index `idx_events_visible`: (date_day)

Unique `sqlite_autoindex_events_1`: (event_uid)

## job_health

| column | type | not null | default | pk | note |
|---|---|---|---|---|---|
| job | TEXT |  |  | 1 |  |
| last_run_at | TEXT | yes |  |  |  |
| last_outcome | TEXT | yes |  |  |  |
| streak | INTEGER | yes | 0 |  |  |
| detail | TEXT |  |  |  |  |
| alerted_at | TEXT |  |  |  |  |

Unique `sqlite_autoindex_job_health_1`: (job)

## linked_events

| column | type | not null | default | pk | note |
|---|---|---|---|---|---|
| link_id | INTEGER |  |  | 1 |  |
| event_uid | TEXT | yes |  |  |  |
| entity_kind | TEXT | yes |  |  | 'business' \| 'community' \| 'venue' \| 'performer' \| 'creator' |
| entity_uid | TEXT | yes |  |  |  |
| role | TEXT | yes |  |  | 'host' \| 'venue' \| 'performer' \| 'creator' \| 'sponsor' |
| linked_at | TEXT | yes |  |  |  |

Index `idx_linked_entity`: (entity_kind, entity_uid)

Index `idx_linked_event`: (event_uid)

Unique `sqlite_autoindex_linked_events_1`: (event_uid, entity_kind, entity_uid, role)

## listing_website_clears

| column | type | not null | default | pk | note |
|---|---|---|---|---|---|
| target | TEXT |  |  | 1 |  |
| kind | TEXT | yes |  |  |  |
| entity_uid | TEXT |  |  |  |  |
| live_slug | TEXT |  |  |  |  |
| url | TEXT |  |  |  |  |
| status | TEXT | yes |  |  |  |
| changed | TEXT |  |  |  |  |
| pending_slug | TEXT |  |  |  |  |
| reenrich | TEXT |  |  |  |  |
| note | TEXT |  |  |  |  |
| created_at | TEXT | yes |  |  |  |
| updated_at | TEXT | yes |  |  |  |

Index `idx_listing_website_clears_pending`: (pending_slug)

Index `idx_listing_website_clears_status`: (status)

Unique `sqlite_autoindex_listing_website_clears_1`: (target)

## newsletters

| column | type | not null | default | pk | note |
|---|---|---|---|---|---|
| iso_week | TEXT |  |  | 1 | '2026-W28' |
| composed_at | TEXT |  |  |  |  |
| event_count | INTEGER |  |  |  |  |
| body_path | TEXT |  |  |  | state/newsletters/<iso-week>.html |
| pushed_at | TEXT |  |  |  |  |
| campaign_id | INTEGER |  |  |  |  |
| scheduled_send_at | TEXT |  |  |  |  |
| status | TEXT | yes | 'draft' |  | 'draft' \| 'scheduled' \| 'sent' \| 'failed' |

Unique `sqlite_autoindex_newsletters_1`: (iso_week)

## performers

| column | type | not null | default | pk | note |
|---|---|---|---|---|---|
| entity_uid | TEXT |  |  | 1 |  |
| source | TEXT | yes |  |  |  |
| source_kind | TEXT | yes |  |  |  |
| name | TEXT | yes |  |  |  |
| kind | TEXT |  |  |  | Townloops performer.kind (default 'band') |
| description | TEXT |  |  |  |  |
| website | TEXT |  |  |  |  |
| instagram | TEXT |  |  |  |  |
| spotify_url | TEXT |  |  |  |  |
| image_path | TEXT |  |  |  |  |
| image_alt | TEXT |  |  |  |  |
| source_url | TEXT |  |  |  |  |
| extra_json | TEXT |  |  |  |  |
| first_seen_at | TEXT | yes |  |  |  |
| last_seen_at | TEXT | yes |  |  |  |
| content_hash | TEXT | yes |  |  |  |
| last_modified_at | TEXT | yes |  |  |  |
| pushed_at | TEXT |  |  |  |  |
| pushed_hash | TEXT |  |  |  |  |
| townloops_slug | TEXT |  |  |  |  |
| enrichment_status | TEXT | yes | 'pending' |  |  |
| enriched_at | TEXT |  |  |  |  |
| facebook | TEXT |  |  |  |  |
| tiktok | TEXT |  |  |  |  |
| twitter | TEXT |  |  |  |  |
| youtube | TEXT |  |  |  |  |
| linkedin | TEXT |  |  |  |  |
| derived_from_event_uid | TEXT |  |  |  |  |
| derivation_confidence | INTEGER |  |  |  |  |
| linked_event_count | INTEGER | yes | 0 |  |  |
| ingest_ref | TEXT |  |  |  |  |

Index `idx_performers_enrich`: (enrichment_status)

Index `idx_performers_source`: (source)

Index `idx_performers_unpushed`: (pushed_at)

Unique `sqlite_autoindex_performers_1`: (entity_uid)

## post_counters

| column | type | not null | default | pk | note |
|---|---|---|---|---|---|
| day | TEXT | yes |  | 1 |  |
| resource | TEXT | yes |  | 2 |  |
| count | INTEGER | yes | 0 |  |  |

Index `idx_post_counters_day`: (day)

Unique `sqlite_autoindex_post_counters_1`: (day, resource)

## schema_version

| column | type | not null | default | pk | note |
|---|---|---|---|---|---|
| filename | TEXT |  |  | 1 |  |
| applied_at | TEXT | yes |  |  |  |

Unique `sqlite_autoindex_schema_version_1`: (filename)

## source_runs

| column | type | not null | default | pk | note |
|---|---|---|---|---|---|
| id | INTEGER |  |  | 1 |  |
| source | TEXT | yes |  |  |  |
| started_at | TEXT | yes |  |  |  |
| ended_at | TEXT |  |  |  |  |
| ok | INTEGER |  |  |  |  |
| fetched | INTEGER |  |  |  |  |
| inserted | INTEGER |  |  |  |  |
| updated | INTEGER |  |  |  |  |
| error | TEXT |  |  |  |  |
| entity_counts_json | TEXT |  |  |  |  |

Index `idx_source_runs_source`: (source, started_at)

## sponsors

| column | type | not null | default | pk | note |
|---|---|---|---|---|---|
| sponsor_uid | TEXT |  |  | 1 | sha1(name_norm) |
| name | TEXT | yes |  |  |  |
| kind | TEXT | yes |  |  | 'business' \| 'community' |
| logo_url | TEXT |  |  |  |  |
| website | TEXT |  |  |  |  |
| description | TEXT |  |  |  |  |
| contact_email | TEXT |  |  |  |  |
| tier | TEXT |  |  |  | 'gold' \| 'silver' \| 'bronze' \| 'community' |
| starts_at | TEXT |  |  |  |  |
| ends_at | TEXT |  |  |  |  |
| is_active | INTEGER | yes | 1 |  |  |
| content_hash | TEXT | yes |  |  |  |
| townloops_slug | TEXT |  |  |  |  |
| pushed_at | TEXT |  |  |  |  |
| pushed_hash | TEXT |  |  |  |  |
| image_alt | TEXT |  |  |  |  |

Unique `sqlite_autoindex_sponsors_1`: (sponsor_uid)

## submissions

| column | type | not null | default | pk | note |
|---|---|---|---|---|---|
| id | INTEGER |  |  | 1 |  |
| resource | TEXT | yes |  |  |  |
| slug | TEXT | yes |  |  |  |
| local_uid | TEXT |  |  |  |  |
| kind | TEXT | yes | 'new' |  |  |
| target_slug | TEXT |  |  |  |  |
| seed | TEXT |  |  |  |  |
| series_slug | TEXT |  |  |  |  |
| discovery_source | TEXT |  |  |  |  |
| status | TEXT | yes | 'pending' |  |  |
| published_unreviewed | INTEGER | yes | 0 |  |  |
| submitted_at | TEXT | yes |  |  |  |
| checked_at | TEXT |  |  |  |  |
| decided_at | TEXT |  |  |  |  |
| surviving_slug | TEXT |  |  |  |  |

Index `idx_submissions_seed`: (seed)

Index `idx_submissions_series`: (series_slug)

Index `idx_submissions_status`: (status, checked_at)

Unique `sqlite_autoindex_submissions_1`: (resource, slug)

## venues

| column | type | not null | default | pk | note |
|---|---|---|---|---|---|
| entity_uid | TEXT |  |  | 1 |  |
| source | TEXT | yes |  |  |  |
| source_kind | TEXT | yes |  |  | 'mined' \| 'manual' |
| name | TEXT | yes |  |  |  |
| description | TEXT |  |  |  |  |
| address | TEXT |  |  |  |  |
| website | TEXT |  |  |  |  |
| phone | TEXT |  |  |  |  |
| accessibility_notes | TEXT |  |  |  |  |
| image_path | TEXT |  |  |  |  |
| image_alt | TEXT |  |  |  |  |
| category_slug | TEXT |  |  |  |  |
| source_url | TEXT |  |  |  |  |
| extra_json | TEXT |  |  |  |  |
| first_seen_at | TEXT | yes |  |  |  |
| last_seen_at | TEXT | yes |  |  |  |
| content_hash | TEXT | yes |  |  |  |
| last_modified_at | TEXT | yes |  |  |  |
| pushed_at | TEXT |  |  |  |  |
| pushed_hash | TEXT |  |  |  |  |
| townloops_slug | TEXT |  |  |  |  |
| enrichment_status | TEXT | yes | 'pending' |  |  |
| enriched_at | TEXT |  |  |  |  |
| last_harvested_at | TEXT |  |  |  |  |
| derived_from_event_uid | TEXT |  |  |  |  |
| derivation_confidence | INTEGER |  |  |  |  |
| linked_event_count | INTEGER | yes | 0 |  |  |
| ingest_ref | TEXT |  |  |  |  |

Index `idx_venues_enrich`: (enrichment_status)

Index `idx_venues_source`: (source)

Index `idx_venues_unpushed`: (pushed_at)

Unique `sqlite_autoindex_venues_1`: (entity_uid)
