-- Manual additive continuation state. Requires 031; install BEFORE revised032.
-- No jobs, source, capture or frozen evidence are rewritten by this migration.
BEGIN;
SET LOCAL lock_timeout='5s';

CREATE TABLE control.customer_upload_resolution_scan (
    singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton),
    last_job_id uuid NOT NULL
);

CREATE TABLE control.customer_upload_resolution_attempt_status (
    run_id uuid PRIMARY KEY REFERENCES control.customer_upload_resolution_attempt(run_id),
    status text NOT NULL CHECK(status IN ('RUNNING','RETRY_WAIT','FAILED','SUCCEEDED')),
    retry_count integer NOT NULL DEFAULT 0 CHECK(retry_count>=0),
    next_attempt_at timestamptz,
    error_code text CHECK(error_code ~ '^[A-Z][A-Z0-9_]{0,95}$'),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK((status IN ('RUNNING','RETRY_WAIT')) = (next_attempt_at IS NOT NULL)),
    CHECK(status NOT IN ('FAILED','RETRY_WAIT') OR error_code IS NOT NULL),
    CHECK(status<>'SUCCEEDED' OR error_code IS NULL)
);
CREATE INDEX customer_upload_resolution_due ON control.customer_upload_resolution_attempt_status
    (next_attempt_at,run_id) WHERE status IN ('RUNNING','RETRY_WAIT');

CREATE FUNCTION control.guard_customer_upload_resolution_attempt_status()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
BEGIN
    IF TG_OP='UPDATE' AND (NEW.run_id<>OLD.run_id OR OLD.status IN ('FAILED','SUCCEEDED')) THEN
        RAISE EXCEPTION 'Customer upload attempt status is immutable';
    END IF;
    IF NEW.status='SUCCEEDED' AND NOT EXISTS(SELECT FROM control.customer_app_backfill_run
        WHERE run_id=NEW.run_id AND status='SUCCEEDED') THEN
        RAISE EXCEPTION 'UPLOAD_RESOLUTION_NOT_SUCCEEDED';
    END IF;
    IF NEW.status='FAILED' AND EXISTS(SELECT FROM control.customer_app_backfill_run
        WHERE run_id=NEW.run_id AND status='SUCCEEDED') THEN
        RAISE EXCEPTION 'UPLOAD_RESOLUTION_ALREADY_SUCCEEDED';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER customer_upload_resolution_status_guard BEFORE INSERT OR UPDATE
    ON control.customer_upload_resolution_attempt_status FOR EACH ROW
    EXECUTE FUNCTION control.guard_customer_upload_resolution_attempt_status();
CREATE TRIGGER customer_upload_resolution_status_no_delete BEFORE DELETE OR TRUNCATE
    ON control.customer_upload_resolution_attempt_status FOR EACH STATEMENT
    EXECUTE FUNCTION control.reject_customer_identity_snapshot_mutation();

-- Preserve every 031 eligibility rule. Only the previous terminal predicate is
-- extended, to permit an explicit new UUID after failure BEFORE a real run exists.
CREATE OR REPLACE FUNCTION control.guard_customer_upload_resolution_attempt()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE previous record;
BEGIN
    PERFORM 1 FROM control.stitching_job WHERE job_id=NEW.stitching_job_id FOR UPDATE;
    IF NOT EXISTS(SELECT FROM control.stitching_job j
        JOIN control.ingestion_batch b ON b.batch_id=j.ingestion_batch_id
        JOIN control.customer_identity_transfer t ON t.stitching_job_id=j.job_id
        CROSS JOIN LATERAL control.customer_app_scope_status(b.batch_id) s
        WHERE j.job_id=NEW.stitching_job_id AND j.status='SUCCEEDED' AND b.status='completed'
            AND b.batch_id=NEW.ingestion_batch_id AND b.app_id=NEW.app_id
            AND s.app_id=NEW.app_id AND s.error_code IS NULL
            AND t.status='BLOCKED' AND t.error_code='MIXED_OR_UNKNOWN_OWNER') THEN
        RAISE EXCEPTION 'UPLOAD_RESOLUTION_NOT_ELIGIBLE';
    END IF;
    SELECT a.attempt,coalesce(r.status,s.status) AS status INTO previous
        FROM control.customer_upload_resolution_attempt a
        LEFT JOIN control.customer_app_backfill_run r ON r.run_id=a.run_id
        LEFT JOIN control.customer_upload_resolution_attempt_status s ON s.run_id=a.run_id
        WHERE a.stitching_job_id=NEW.stitching_job_id ORDER BY a.attempt DESC LIMIT 1;
    IF NEW.attempt<>coalesce(previous.attempt,0)+1
        OR (previous.attempt IS NOT NULL AND previous.status IS DISTINCT FROM 'FAILED')
        OR EXISTS(SELECT FROM identity.resolution_run WHERE run_id=NEW.run_id) THEN
        RAISE EXCEPTION 'UPLOAD_RESOLUTION_INVALID_ATTEMPT';
    END IF;
    RETURN NEW;
END $$;

DO $private_acl$
DECLARE acl record;
BEGIN
    FOR acl IN
        SELECT DISTINCT 'TABLE' AS kind,c.oid::regclass::text AS object_name,a.grantee
        FROM pg_class c CROSS JOIN LATERAL aclexplode(coalesce(c.relacl,acldefault('r',c.relowner))) a
        WHERE c.oid IN ('control.customer_upload_resolution_attempt_status'::regclass,
            'control.customer_upload_resolution_scan'::regclass) AND a.grantee<>c.relowner
        UNION ALL
        SELECT DISTINCT 'FUNCTION',p.oid::regprocedure::text,a.grantee FROM pg_proc p
        CROSS JOIN LATERAL aclexplode(coalesce(p.proacl,acldefault('f',p.proowner))) a
        WHERE p.oid='control.guard_customer_upload_resolution_attempt_status()'::regprocedure
            AND a.grantee<>p.proowner
    LOOP
        EXECUTE format('REVOKE ALL PRIVILEGES ON %s %s FROM %s CASCADE',acl.kind,acl.object_name,
            CASE WHEN acl.grantee=0 THEN 'PUBLIC' ELSE quote_ident(pg_get_userbyid(acl.grantee)) END);
    END LOOP;
END $private_acl$;
COMMIT;
