-- Raw customer status is a source-provided lifecycle label, not a CDP enum.
-- Preserve all source values; downstream validation may flag unknown labels.
-- Apply in a bounded ingestion/stitching maintenance window; existing source
-- identity and non-status integrity constraints remain enforced.
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';
DO $gate$
DECLARE rel regclass; definition text; status_type text;
BEGIN
    FOREACH rel IN ARRAY ARRAY['raw.customer_current'::regclass, 'raw.customer'::regclass] LOOP
        SELECT format_type(a.atttypid, a.atttypmod) INTO status_type
        FROM pg_attribute a WHERE a.attrelid = rel AND a.attname = 'status' AND NOT a.attisdropped;
        IF status_type <> 'text' THEN
            RAISE EXCEPTION 'customer status type drift on %', rel;
        END IF;
        SELECT pg_get_constraintdef(c.oid) INTO definition
        FROM pg_constraint c WHERE c.conrelid = rel AND c.conname = 'customer_status_check';
        IF definition NOT IN (
            'CHECK ((status = ANY (ARRAY[''ACTIVE''::text, ''INACTIVE''::text, ''BLOCKED''::text, ''DELETED''::text, ''SUSPENDED''::text])))',
            'CHECK ((status = ANY (ARRAY[''ACTIVE''::text, ''INACTIVE''::text, ''BLOCKED''::text, ''DELETED''::text, ''SUSPENDED''::text, ''PENDING''::text]))) NOT VALID'
        ) THEN
            RAISE EXCEPTION 'customer status constraint drift on %', rel;
        END IF;
    END LOOP;
END $gate$;
ALTER TABLE raw.customer_current DROP CONSTRAINT customer_status_check;
ALTER TABLE raw.customer DROP CONSTRAINT customer_status_check;
COMMENT ON COLUMN raw.customer_current.status IS
    'Open source-provided customer lifecycle label; unknown values are preserved and assessed by data quality.';
COMMENT ON COLUMN raw.customer.status IS
    'Immutable observation of the source-provided customer lifecycle label; no ingestion enum.';
COMMIT;
