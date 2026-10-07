-- 020_discovery_promote_attempts.sql
-- The business promote run takes operator-approved candidates fewest
-- attempts first, so a candidate whose site never reads or whose ship keeps
-- failing can't hold the front of the queue. promote_error is the last
-- failure, for the operator.

ALTER TABLE discovery_candidates ADD COLUMN promote_attempts INTEGER NOT NULL DEFAULT 0;
ALTER TABLE discovery_candidates ADD COLUMN promote_error TEXT;
