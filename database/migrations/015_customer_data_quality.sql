-- Add immutable customer data-quality snapshots before identity stitching.
-- Apply manually before deploying the updated ingestion Lambda or Fargate image.

BEGIN;

CREATE EXTENSION IF NOT EXISTS pgcrypto;
CREATE SCHEMA IF NOT EXISTS quality;

ALTER TABLE control.stitching_job
    DROP CONSTRAINT IF EXISTS ck_stitching_job_status;

ALTER TABLE control.stitching_job
    ADD CONSTRAINT ck_stitching_job_status
    CHECK (status IN (
        'QUEUED',
        'SUBMITTED',
        'WAITING_FOR_LOCK',
        'VALIDATING',
        'RUNNING',
        'SUCCEEDED',
        'FAILED',
        'LEGACY_UNKNOWN'
    ));

CREATE TABLE IF NOT EXISTS quality.customer_validation_run (
    validation_run_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    landing_batch_id uuid NOT NULL,
    ingestion_batch_id uuid NOT NULL,
    stitching_job_id uuid NOT NULL,
    attempt integer NOT NULL,
    run_status varchar(20) NOT NULL DEFAULT 'PENDING',
    ruleset_version varchar(50) NOT NULL,
    normalization_version varchar(32),
    source_row_count bigint NOT NULL DEFAULT 0,
    canonical_row_count bigint NOT NULL DEFAULT 0,
    selected_record_count bigint NOT NULL DEFAULT 0,
    standardized_record_count bigint NOT NULL DEFAULT 0,
    ready_record_count bigint NOT NULL DEFAULT 0,
    issue_record_count bigint NOT NULL DEFAULT 0,
    blank_key_row_count bigint NOT NULL DEFAULT 0,
    duplicate_group_count bigint NOT NULL DEFAULT 0,
    duplicate_affected_row_count bigint NOT NULL DEFAULT 0,
    duplicate_excess_row_count bigint NOT NULL DEFAULT 0,
    existing_source_record_count bigint NOT NULL DEFAULT 0,
    existing_entity_record_count bigint NOT NULL DEFAULT 0,
    new_to_cdp_record_count bigint NOT NULL DEFAULT 0,
    ambiguous_entity_record_count bigint NOT NULL DEFAULT 0,
    insufficient_identifier_count bigint NOT NULL DEFAULT 0,
    error_stage varchar(30),
    error_code text,
    error_message text,
    started_at timestamptz,
    completed_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT customer_validation_run_landing_batch_fkey
        FOREIGN KEY (landing_batch_id)
        REFERENCES control.source_landing_batch(batch_id),
    CONSTRAINT customer_validation_run_ingestion_batch_fkey
        FOREIGN KEY (ingestion_batch_id)
        REFERENCES control.ingestion_batch(batch_id),
    CONSTRAINT customer_validation_run_stitching_job_fkey
        FOREIGN KEY (stitching_job_id)
        REFERENCES control.stitching_job(job_id),
    CONSTRAINT uq_customer_validation_run_job
        UNIQUE (stitching_job_id),
    CONSTRAINT uq_customer_validation_run_batch_attempt
        UNIQUE (ingestion_batch_id, attempt),
    CONSTRAINT ck_customer_validation_run_attempt
        CHECK (attempt > 0),
    CONSTRAINT ck_customer_validation_run_status
        CHECK (run_status IN ('PENDING', 'RUNNING', 'SUCCEEDED', 'FAILED')),
    CONSTRAINT ck_customer_validation_run_counts
        CHECK (
            source_row_count >= 0
            AND canonical_row_count >= 0
            AND selected_record_count >= 0
            AND standardized_record_count >= 0
            AND ready_record_count >= 0
            AND issue_record_count >= 0
            AND blank_key_row_count >= 0
            AND duplicate_group_count >= 0
            AND duplicate_affected_row_count >= 0
            AND duplicate_excess_row_count >= 0
            AND existing_source_record_count >= 0
            AND existing_entity_record_count >= 0
            AND new_to_cdp_record_count >= 0
            AND ambiguous_entity_record_count >= 0
            AND insufficient_identifier_count >= 0
        )
);

CREATE INDEX IF NOT EXISTS ix_customer_validation_run_landing
    ON quality.customer_validation_run (landing_batch_id, attempt DESC);
CREATE INDEX IF NOT EXISTS ix_customer_validation_run_ingestion
    ON quality.customer_validation_run (ingestion_batch_id, attempt DESC);
CREATE INDEX IF NOT EXISTS ix_customer_validation_run_status
    ON quality.customer_validation_run (run_status, updated_at);

CREATE TABLE IF NOT EXISTS quality.customer_validation_metric (
    metric_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    validation_run_id uuid NOT NULL,
    dimension varchar(30) NOT NULL,
    rule_code varchar(100) NOT NULL,
    field_name varchar(100) NOT NULL DEFAULT '*',
    evaluation_status varchar(20) NOT NULL,
    severity varchar(20) NOT NULL,
    observed_value numeric,
    denominator bigint,
    failure_count bigint,
    metric_rate numeric(12,8),
    threshold jsonb NOT NULL DEFAULT '{}'::jsonb,
    baseline_value numeric,
    baseline_sample_count integer,
    delta_value numeric,
    details jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT customer_validation_metric_run_fkey
        FOREIGN KEY (validation_run_id)
        REFERENCES quality.customer_validation_run(validation_run_id),
    CONSTRAINT uq_customer_validation_metric_rule
        UNIQUE (validation_run_id, rule_code, field_name),
    CONSTRAINT ck_customer_validation_metric_dimension
        CHECK (dimension IN (
            'SCHEMA',
            'RECONCILIATION',
            'COMPLETENESS',
            'VALIDITY',
            'UNIQUENESS',
            'IDENTITY_COVERAGE',
            'TIMELINESS',
            'ANOMALY'
        )),
    CONSTRAINT ck_customer_validation_metric_status
        CHECK (evaluation_status IN ('PASS', 'ISSUE', 'NOT_EVALUATED')),
    CONSTRAINT ck_customer_validation_metric_severity
        CHECK (severity IN ('INFO', 'WARNING', 'ERROR')),
    CONSTRAINT ck_customer_validation_metric_counts
        CHECK (
            (denominator IS NULL OR denominator >= 0)
            AND (failure_count IS NULL OR failure_count >= 0)
            AND (baseline_sample_count IS NULL OR baseline_sample_count >= 0)
            AND (metric_rate IS NULL OR (metric_rate >= 0 AND metric_rate <= 1))
        )
);

CREATE INDEX IF NOT EXISTS ix_customer_validation_metric_run_dimension
    ON quality.customer_validation_metric (validation_run_id, dimension, field_name);

CREATE TABLE IF NOT EXISTS quality.customer_validation_record (
    validation_record_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    validation_run_id uuid NOT NULL,
    source_record_id uuid,
    raw_customer_id uuid,
    source_row_number integer NOT NULL,
    selected_for_canonical boolean NOT NULL DEFAULT FALSE,
    disposition varchar(30) NOT NULL DEFAULT 'ACCEPTED',
    existing_source_record boolean NOT NULL DEFAULT FALSE,
    preexisting_entity_id uuid,
    identity_classification varchar(40) NOT NULL DEFAULT 'NOT_EVALUATED',
    entity_candidate_count integer NOT NULL DEFAULT 0,
    matched_identifier_types text[] NOT NULL DEFAULT '{}'::text[],
    record_fingerprint varchar(64),
    issue_codes text[] NOT NULL DEFAULT '{}'::text[],
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT customer_validation_record_run_fkey
        FOREIGN KEY (validation_run_id)
        REFERENCES quality.customer_validation_run(validation_run_id),
    CONSTRAINT customer_validation_record_source_record_fkey
        FOREIGN KEY (source_record_id)
        REFERENCES raw.customer_landing_source(source_record_id),
    CONSTRAINT customer_validation_record_raw_customer_fkey
        FOREIGN KEY (raw_customer_id)
        REFERENCES raw.customer(raw_customer_id),
    CONSTRAINT customer_validation_record_preexisting_entity_fkey
        FOREIGN KEY (preexisting_entity_id)
        REFERENCES identity.customer_entity(customer_entity_id),
    CONSTRAINT uq_customer_validation_record_source_row
        UNIQUE (validation_run_id, source_row_number),
    CONSTRAINT ck_customer_validation_record_source_row
        CHECK (source_row_number > 0),
    CONSTRAINT ck_customer_validation_record_disposition
        CHECK (disposition IN (
            'ACCEPTED',
            'ACCEPTED_WITH_ISSUES',
            'EXCLUDED'
        )),
    CONSTRAINT ck_customer_validation_record_identity
        CHECK (identity_classification IN (
            'NEW_TO_CDP',
            'EXISTING_SOURCE_RECORD',
            'EXISTING_ENTITY',
            'AMBIGUOUS_EXISTING_ENTITY',
            'INSUFFICIENT_IDENTIFIERS',
            'NOT_EVALUATED'
        )),
    CONSTRAINT ck_customer_validation_record_candidates
        CHECK (entity_candidate_count >= 0)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_customer_validation_record_source
    ON quality.customer_validation_record (validation_run_id, source_record_id)
    WHERE source_record_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_customer_validation_record_run_disposition
    ON quality.customer_validation_record (validation_run_id, disposition, source_row_number);
CREATE INDEX IF NOT EXISTS ix_customer_validation_record_raw_customer
    ON quality.customer_validation_record (raw_customer_id)
    WHERE raw_customer_id IS NOT NULL;

CREATE OR REPLACE FUNCTION quality.enforce_validation_run_transition()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
BEGIN
    IF OLD.run_status IN ('SUCCEEDED', 'FAILED') AND NEW IS DISTINCT FROM OLD THEN
        RAISE EXCEPTION
            'quality.customer_validation_run % is immutable after %',
            OLD.validation_run_id,
            OLD.run_status;
    END IF;

    IF OLD.run_status = 'PENDING'
       AND NEW.run_status NOT IN ('PENDING', 'RUNNING', 'FAILED') THEN
        RAISE EXCEPTION
            'invalid validation run transition: % -> %',
            OLD.run_status,
            NEW.run_status;
    END IF;

    IF OLD.run_status = 'RUNNING'
       AND NEW.run_status NOT IN ('RUNNING', 'SUCCEEDED', 'FAILED') THEN
        RAISE EXCEPTION
            'invalid validation run transition: % -> %',
            OLD.run_status,
            NEW.run_status;
    END IF;

    RETURN NEW;
END
$function$;

DROP TRIGGER IF EXISTS customer_validation_run_transition
    ON quality.customer_validation_run;

CREATE TRIGGER customer_validation_run_transition
BEFORE UPDATE
ON quality.customer_validation_run
FOR EACH ROW
EXECUTE FUNCTION quality.enforce_validation_run_transition();

CREATE OR REPLACE FUNCTION quality.reject_terminal_validation_child_mutation()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
DECLARE
    run_id uuid;
    status_value varchar(20);
BEGIN
    run_id := CASE WHEN TG_OP = 'DELETE'
                   THEN OLD.validation_run_id
                   ELSE NEW.validation_run_id
              END;

    SELECT run_status
    INTO status_value
    FROM quality.customer_validation_run
    WHERE validation_run_id = run_id;

    IF status_value IN ('SUCCEEDED', 'FAILED') THEN
        RAISE EXCEPTION
            'validation child rows are immutable after run % is %',
            run_id,
            status_value;
    END IF;

    RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
END
$function$;

DROP TRIGGER IF EXISTS customer_validation_metric_immutable
    ON quality.customer_validation_metric;
CREATE TRIGGER customer_validation_metric_immutable
BEFORE INSERT OR UPDATE OR DELETE
ON quality.customer_validation_metric
FOR EACH ROW
EXECUTE FUNCTION quality.reject_terminal_validation_child_mutation();

DROP TRIGGER IF EXISTS customer_validation_record_immutable
    ON quality.customer_validation_record;
CREATE TRIGGER customer_validation_record_immutable
BEFORE INSERT OR UPDATE OR DELETE
ON quality.customer_validation_record
FOR EACH ROW
EXECUTE FUNCTION quality.reject_terminal_validation_child_mutation();

CREATE OR REPLACE VIEW control.customer_data_quality_metric AS
SELECT vr.landing_batch_id,
       vr.ingestion_batch_id,
       vr.stitching_job_id,
       vr.validation_run_id,
       vr.attempt AS validation_attempt,
       vr.run_status AS validation_run_status,
       vr.ruleset_version,
       vr.normalization_version,
       metric.dimension,
       metric.rule_code,
       metric.field_name,
       metric.evaluation_status,
       metric.severity,
       metric.observed_value,
       metric.denominator,
       metric.failure_count,
       metric.metric_rate,
       metric.threshold,
       metric.baseline_value,
       metric.baseline_sample_count,
       metric.delta_value,
       metric.details,
       metric.created_at
FROM quality.customer_validation_metric AS metric
JOIN quality.customer_validation_run AS vr
  ON vr.validation_run_id = metric.validation_run_id;

COMMENT ON VIEW control.customer_data_quality_metric IS
    'PII-safe customer data-quality metrics addressable by public landing_batch_id.';

CREATE OR REPLACE VIEW control.customer_data_quality_record AS
SELECT vr.landing_batch_id,
       vr.ingestion_batch_id,
       vr.stitching_job_id,
       vr.validation_run_id,
       vr.attempt AS validation_attempt,
       vr.run_status AS validation_run_status,
       record.source_record_id,
       record.raw_customer_id,
       record.source_row_number,
       record.selected_for_canonical,
       record.disposition,
       record.existing_source_record,
       record.preexisting_entity_id,
       record.identity_classification,
       record.entity_candidate_count,
       record.matched_identifier_types,
       record.record_fingerprint,
       record.issue_codes,
       record.created_at,
       record.updated_at
FROM quality.customer_validation_record AS record
JOIN quality.customer_validation_run AS vr
  ON vr.validation_run_id = record.validation_run_id;

COMMENT ON VIEW control.customer_data_quality_record IS
    'PII-safe row outcomes for customer uploads; join source_record_id only under controlled access.';

CREATE OR REPLACE VIEW control.customer_pipeline_status AS
SELECT slb.batch_id AS landing_batch_id,
       slb.source_code,
       slb.batch_ref,
       CASE
           WHEN slb.status = 'INVALID'
             OR slb.output_status = 'FAILED'
             OR ib.status = 'failed'
             OR sj.status = 'FAILED'
             OR vr.run_status = 'FAILED'
               THEN 'FAILED'
           WHEN slb.output_status IS DISTINCT FROM 'PUBLISHED'
               THEN 'LANDING'
           WHEN ib.batch_id IS NULL
               THEN 'INGESTION_PENDING'
           WHEN ib.status IS DISTINCT FROM 'completed'
               THEN 'INGESTING'
           WHEN vr.run_status = 'PENDING'
               THEN 'VALIDATION_PENDING'
           WHEN vr.run_status = 'RUNNING' OR sj.status = 'VALIDATING'
               THEN 'VALIDATING'
           WHEN sj.job_id IS NULL OR sj.status IN ('QUEUED', 'SUBMITTED')
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
           WHEN vr.run_status = 'FAILED'
               THEN COALESCE(vr.error_code, 'VALIDATION_FAILED')
           WHEN sj.status = 'FAILED'
               THEN COALESCE(sj.error_code, sj.stop_code, 'STITCHING_FAILED')
           WHEN ib.status = 'failed' THEN 'INGESTION_FAILED'
           WHEN slb.output_status = 'FAILED' OR slb.status = 'INVALID'
               THEN 'LANDING_FAILED'
           ELSE NULL
       END AS error_code,
       CASE
           WHEN vr.run_status = 'FAILED'
               THEN COALESCE(vr.error_message, 'Customer data validation failed')
           WHEN sj.status = 'FAILED' THEN 'Customer stitching failed'
           WHEN ib.status = 'failed' THEN 'Customer ingestion failed'
           WHEN slb.output_status = 'FAILED' OR slb.status = 'INVALID'
               THEN 'Customer landing failed'
           ELSE NULL
       END AS error_message,
       GREATEST(
           slb.updated_at,
           ib.updated_at,
           sj.updated_at,
           vr.updated_at
       ) AS updated_at,
       (vr.validation_run_id IS NOT NULL) AS validation_available,
       vr.validation_run_id,
       vr.run_status AS validation_run_status,
       vr.started_at AS validation_started_at,
       vr.completed_at AS validation_completed_at,
       vr.source_row_count AS validation_source_row_count,
       vr.canonical_row_count AS validation_canonical_row_count,
       vr.standardized_record_count AS validation_standardized_record_count,
       vr.issue_record_count AS validation_issue_record_count,
       vr.blank_key_row_count AS validation_blank_key_row_count,
       vr.duplicate_group_count AS validation_duplicate_group_count,
       vr.duplicate_affected_row_count AS validation_duplicate_affected_row_count,
       vr.duplicate_excess_row_count AS validation_duplicate_excess_row_count,
       vr.existing_source_record_count AS validation_existing_source_record_count,
       vr.existing_entity_record_count AS validation_existing_entity_record_count,
       vr.new_to_cdp_record_count AS validation_new_to_cdp_record_count,
       vr.ambiguous_entity_record_count AS validation_ambiguous_entity_record_count,
       vr.insufficient_identifier_count AS validation_insufficient_identifier_count,
       vr.error_stage,
       vr.error_code AS validation_error_code,
       vr.error_message AS validation_error_message
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
LEFT JOIN quality.customer_validation_run AS vr
  ON vr.stitching_job_id = sj.job_id
LEFT JOIN identity.resolution_run AS rr
  ON rr.stitching_job_id = sj.job_id
LEFT JOIN LATERAL (
    SELECT count(*)::bigint AS member_count
    FROM raw.customer_batch_member AS member
    WHERE member.ingestion_batch_id = ib.batch_id
) AS membership ON TRUE;

COMMENT ON VIEW control.customer_pipeline_status IS
    'One-row-per-landing-batch pipeline and customer data-quality status contract.';

COMMIT;
