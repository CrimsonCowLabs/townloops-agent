-- 027_seed_adapters.sql
-- Deterministic seed adapters (engine/seed_adapters.py).
--
-- adapter: how event-find reads the seed — NULL (or 'generic') is the
-- Firecrawl markdown → LLM path; otherwise a key of
-- engine.seed_adapters.ADAPTERS ('tribe', ...).
-- adapter_config: JSON object of that adapter's settings (e.g. the Tribe
-- feed `url`, `days_ahead`), NULL for none.

ALTER TABLE event_seeds ADD COLUMN adapter TEXT;
ALTER TABLE event_seeds ADD COLUMN adapter_config TEXT;
