-- Manual, additive scoped resolution intents for genuine mixed-owner captures.
-- Requires027/028. Does not rewrite capture024, transfer history, matching or views.
-- Runtime: explicit SELECT,INSERT on this table; EXECUTE customer_app_scope_status,
-- plus027 trusted resolver grants. Publication worker receives NO new privileges.
BEGIN;
SET LOCAL lock_timeout='5s';
CREATE TABLE control.customer_upload_resolution_attempt (
    stitching_job_id uuid NOT NULL REFERENCES control.stitching_job(job_id),
    attempt integer NOT NULL CHECK (attempt>0),
    -- Intent precedes the run. A future UUID cannot have an immediate FK.
    run_id uuid NOT NULL UNIQUE,
    ingestion_batch_id uuid NOT NULL REFERENCES control.ingestion_batch(batch_id),
    app_id text NOT NULL CHECK (app_id ~ '^[0-9]+$'),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY(stitching_job_id,attempt)
);
CREATE FUNCTION control.guard_customer_upload_resolution_attempt()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE previous record;
BEGIN
    -- Atomic per-job intent admission; caller also holds a session lock during work.
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
    SELECT a.attempt,r.status INTO previous FROM control.customer_upload_resolution_attempt a
        LEFT JOIN control.customer_app_backfill_run r ON r.run_id=a.run_id
        WHERE a.stitching_job_id=NEW.stitching_job_id ORDER BY a.attempt DESC LIMIT 1;
    IF NEW.attempt<>coalesce(previous.attempt,0)+1
        OR (previous.attempt IS NOT NULL AND previous.status IS DISTINCT FROM 'FAILED')
        OR EXISTS(SELECT FROM identity.resolution_run WHERE run_id=NEW.run_id) THEN
        RAISE EXCEPTION 'UPLOAD_RESOLUTION_INVALID_ATTEMPT';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER customer_upload_resolution_intent BEFORE INSERT
    ON control.customer_upload_resolution_attempt FOR EACH ROW
    EXECUTE FUNCTION control.guard_customer_upload_resolution_attempt();
CREATE TRIGGER customer_upload_resolution_immutable BEFORE UPDATE OR DELETE OR TRUNCATE
    ON control.customer_upload_resolution_attempt FOR EACH STATEMENT
    EXECUTE FUNCTION control.reject_customer_identity_snapshot_mutation();

-- Additional gate, not a replacement for ANY027 finalizer check. A full-app
-- latest result can supersede an old upload only when its exact selected batch
-- is included and all of that batch's typed evidence remains available.
CREATE FUNCTION control.guard_customer_upload_resolution_coverage()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE a control.customer_upload_resolution_attempt%ROWTYPE;
BEGIN
    SELECT * INTO a FROM control.customer_upload_resolution_attempt WHERE run_id=NEW.run_id;
    IF NOT FOUND THEN RETURN NEW; END IF;
    IF NEW.app_id<>a.app_id THEN RAISE EXCEPTION 'UPLOAD_RESOLUTION_APP_MISMATCH'; END IF;
    IF TG_OP='UPDATE' AND NEW.status='SUCCEEDED' AND OLD.status<>NEW.status THEN
        IF NOT EXISTS(SELECT FROM control.customer_app_backfill_batch m
            JOIN control.ingestion_batch b USING(batch_id)
            JOIN control.stitching_job j ON j.job_id=a.stitching_job_id
            CROSS JOIN LATERAL control.customer_app_scope_status(b.batch_id) s
            WHERE m.run_id=NEW.run_id AND m.batch_id=a.ingestion_batch_id
                AND m.expected_members=m.available_observations
                AND m.expected_members=j.selected_record_count
                AND j.status='SUCCEEDED' AND b.status='completed'
                AND b.app_id=a.app_id AND s.app_id=a.app_id AND s.error_code IS NULL) THEN
            RAISE EXCEPTION 'UPLOAD_RESOLUTION_SELECTED_COVERAGE';
        END IF;
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER customer_upload_resolution_coverage BEFORE INSERT OR UPDATE
    ON control.customer_app_backfill_run FOR EACH ROW
    EXECUTE FUNCTION control.guard_customer_upload_resolution_coverage();
DO $private_acl$
DECLARE acl record;
BEGIN
    FOR acl IN
        SELECT DISTINCT 'TABLE' AS kind,c.oid::regclass::text AS object_name,a.grantee
        FROM pg_class c CROSS JOIN LATERAL aclexplode(coalesce(c.relacl,acldefault('r',c.relowner))) a
        WHERE c.oid='control.customer_upload_resolution_attempt'::regclass AND a.grantee<>c.relowner
        UNION ALL
        SELECT DISTINCT 'FUNCTION',p.oid::regprocedure::text,a.grantee FROM pg_proc p
        CROSS JOIN LATERAL aclexplode(coalesce(p.proacl,acldefault('f',p.proowner))) a
        WHERE p.oid IN ('control.guard_customer_upload_resolution_attempt()'::regprocedure,
            'control.guard_customer_upload_resolution_coverage()'::regprocedure) AND a.grantee<>p.proowner
    LOOP
        EXECUTE format('REVOKE ALL PRIVILEGES ON %s %s FROM %s CASCADE',acl.kind,acl.object_name,
            CASE WHEN acl.grantee=0 THEN 'PUBLIC' ELSE quote_ident(pg_get_userbyid(acl.grantee)) END);
    END LOOP;
END $private_acl$;
COMMIT;
