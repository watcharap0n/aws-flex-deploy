-- Staging-only physical customer profile table. Column names and types match
-- the current production cdp_customer.customer_profile query contract.
-- The production object is a view over separate publication layers.
CREATE DATABASE IF NOT EXISTS cdp_customer ENGINE = Atomic;

CREATE TABLE IF NOT EXISTS cdp_customer.customer_profile_stage
(
    standardized_record_id UUID,
    app_id String,
    source_system_id Int64,
    tenant_key String,
    customer_id String,
    customer_key_id Nullable(Int64),
    profile_source_record_id Nullable(Int64),
    external_id Nullable(String),
    first_name Nullable(String),
    middle_name Nullable(String),
    last_name Nullable(String),
    full_name Nullable(String),
    nickname Nullable(String),
    email Nullable(String),
    phone Nullable(String),
    gender Nullable(String),
    date_of_birth_raw Nullable(String),
    date_of_birth Nullable(Date32),
    member_code Nullable(String),
    customer_type Nullable(String),
    status Nullable(String),
    language Nullable(String),
    address_line_1 Nullable(String),
    address_line_2 Nullable(String),
    district Nullable(String),
    province Nullable(String),
    postal_code Nullable(String),
    country_code Nullable(String),
    external_ids String,
    attributes String,
    source_created_at Nullable(DateTime64(6, 'UTC')),
    source_updated_at Nullable(DateTime64(6, 'UTC')),
    profile_updated_at DateTime64(6, 'UTC'),
    record_fingerprint FixedString(64),
    identity_eligible Bool,
    quality_flags Array(String),
    standardized_at DateTime64(6, 'UTC'),
    first_seen_at DateTime64(6, 'UTC'),
    last_seen_at DateTime64(6, 'UTC'),
    customer_entity_id Nullable(String),
    resolution_run_id Nullable(UUID),
    workspace_id Nullable(UUID),
    transfer_id Nullable(UUID),
    publication_seq Nullable(UInt64)
)
ENGINE = ReplacingMergeTree(standardized_at)
ORDER BY (app_id, source_system_id, tenant_key, customer_id);

CREATE VIEW IF NOT EXISTS cdp_customer.customer_profile AS
SELECT * EXCEPT (standardized_record_id)
FROM cdp_customer.customer_profile_stage FINAL;
