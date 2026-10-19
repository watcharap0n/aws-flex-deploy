-- Add durable lineage and stitching lifecycle tracking to the customer pipeline.
-- Apply manually before deploying the updated ingestion Lambda or Fargate image.

BEGIN;

CREATE EXTENSION IF NOT EXISTS pgcrypto;

ALTER TABLE control.ingestion_batch
    ADD COLUMN IF NOT EXISTS landing_batch_id uuid;

DO $migration$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'control.ingestion_batch'::regclass
          AND conname = 'ingestion_batch_landing_batch_id_fkey'
    ) THEN
        ALTER TABLE control.ingestion_batch
            ADD CONSTRAINT ingestion_batch_landing_batch_id_fkey
            FOREIGN KEY (landing_batch_id)
            REFERENCES control.source_landing_batch(batch_id);
    END IF;
END
$migration$;

CREATE INDEX IF NOT EXISTS ix_ingestion_batch_landing_batch_id
    ON control.ingestion_batch (landing_batch_id);

-- Refuse to guess when historical output keys are ambiguous.
DO $migration$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM control.source_landing_batch AS slb
        JOIN control.ingestion_batch AS ib
          ON ib.object_name = slb.output_key
        WHERE slb.output_key IS NOT NULL
        GROUP BY ib.batch_id
        HAVING count(DISTINCT slb.batch_id) > 1
    ) THEN
        RAISE EXCEPTION
            'Cannot backfill ingestion_batch.landing_batch_id: ambiguous output_key matches exist';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM control.source_landing_batch AS slb
        JOIN control.ingestion_batch AS ib
          ON ib.object_name = slb.output_key
        WHERE slb.output_key IS NOT NULL
        GROUP BY slb.batch_id
        HAVING count(DISTINCT ib.batch_id) > 1
    ) THEN
        RAISE EXCEPTION
            'Cannot backfill ingestion_batch.landing_batch_id: one landing output matches multiple ingestion batches';
    END IF;

    IF EXISTS (
        SELECT 1
        FROM control.source_landing_batch AS slb
        JOIN control.ingestion_batch AS ib
          ON ib.object_name = slb.output_key
        WHERE slb.output_key IS NOT NULL
          AND ib.landing_batch_id IS NOT NULL
          AND ib.landing_batch_id <> slb.batch_id
    ) THEN
        RAISE EXCEPTION
            'Cannot backfill ingestion_batch.landing_batch_id: an existing lineage value conflicts';
    END IF;
END
$migration$;

UPDATE control.ingestion_batch AS ib
SET landing_batch_id = slb.batch_id,
    batch_ref = COALESCE(ib.batch_ref, slb.batch_ref),
    updated_at = now()
FROM control.source_landing_batch AS slb
WHERE slb.output_key IS NOT NULL
  AND ib.object_name = slb.output_key
  AND ib.landing_batch_id IS NULL;

CREATE TABLE IF NOT EXISTS raw.customer_batch_member (
    ingestion_batch_id uuid NOT NULL,
    raw_customer_id uuid NOT NULL,
    source_system_id bigint NOT NULL,
    source_customer_id varchar(255) NOT NULL,
    recorded_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT customer_batch_member_pkey
        PRIMARY KEY (ingestion_batch_id, raw_customer_id),
    CONSTRAINT customer_batch_member_batch_fkey
        FOREIGN KEY (ingestion_batch_id)
        REFERENCES control.ingestion_batch(batch_id),
    CONSTRAINT customer_batch_member_raw_customer_fkey
        FOREIGN KEY (raw_customer_id)
        REFERENCES raw.customer(raw_customer_id),
    CONSTRAINT customer_batch_member_source_system_fkey
        FOREIGN KEY (source_system_id)
        REFERENCES control.source_system(source_system_id),
    CONSTRAINT uq_customer_batch_member_source_customer
        UNIQUE (ingestion_batch_id, source_system_id, source_customer_id)
);

CREATE INDEX IF NOT EXISTS ix_customer_batch_member_raw_customer
    ON raw.customer_batch_member (raw_customer_id);

-- Historical membership can only be proven for rows that still point at the
-- ingestion batch today. Future writes populate this table before later
-- upserts can move raw.customer.batch_id.
INSERT INTO raw.customer_batch_member (
    ingestion_batch_id,
    raw_customer_id,
    source_system_id,
    source_customer_id
)
SELECT r.batch_id,
       r.raw_customer_id,
       r.source_system_id,
       r.source_customer_id
FROM raw.customer AS r
JOIN control.ingestion_batch AS ib
  ON ib.batch_id = r.batch_id
ON CONFLICT DO NOTHING;

COMMENT ON COLUMN raw.customer.batch_id IS
    'Latest ingestion batch to upsert this current-state row; use raw.customer_batch_member for historical batch membership.';

CREATE OR REPLACE FUNCTION raw.reject_customer_batch_member_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
BEGIN
    RAISE EXCEPTION
        'raw.customer_batch_member is immutable; % is not allowed', TG_OP;
END
$function$;

DROP TRIGGER IF EXISTS customer_batch_member_immutable
    ON raw.customer_batch_member;

CREATE TRIGGER customer_batch_member_immutable
BEFORE UPDATE OR DELETE OR TRUNCATE
ON raw.customer_batch_member
FOR EACH STATEMENT
EXECUTE FUNCTION raw.reject_customer_batch_member_mutation();

CREATE TABLE IF NOT EXISTS control.stitching_job (
    job_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    ingestion_batch_id uuid NOT NULL,
    attempt integer NOT NULL DEFAULT 1,
    status varchar(30) NOT NULL,
    ecs_task_arn text,
    submitted_at timestamptz,
    started_at timestamptz,
    completed_at timestamptz,
    selected_record_count bigint NOT NULL DEFAULT 0,
    standardized_record_count bigint NOT NULL DEFAULT 0,
    ready_record_count bigint NOT NULL DEFAULT 0,
    member_record_count bigint NOT NULL DEFAULT 0,
    entity_count bigint NOT NULL DEFAULT 0,
    exit_code integer,
    stop_code text,
    stopped_reason text,
    error_code text,
    error_message text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT stitching_job_ingestion_batch_fkey
        FOREIGN KEY (ingestion_batch_id)
        REFERENCES control.ingestion_batch(batch_id),
    CONSTRAINT uq_stitching_job_batch_attempt
        UNIQUE (ingestion_batch_id, attempt),
    CONSTRAINT uq_stitching_job_task_arn
        UNIQUE (ecs_task_arn),
    CONSTRAINT ck_stitching_job_attempt
        CHECK (attempt > 0),
    CONSTRAINT ck_stitching_job_status
        CHECK (status IN (
            'QUEUED',
            'SUBMITTED',
            'WAITING_FOR_LOCK',
            'RUNNING',
            'SUCCEEDED',
            'FAILED',
            'LEGACY_UNKNOWN'
        ))
);

CREATE INDEX IF NOT EXISTS ix_stitching_job_ingestion_batch
    ON control.stitching_job (ingestion_batch_id, attempt DESC);
CREATE INDEX IF NOT EXISTS ix_stitching_job_status
    ON control.stitching_job (status);

ALTER TABLE identity.resolution_run
    ADD COLUMN IF NOT EXISTS stitching_job_id uuid,
    ADD COLUMN IF NOT EXISTS selected_record_count bigint NOT NULL DEFAULT 0;

DO $migration$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'identity.resolution_run'::regclass
          AND conname = 'resolution_run_stitching_job_id_fkey'
    ) THEN
        ALTER TABLE identity.resolution_run
            ADD CONSTRAINT resolution_run_stitching_job_id_fkey
            FOREIGN KEY (stitching_job_id)
            REFERENCES control.stitching_job(job_id);
    END IF;
END
$migration$;

CREATE UNIQUE INDEX IF NOT EXISTS uq_resolution_run_stitching_job
    ON identity.resolution_run (stitching_job_id)
    WHERE stitching_job_id IS NOT NULL;

-- Preserve old ECS dispatches without inventing a resolution-run relationship.
INSERT INTO control.stitching_job (
    ingestion_batch_id,
    attempt,
    status,
    ecs_task_arn,
    submitted_at,
    created_at,
    updated_at
)
SELECT ib.batch_id,
       1,
       'LEGACY_UNKNOWN',
       ib.metadata ->> 'stitching_task_arn',
       ib.updated_at,
       ib.updated_at,
       ib.updated_at
FROM control.ingestion_batch AS ib
WHERE NULLIF(ib.metadata ->> 'stitching_task_arn', '') IS NOT NULL
ON CONFLICT DO NOTHING;

CREATE OR REPLACE VIEW control.customer_pipeline_status AS
SELECT slb.batch_id AS landing_batch_id,
       slb.source_code,
       slb.batch_ref,
       CASE
           WHEN slb.status = 'INVALID'
             OR slb.output_status = 'FAILED'
             OR ib.status = 'failed'
             OR sj.status = 'FAILED'
               THEN 'FAILED'
           WHEN slb.output_status IS DISTINCT FROM 'PUBLISHED'
               THEN 'LANDING'
           WHEN ib.batch_id IS NULL
               THEN 'INGESTION_PENDING'
           WHEN ib.status IS DISTINCT FROM 'completed'
               THEN 'INGESTING'
           WHEN sj.job_id IS NULL OR sj.status = 'QUEUED'
               THEN 'STITCHING_QUEUED'
           WHEN sj.status = 'SUBMITTED'
               THEN 'STITCHING_QUEUED'
           WHEN sj.status = 'WAITING_FOR_LOCK'
               THEN 'WAITING_FOR_LOCK'
           WHEN sj.status = 'RUNNING'
               THEN 'STITCHING'
           WHEN sj.status = 'SUCCEEDED'
             AND (COALESCE(ib.records_rejected, 0) > 0
                  OR sj.ready_record_count < sj.standardized_record_count)
               THEN 'COMPLETED_WITH_WARNINGS'
           WHEN sj.status = 'SUCCEEDED'
               THEN 'COMPLETED'
           ELSE 'STITCHING_UNKNOWN'
       END AS overall_status,
       slb.status AS landing_status,
       slb.output_status,
       slb.output_row_count,
       slb.output_published_at,
       ib.batch_id AS ingestion_batch_id,
       ib.status AS ingestion_status,
       ib.records_received,
       ib.records_loaded,
       ib.records_rejected,
       ib.completed_at AS ingestion_completed_at,
       COALESCE(membership.member_count, 0) AS batch_member_count,
       sj.job_id AS stitching_job_id,
       sj.attempt AS stitching_attempt,
       sj.status AS stitching_status,
       sj.ecs_task_arn,
       rr.run_id AS resolution_run_id,
       sj.selected_record_count,
       sj.standardized_record_count,
       sj.ready_record_count,
       sj.member_record_count,
       sj.entity_count,
       sj.submitted_at AS stitching_submitted_at,
       sj.started_at AS stitching_started_at,
       sj.completed_at AS stitching_completed_at,
       CASE
           WHEN sj.status = 'FAILED'
               THEN COALESCE(sj.error_code, sj.stop_code, 'STITCHING_FAILED')
           WHEN ib.status = 'failed' THEN 'INGESTION_FAILED'
           WHEN slb.output_status = 'FAILED' OR slb.status = 'INVALID'
               THEN 'LANDING_FAILED'
           ELSE NULL
       END AS error_code,
       CASE
           WHEN sj.status = 'FAILED' THEN 'Customer stitching failed'
           WHEN ib.status = 'failed' THEN 'Customer ingestion failed'
           WHEN slb.output_status = 'FAILED' OR slb.status = 'INVALID'
               THEN 'Customer landing failed'
           ELSE NULL
       END AS error_message,
       GREATEST(
           slb.updated_at,
           ib.updated_at,
           sj.updated_at
       ) AS updated_at
FROM control.source_landing_batch AS slb
LEFT JOIN LATERAL (
    SELECT candidate.*
    FROM control.ingestion_batch AS candidate
    WHERE candidate.landing_batch_id = slb.batch_id
    ORDER BY candidate.created_at DESC, candidate.batch_id DESC
    LIMIT 1
) AS ib ON TRUE
LEFT JOIN LATERAL (
    SELECT candidate.*
    FROM control.stitching_job AS candidate
    WHERE candidate.ingestion_batch_id = ib.batch_id
    ORDER BY candidate.attempt DESC, candidate.created_at DESC
    LIMIT 1
) AS sj ON TRUE
LEFT JOIN identity.resolution_run AS rr
  ON rr.stitching_job_id = sj.job_id
LEFT JOIN LATERAL (
    SELECT count(*)::bigint AS member_count
    FROM raw.customer_batch_member AS member
    WHERE member.ingestion_batch_id = ib.batch_id
) AS membership ON TRUE;

COMMENT ON VIEW control.customer_pipeline_status IS
    'One-row-per-landing-batch status contract for a future read-only API.';

COMMIT;
