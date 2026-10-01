-- Additive evidence only, after 025/026/027/028. Manual writer-approved rollout.
-- An app snapshot includes the batch in its frozen input manifest; it does NOT
-- publish the old stitching resolution or every historical observation.
-- No execution/outbox/history writes, no replacement overall_status values.
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '120s';

CREATE VIEW control.customer_upload_historical_coverage AS
SELECT DISTINCT ON (b.batch_id)
       b.batch_id AS ingestion_batch_id,
       'PUBLISHED'::text AS app_snapshot_status,
       'LATEST_APP_USERS_AT_CUTOFF'::text AS app_snapshot_scope,
       r.run_id AS app_snapshot_run_id,
       r.app_id AS app_snapshot_app_id,
       r.workspace_id AS app_snapshot_workspace_id,
       r.cutoff AS app_snapshot_cutoff,
       r.input_sha256 AS app_snapshot_input_sha256,
       r.expected_users AS app_snapshot_app_user_count,
       b.expected_members AS app_snapshot_batch_expected_members,
       b.available_observations AS app_snapshot_batch_available_observations,
       b.expected_members-b.available_observations AS app_snapshot_batch_missing_observations,
       t.transfer_id AS app_snapshot_transfer_id,
       t.publication_seq AS app_snapshot_publication_seq,
       t.published_at AS app_snapshot_published_at,
       t.updated_at AS app_snapshot_updated_at
FROM control.customer_app_backfill_batch b
JOIN control.customer_app_backfill_run r USING (run_id)
JOIN control.ingestion_batch ib ON ib.batch_id=b.batch_id
JOIN identity.resolution_run rr ON rr.run_id=r.run_id
JOIN control.customer_identity_transfer t
  ON t.backfill_run_id=r.run_id
 AND t.transfer_id=r.run_id
 AND t.resolution_run_id=r.run_id
 AND t.ingestion_batch_id IS NULL AND t.stitching_job_id IS NULL
 AND t.app_id=r.app_id AND t.workspace_id=r.workspace_id
 AND t.row_count=r.expected_users
WHERE r.status='SUCCEEDED' AND rr.status='SUCCEEDED'
  AND rr.stitching_job_id IS NULL
  AND ib.app_id=r.app_id AND ib.created_at=b.batch_created_at
  AND b.batch_created_at<=r.cutoff
  AND t.status='PUBLISHED' AND t.published_at IS NOT NULL AND t.source_sha256 IS NOT NULL
  AND NOT control.customer_publication_is_connector(ib.batch_id)
ORDER BY b.batch_id,t.publication_seq DESC,r.run_id DESC;

-- Keep OID, owner, ACL, column order and every existing value (including clocks,
-- transfer errors, history outcome, and future extensions). Evidence clocks are
-- separate: nothing here claims that the original execution advanced.
DO $$
DECLARE definition text; projection text;
BEGIN
    IF to_regclass('control.customer_pipeline_status') IS NULL THEN
        RAISE EXCEPTION 'CUSTOMER_PIPELINE_STATUS_REQUIRED';
    END IF;
    definition := rtrim(pg_get_viewdef('control.customer_pipeline_status'::regclass, true), E';\n ');
    SELECT string_agg(format('p.%I', a.attname), ', ' ORDER BY a.attnum)
      INTO projection FROM pg_attribute a
      WHERE a.attrelid='control.customer_pipeline_status'::regclass
        AND a.attnum>0 AND NOT a.attisdropped;
    EXECUTE 'CREATE OR REPLACE VIEW control.customer_pipeline_status AS SELECT '
      || projection
      || ', h.app_snapshot_status, h.app_snapshot_scope, h.app_snapshot_run_id'
      || ', h.app_snapshot_app_id, h.app_snapshot_workspace_id, h.app_snapshot_cutoff'
      || ', h.app_snapshot_input_sha256, h.app_snapshot_app_user_count'
      || ', h.app_snapshot_batch_expected_members, h.app_snapshot_batch_available_observations'
      || ', h.app_snapshot_batch_missing_observations, h.app_snapshot_transfer_id'
      || ', h.app_snapshot_publication_seq, h.app_snapshot_published_at, h.app_snapshot_updated_at'
      || ' FROM (' || definition || ') p LEFT JOIN control.customer_upload_historical_coverage h'
      || ' ON h.ingestion_batch_id=p.ingestion_batch_id';
END $$;

DO $$
DECLARE reader record;
BEGIN
    FOR reader IN
        SELECT DISTINCT a.grantee FROM pg_class c, LATERAL aclexplode(c.relacl) a
        WHERE c.oid='control.customer_pipeline_status'::regclass AND a.privilege_type='SELECT'
        UNION SELECT relowner FROM pg_class WHERE oid='control.customer_pipeline_status'::regclass
    LOOP
        EXECUTE format('GRANT SELECT ON control.customer_upload_historical_coverage TO %s',
            CASE WHEN reader.grantee=0 THEN 'PUBLIC' ELSE quote_ident(pg_get_userbyid(reader.grantee)) END);
        -- Same non-PII boolean as 025; readers granted since 025 need it too.
        EXECUTE format('GRANT EXECUTE ON FUNCTION control.customer_publication_is_connector(uuid) TO %s',
            CASE WHEN reader.grantee=0 THEN 'PUBLIC' ELSE quote_ident(pg_get_userbyid(reader.grantee)) END);
    END LOOP;
END $$;
COMMENT ON VIEW control.customer_upload_historical_coverage IS
    'Latest verified PUBLISHED app snapshot whose frozen manifest names this exact upload. Latest app users at cutoff, NOT immutable per-upload publication; NULL means no matching published evidence, not queued work. Batch history counts are frozen at snapshot creation.';
COMMIT;
