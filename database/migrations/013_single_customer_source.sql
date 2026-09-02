-- Cut the landing contract over from a two-file join to one customer file.
-- Apply this migration before deploying either updated Lambda.

BEGIN;

CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE SCHEMA IF NOT EXISTS control;
CREATE SCHEMA IF NOT EXISTS raw;

-- Preserve legacy customer_ids/customer_profiles rows while allowing the new
-- single source type for every new batch.
ALTER TABLE control.source_landing_file
    DROP CONSTRAINT IF EXISTS source_landing_file_source_type_check;

ALTER TABLE control.source_landing_file
    ADD CONSTRAINT source_landing_file_source_type_check
    CHECK (source_type IN ('customers', 'customer_ids', 'customer_profiles'));

CREATE TABLE IF NOT EXISTS raw.customer_landing_source (
    source_record_id uuid DEFAULT gen_random_uuid() NOT NULL,
    batch_id uuid NOT NULL,
    file_id uuid NOT NULL,
    source_row_number integer NOT NULL,
    source_customer_id text NULL,
    raw_payload jsonb NOT NULL,
    validation_errors jsonb NULL,
    ingested_at timestamptz DEFAULT now() NOT NULL,
    CONSTRAINT customer_landing_source_pkey
        PRIMARY KEY (source_record_id),
    CONSTRAINT customer_landing_source_batch_fkey
        FOREIGN KEY (batch_id)
        REFERENCES control.source_landing_batch(batch_id),
    CONSTRAINT customer_landing_source_file_fkey
        FOREIGN KEY (batch_id, file_id)
        REFERENCES control.source_landing_file(batch_id, file_id),
    CONSTRAINT uq_customer_landing_source_file_row
        UNIQUE (file_id, source_row_number)
);

CREATE INDEX IF NOT EXISTS ix_customer_landing_source_batch_customer
    ON raw.customer_landing_source (batch_id, source_customer_id);

-- raw.customer is a source-scoped current record table. The canonical/master
-- customer identifier is created later in identity.customer_entity.
ALTER TABLE raw.customer
    ALTER COLUMN customer_id DROP NOT NULL;

ALTER TABLE raw.customer
    DROP CONSTRAINT IF EXISTS customer_customer_id_key;

CREATE UNIQUE INDEX IF NOT EXISTS uq_customer_source_customer_id
    ON raw.customer (source_system_id, source_customer_id);

-- Keep one date-of-birth representation for new writes. Stop safely if an
-- existing non-empty text value cannot be converted without data loss.
DO $migration$
DECLARE
    current_type text;
    invalid_dates bigint;
BEGIN
    SELECT data_type
    INTO current_type
    FROM information_schema.columns
    WHERE table_schema = 'raw'
      AND table_name = 'customer'
      AND column_name = 'date_of_birth';

    IF current_type IN ('text', 'character varying') THEN
        SELECT count(*)
        INTO invalid_dates
        FROM raw.customer
        WHERE date_of_birth IS NOT NULL
          AND btrim(date_of_birth) <> ''
          AND (
              date_of_birth !~ '^\d{4}-\d{2}-\d{2}$'
              OR to_char(to_date(date_of_birth, 'YYYY-MM-DD'), 'YYYY-MM-DD')
                    <> date_of_birth
          );

        IF invalid_dates > 0 THEN
            RAISE EXCEPTION
                'raw.customer contains % non-ISO date_of_birth value(s); migration stopped without changing the type',
                invalid_dates;
        END IF;

        ALTER TABLE raw.customer
            ALTER COLUMN date_of_birth TYPE date
            USING NULLIF(btrim(date_of_birth), '')::date;
    END IF;
END
$migration$;

COMMIT;
