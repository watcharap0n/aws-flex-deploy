-- Temporary, display-only override for one customer upload.
-- Execution, resolution, transfer and publication ledgers remain authoritative.
-- Revert by restoring the saved base definition (see rollback note below).
BEGIN;
SET LOCAL lock_timeout = '3s';
SET LOCAL statement_timeout = '30s';

DO $preflight$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM control.customer_pipeline_status
        WHERE landing_batch_id = '9ba418c2-96fc-40fd-ab33-d0e9c6f1b212'
          AND overall_status = 'SCOPED_RESOLUTION_FAILED'
          AND output_status = 'PUBLISHED'
          AND ingestion_status = 'completed'
          AND validation_run_status = 'SUCCEEDED'
          AND stitching_status = 'SUCCEEDED'
    ) THEN
        RAISE EXCEPTION 'Target batch state changed; display override not applied';
    END IF;
END $preflight$;

CREATE TABLE control.customer_pipeline_display_override (
    landing_batch_id uuid PRIMARY KEY REFERENCES control.source_landing_batch(batch_id),
    display_status text NOT NULL CHECK (display_status = 'COMPLETED'),
    reason_code text NOT NULL CHECK (reason_code = 'USER_REQUESTED_DISPLAY_ONLY'),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

INSERT INTO control.customer_pipeline_display_override
    (landing_batch_id, display_status, reason_code)
VALUES
    ('9ba418c2-96fc-40fd-ab33-d0e9c6f1b212', 'COMPLETED', 'USER_REQUESTED_DISPLAY_ONLY');

DO $override$
DECLARE
    original_definition text;
    projection text;
BEGIN
    -- Snapshot the installed 032+ view, including its future-safe column order.
    original_definition := rtrim(
        pg_get_viewdef('control.customer_pipeline_status'::regclass, true),
        E';\n '
    );
    EXECUTE 'CREATE VIEW control.customer_pipeline_display_base AS '
        || original_definition;

    SELECT string_agg(
        CASE WHEN a.attname = 'overall_status' THEN
            'CASE WHEN o.landing_batch_id IS NOT NULL '
            || 'AND p.output_status = ''PUBLISHED'' '
            || 'AND p.ingestion_status = ''completed'' '
            || 'AND p.validation_run_status = ''SUCCEEDED'' '
            || 'AND p.stitching_status = ''SUCCEEDED'' '
            || 'THEN o.display_status ELSE p.overall_status END AS overall_status'
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

COMMENT ON TABLE control.customer_pipeline_display_override IS
    'One-batch presentation exception only. Never use as evidence of identity publication.';
COMMENT ON VIEW control.customer_pipeline_display_base IS
    'Unmodified pipeline/publication status; compare with the presentation view while the exception is active.';
COMMIT;

-- Rollback, after checking no later migration changed the public view:
-- BEGIN;
-- DO $rollback$ BEGIN
--   EXECUTE 'CREATE OR REPLACE VIEW control.customer_pipeline_status AS '
--       || pg_get_viewdef('control.customer_pipeline_display_base'::regclass, true);
-- END $rollback$;
-- DROP VIEW control.customer_pipeline_display_base;
-- DROP TABLE control.customer_pipeline_display_override;
-- COMMIT;
