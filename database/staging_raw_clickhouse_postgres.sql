-- Staging-only delivery ledger. A failed ClickHouse write leaves rows eligible
-- for retry; a retry after an uncertain commit is deduplicated by snapshot_id.
CREATE TABLE IF NOT EXISTS control.clickhouse_raw_mirror
(
    snapshot_id uuid PRIMARY KEY,
    published_at timestamptz NOT NULL DEFAULT now()
);
