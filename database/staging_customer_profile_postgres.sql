-- Staging-only delivery ledger for standardized customer profiles.
CREATE TABLE IF NOT EXISTS control.clickhouse_customer_profile_mirror
(
    standardized_record_id uuid PRIMARY KEY,
    record_fingerprint text NOT NULL,
    published_at timestamptz NOT NULL DEFAULT now()
);
