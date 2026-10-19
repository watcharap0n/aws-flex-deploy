-- Apply after 024 and the existing 015/021 pipeline status contract, with writer approval.
-- This changes PUBLIC completion only: ingestion/stitching execution rows are untouched.
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '120s';

-- PostgreSQL checks function EXECUTE as the querying user even through a view.
-- Expose only a boolean to existing status readers, not the private capture
-- helper or connector tables. Fixed path; no payload/source fields or writes.
CREATE FUNCTION control.customer_publication_is_connector(batch_id uuid)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp AS $$
    SELECT EXISTS(SELECT 1 FROM control.customer_identity_connector_lineage(ARRAY[batch_id]))
$$;
DO $$
DECLARE acl record;
BEGIN
    FOR acl IN SELECT a.grantee FROM pg_proc p,
        LATERAL aclexplode(coalesce(p.proacl,acldefault('f',p.proowner))) a
        WHERE p.oid='control.customer_publication_is_connector(uuid)'::regprocedure
          AND a.grantee<>p.proowner
    LOOP
        EXECUTE format('REVOKE ALL ON FUNCTION control.customer_publication_is_connector(uuid) FROM %s CASCADE',
            CASE WHEN acl.grantee=0 THEN 'PUBLIC' ELSE quote_ident(pg_get_userbyid(acl.grantee)) END);
    END LOOP;
END $$;

-- Upload-only contract. Connector API flag remains false for this release.
-- Follow the effective latest attempt, never gate connector completion.
-- Referencing the outbox here deliberately fails deployment when 024 is absent.
CREATE VIEW control.customer_publication_status AS
SELECT ib.batch_id AS ingestion_batch_id,
       sj.job_id AS stitching_job_id,
       rr.run_id AS resolution_run_id,
       sj.status AS stitching_status,
       CASE
           WHEN ib.status = 'failed' THEN 'FAILED'
           WHEN ib.status IS DISTINCT FROM 'completed' THEN 'INGESTING'
           WHEN sj.status = 'SUCCEEDED' THEN
               CASE COALESCE(t.status, issue.status)
                   WHEN 'PUBLISHED' THEN CASE
                       WHEN COALESCE(ib.records_rejected, 0) > 0
                         OR sj.ready_record_count < sj.standardized_record_count
                           THEN 'COMPLETED_WITH_WARNINGS'
                       ELSE 'COMPLETED'
                   END
                   WHEN 'RUNNING' THEN 'TRANSFERRING'
                   WHEN 'FAILED' THEN 'TRANSFER_FAILED'
                   WHEN 'BLOCKED' THEN 'TRANSFER_BLOCKED'
                   ELSE 'TRANSFER_PENDING'
               END
           WHEN sj.status = 'FAILED' THEN 'FAILED'
           WHEN sj.status IN ('QUEUED', 'SUBMITTED') OR sj.job_id IS NULL
               THEN 'STITCHING_QUEUED'
           WHEN sj.status = 'WAITING_FOR_LOCK' THEN 'WAITING_FOR_LOCK'
           WHEN sj.status = 'VALIDATING' THEN 'VALIDATING'
           WHEN sj.status = 'RUNNING' THEN 'STITCHING'
           ELSE 'STITCHING_UNKNOWN'
       END AS overall_status,
       t.transfer_id,
       COALESCE(t.status, issue.status) AS transfer_status,
       t.workspace_id AS transfer_workspace_id,
       t.app_id AS transfer_app_id,
       t.publication_seq AS transfer_publication_seq,
       t.published_at AS transfer_published_at,
       CASE WHEN t.transfer_id IS NOT NULL THEN t.error_code
            ELSE issue.error_code END AS transfer_error_code,
       COALESCE(t.updated_at, issue.created_at) AS transfer_updated_at
FROM control.ingestion_batch ib
LEFT JOIN LATERAL (
    SELECT candidate.* FROM control.stitching_job candidate
    WHERE candidate.ingestion_batch_id = ib.batch_id
    ORDER BY candidate.attempt DESC, candidate.created_at DESC, candidate.job_id DESC
    LIMIT 1
) sj ON TRUE
LEFT JOIN identity.resolution_run rr ON rr.stitching_job_id = sj.job_id
LEFT JOIN control.customer_identity_transfer t
  ON t.stitching_job_id = sj.job_id
 AND t.resolution_run_id = rr.run_id
 AND t.ingestion_batch_id = ib.batch_id
-- Capture may fail before an outbox/resolution exists. Only the effective
-- succeeded job's issue applies, and a valid outbox always supersedes it.
LEFT JOIN control.customer_identity_capture_issue issue
  ON issue.stitching_job_id = sj.job_id
 AND sj.status = 'SUCCEEDED'
 AND t.transfer_id IS NULL
WHERE NOT control.customer_publication_is_connector(ib.batch_id);

-- Preserve the installed view's OID/ACL/column order, including 021 history fields
-- and any later additive extensions. Never drop/recreate or rebuild a stale 015 copy.
DO $$
DECLARE definition text; projection text;
BEGIN
    IF to_regclass('control.customer_pipeline_status') IS NULL THEN
        RAISE EXCEPTION 'CUSTOMER_PIPELINE_STATUS_REQUIRED';
    END IF;
    definition := rtrim(pg_get_viewdef('control.customer_pipeline_status'::regclass, true), E';\n ');
    SELECT string_agg(
        CASE a.attname
            WHEN 'overall_status' THEN
                'CASE WHEN control.customer_publication_is_connector(p.ingestion_batch_id) '
                || 'THEN p.overall_status '
                || 'WHEN p.overall_status IN (''COMPLETED'', ''COMPLETED_WITH_WARNINGS'') '
                || 'THEN COALESCE(t.overall_status, ''TRANSFER_PENDING'') '
                || 'ELSE p.overall_status END AS overall_status'
            WHEN 'updated_at' THEN
                'GREATEST(p.updated_at, t.transfer_updated_at) AS updated_at'
            ELSE format('p.%I', a.attname)
        END, ', ' ORDER BY a.attnum)
    INTO projection
    FROM pg_attribute a
    WHERE a.attrelid = 'control.customer_pipeline_status'::regclass
      AND a.attnum > 0 AND NOT a.attisdropped;
    EXECUTE 'CREATE OR REPLACE VIEW control.customer_pipeline_status AS SELECT '
        || projection
        || ', t.transfer_id, t.transfer_status, t.transfer_workspace_id, t.transfer_app_id'
        || ', t.transfer_publication_seq, t.transfer_published_at, t.transfer_error_code'
        || ', t.transfer_updated_at FROM (' || definition || ') p'
        || ' LEFT JOIN control.customer_publication_status t'
        || ' ON t.ingestion_batch_id = p.ingestion_batch_id'
        || ' AND t.stitching_job_id = p.stitching_job_id'
        || ' AND t.resolution_run_id IS NOT DISTINCT FROM p.resolution_run_id';
END $$;

-- Copy only existing SELECT grants for the API's lightweight read contract.
DO $$
DECLARE reader record;
BEGIN
    FOR reader IN
        SELECT DISTINCT x.grantee
        FROM pg_class c, LATERAL aclexplode(c.relacl) x
        WHERE c.oid = 'control.customer_pipeline_status'::regclass
          AND x.privilege_type = 'SELECT'
        UNION
        SELECT relowner FROM pg_class
        WHERE oid = 'control.customer_pipeline_status'::regclass
    LOOP
        IF reader.grantee = 0 THEN
            GRANT SELECT ON control.customer_publication_status TO PUBLIC;
            GRANT EXECUTE ON FUNCTION control.customer_publication_is_connector(uuid) TO PUBLIC;
        ELSE
            EXECUTE format('GRANT SELECT ON control.customer_publication_status TO %I',
                           pg_get_userbyid(reader.grantee));
            EXECUTE format('GRANT EXECUTE ON FUNCTION control.customer_publication_is_connector(uuid) TO %I',
                           pg_get_userbyid(reader.grantee));
        END IF;
    END LOOP;
END $$;
COMMENT ON VIEW control.customer_publication_status IS
    'Upload-only publication status for latest stitching attempt. Connector batches are excluded; missing/mismatched upload outbox is never complete.';
COMMIT;
