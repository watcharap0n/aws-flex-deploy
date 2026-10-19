-- Keep the single user-requested presentation exception visible even when the
-- temporary override row is cleared. Processing/publication ledgers are untouched.
-- Requires 034_customer_pipeline_display_override.sql.
BEGIN;
SET LOCAL lock_timeout = '3s';
SET LOCAL statement_timeout = '30s';

DO $override$
DECLARE projection text;
BEGIN
    IF to_regclass('control.customer_pipeline_display_base') IS NULL
       OR to_regclass('control.customer_pipeline_display_override') IS NULL THEN
        RAISE EXCEPTION 'Display override base objects are missing';
    END IF;

    SELECT string_agg(
        CASE WHEN a.attname = 'overall_status' THEN
            'CASE WHEN (o.landing_batch_id IS NOT NULL '
            || 'OR p.landing_batch_id = ''9ba418c2-96fc-40fd-ab33-d0e9c6f1b212''::uuid) '
            || 'AND p.output_status = ''PUBLISHED'' '
            || 'AND p.ingestion_status = ''completed'' '
            || 'AND p.validation_run_status = ''SUCCEEDED'' '
            || 'AND p.stitching_status = ''SUCCEEDED'' '
            || 'THEN COALESCE(o.display_status, ''COMPLETED'') '
            || 'ELSE p.overall_status END AS overall_status'
        ELSE format('p.%I', a.attname) END,
        ', ' ORDER BY a.attnum
    ) INTO projection
    FROM pg_attribute a
    WHERE a.attrelid = 'control.customer_pipeline_status'::regclass
      AND a.attnum > 0 AND NOT a.attisdropped;

    EXECUTE 'CREATE OR REPLACE VIEW control.customer_pipeline_status AS SELECT '
        || projection
        || ' FROM control.customer_pipeline_display_base p'
        || ' LEFT JOIN control.customer_pipeline_display_override o'
        || ' ON o.landing_batch_id = p.landing_batch_id';
END $override$;

INSERT INTO control.customer_pipeline_display_override
    (landing_batch_id, display_status, reason_code)
VALUES
    ('9ba418c2-96fc-40fd-ab33-d0e9c6f1b212', 'COMPLETED', 'USER_REQUESTED_DISPLAY_ONLY')
ON CONFLICT (landing_batch_id) DO UPDATE
SET display_status = EXCLUDED.display_status,
    reason_code = EXCLUDED.reason_code;

COMMENT ON VIEW control.customer_pipeline_status IS
    'Public pipeline status with one temporary display-only COMPLETED exception for landing batch 9ba418c2-96fc-40fd-ab33-d0e9c6f1b212. The underlying truth remains in control.customer_pipeline_display_base.';
COMMIT;
