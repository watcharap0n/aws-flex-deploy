-- Portable synthetic pre-019 contract. No prototype or external catalog dependency.
DROP SCHEMA IF EXISTS quality CASCADE;
DROP SCHEMA IF EXISTS raw CASCADE;
DROP SCHEMA IF EXISTS staging CASCADE;
DROP SCHEMA IF EXISTS control CASCADE;
CREATE SCHEMA raw; CREATE SCHEMA staging; CREATE SCHEMA control;
CREATE TABLE control.source_landing_batch (
 batch_id uuid PRIMARY KEY, output_status text, output_bucket text, output_key text,
 output_sha256 text, output_row_count bigint
);
CREATE TABLE control.ingestion_batch (
 batch_id uuid PRIMARY KEY, landing_batch_id uuid REFERENCES control.source_landing_batch,
 source_system_id bigint, status text, records_loaded bigint, checksum text,
 object_name text, metadata jsonb DEFAULT '{}'::jsonb, created_at timestamptz DEFAULT now()
);
CREATE TABLE raw.customer (
 raw_customer_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 batch_id uuid NOT NULL REFERENCES control.ingestion_batch,
 source_system_id bigint NOT NULL,
    source_customer_id text NOT NULL,
    external_id text,
    first_name text,
    middle_name text,
    last_name text,
    full_name text,
    nickname text,
    email text,
    phone text,
    address_line_1 text,
    address_line_2 text,
    district text,
    province text,
    postal_code text,
    country text,
    date_of_birth date,
    birthday date,
    age integer,
    gender text,
    line_user_id text,
    facebook_id text,
    google_id text,
    apple_id text,
    member_code text,
    customer_type text,
    status text,
    language text,
    source text,
    source_system text,
    external_ids jsonb,
    attributes jsonb,
    raw_payload jsonb,
    source_created_at timestamptz,
    source_updated_at timestamptz,
 created_at timestamptz DEFAULT now(), updated_at timestamptz DEFAULT now(), ingested_at timestamptz DEFAULT now(),
 UNIQUE(source_system_id,source_customer_id)
);
CREATE TABLE staging.customer_profile (
 standardized_record_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 standardized_at timestamptz DEFAULT now(),
    raw_customer_id uuid UNIQUE REFERENCES raw.customer(raw_customer_id),
    batch_id uuid,
    source_system_id bigint,
    source_customer_id text,
    first_name_raw text,
    first_name_normalized text,
    middle_name_raw text,
    middle_name_normalized text,
    last_name_raw text,
    last_name_normalized text,
    full_name_raw text,
    full_name_normalized text,
    email_raw text,
    email_normalized text,
    email_domain text,
    email_is_valid boolean,
    phone_raw text,
    phone_normalized text,
    phone_country_code text,
    phone_is_valid boolean,
    address_raw text,
    address_normalized text,
    district_normalized text,
    province_normalized text,
    postal_code_normalized text,
    country_code text,
    date_of_birth date,
    gender_normalized text,
    national_id_hash text,
    device_ids_normalized text[],
    record_quality_score numeric,
    standardization_status text,
    attributes jsonb,
    source_created_at timestamptz,
    source_updated_at timestamptz,
    record_fingerprint text,
    normalization_version text,
 created_at timestamptz DEFAULT now(), updated_at timestamptz DEFAULT now()
);
CREATE TABLE raw.customer_batch_member (
 ingestion_batch_id uuid NOT NULL REFERENCES control.ingestion_batch,
 raw_customer_id uuid NOT NULL REFERENCES raw.customer,
 source_system_id bigint NOT NULL, source_customer_id text NOT NULL,
 PRIMARY KEY(ingestion_batch_id,raw_customer_id),
 UNIQUE(ingestion_batch_id,source_system_id,source_customer_id)
);
