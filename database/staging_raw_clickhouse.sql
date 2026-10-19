-- Staging-only raw mirror. The production ClickHouse customer tables are
-- downstream of standardization and identity publication.
CREATE DATABASE IF NOT EXISTS staging ENGINE = Atomic;

CREATE TABLE IF NOT EXISTS staging.raw_customer
(
    snapshot_id UUID,
    batch_id UUID,
    raw_customer_id UUID,
    source_system_id Int64,
    source_customer_id String,
    app_id Nullable(String),
    status String,
    raw_payload String,
    ingested_at DateTime64(6, 'UTC'),
    snapshot_recorded_at DateTime64(6, 'UTC')
)
ENGINE = ReplacingMergeTree(snapshot_recorded_at)
ORDER BY snapshot_id;
