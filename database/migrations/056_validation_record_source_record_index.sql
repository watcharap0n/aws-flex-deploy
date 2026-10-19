-- FK quality.customer_validation_record.source_record_id -> raw.customer_landing_source
-- had no index led by source_record_id (uq_customer_validation_record_source leads
-- with validation_run_id). Every delete of landing rows seq-scanned validation_record
-- per row: removing 100k load-test rows took 10+ min. Partial like the other
-- nullable FK indexes; FK checks (source_record_id = $1) imply NOT NULL.
-- CONCURRENTLY: run outside a transaction block. Applied to prod 2026-10-11.
CREATE INDEX CONCURRENTLY IF NOT EXISTS ix_customer_validation_record_source_record
    ON quality.customer_validation_record (source_record_id)
    WHERE source_record_id IS NOT NULL;
