-- A durable canonical ingestion job that fails terminally rolls its whole
-- transaction back, so no control.ingestion_batch row exists and the public
-- status kept showing INGESTION_PENDING forever (prod 2026-10-22, employees.csv:
-- the job had FAILED at 05:51 UTC, the screen still said "processing").
-- Show FAILED / INGESTION_FAILED for that case. Output columns are unchanged, so
-- control.customer_pipeline_status (built on this view) needs no change.
-- Idempotent: a second run only reports that the patch is present.
BEGIN;
SET LOCAL lock_timeout = '3s';
SET LOCAL statement_timeout = '60s';

CREATE INDEX IF NOT EXISTS customer_ingestion_job_canonical_key
    ON control.customer_ingestion_job (object_key)
    WHERE stage = 'canonical';

DO $failure$
DECLARE
    definition text := pg_get_viewdef('control.customer_pipeline_display_base'::regclass, false);
    failed_job constant text :=
        '((ib.batch_id IS NULL) AND (EXISTS ( SELECT 1 FROM control.customer_ingestion_job failed_job '
        || 'WHERE ((failed_job.stage = ''canonical''::text) AND (failed_job.status = ''FAILED''::text) '
        || 'AND (failed_job.object_key = (slb.output_key)::text)))))';
    overall_anchor constant text :=
        'OR ((vr.run_status)::text = ''FAILED''::text)) THEN ''FAILED''::text';
    code_anchor constant text :=
        'WHEN (((slb.output_status)::text = ''FAILED''::text) OR ((slb.status)::text = ''INVALID''::text)) '
        || 'THEN ''LANDING_FAILED''::text';
    message_anchor constant text :=
        'WHEN (((slb.output_status)::text = ''FAILED''::text) OR ((slb.status)::text = ''INVALID''::text)) '
        || 'THEN ''Customer landing failed''::text';
    anchor text;
BEGIN
    IF position('customer_ingestion_job failed_job' IN definition) > 0 THEN
        RAISE NOTICE 'customer ingestion failure visibility already present';
        RETURN;
    END IF;
    FOREACH anchor IN ARRAY ARRAY[overall_anchor, code_anchor, message_anchor] LOOP
        IF (length(definition) - length(replace(definition, anchor, ''))) / length(anchor) <> 1 THEN
            RAISE EXCEPTION 'display_base no longer has exactly one anchor: %', anchor;
        END IF;
    END LOOP;
    definition := replace(definition, overall_anchor,
        'OR ((vr.run_status)::text = ''FAILED''::text) OR ' || failed_job || ') THEN ''FAILED''::text');
    definition := replace(definition, code_anchor,
        'WHEN ' || failed_job || ' THEN ''INGESTION_FAILED''::text ' || code_anchor);
    definition := replace(definition, message_anchor,
        'WHEN ' || failed_job || ' THEN ''Customer ingestion failed''::text ' || message_anchor);
    EXECUTE 'CREATE OR REPLACE VIEW control.customer_pipeline_display_base AS '
        || rtrim(definition, E'; \n');
END $failure$;

COMMIT;
