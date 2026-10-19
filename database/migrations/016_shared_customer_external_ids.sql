-- raw.customer stores source records, not resolved identities. Different source
-- records may legitimately share an external identifier and must remain
-- available to the downstream stitcher.

BEGIN;

-- Fail quickly instead of waiting behind an active ingestion transaction.
SET LOCAL lock_timeout = '5s';

DROP INDEX IF EXISTS raw.uq_customer_source_external_id;

CREATE INDEX IF NOT EXISTS idx_raw_customer_source_external_id
    ON raw.customer (source_system_id, external_id)
    WHERE external_id IS NOT NULL;

COMMIT;
