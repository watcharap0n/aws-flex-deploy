-- READ ONLY. Snapshot integrity/lineage summary, deliberately no PII output.
-- Run after 024. Rows with a mismatch require investigation, never recapture
-- from mutable identity tables. CH reconciliation must compare the worker's
-- canonical source_sha256 and both staged/published counts under this run ID.
-- PostgreSQL jsonb::text is NOT the worker's canonical JSON checksum format.
BEGIN TRANSACTION READ ONLY;
SET LOCAL statement_timeout = '60s';

SELECT t.transfer_id,t.resolution_run_id,t.stitching_job_id,t.ingestion_batch_id,
       t.publication_seq,t.workspace_id,t.app_id,t.status,t.error_code,
       t.row_count,coalesce(x.snapshot_rows,0) AS snapshot_rows,
       t.row_count=coalesce(x.snapshot_rows,0) AS count_matches,
       CASE WHEN t.row_count=0 THEN x.first_row IS NULL
            ELSE x.first_row=1 AND x.last_row=t.row_count END AS contiguous_rows,
       coalesce(x.invalid_keys,0) AS invalid_keys,
       coalesce(x.invalid_lineage,0) AS invalid_lineage,
       t.source_sha256,t.published_at,t.attempts,
       r.status AS resolution_status,j.status AS stitching_status
FROM control.customer_identity_transfer t
JOIN identity.resolution_run r ON r.run_id=t.resolution_run_id
JOIN control.stitching_job j ON j.job_id=t.stitching_job_id
LEFT JOIN LATERAL (
    SELECT count(*) AS snapshot_rows,min(e.row_number) AS first_row,max(e.row_number) AS last_row,
        count(*) FILTER (WHERE btrim(e.app_id)='' OR btrim(e.user_id)=''
            OR e.app_id IS DISTINCT FROM t.app_id
            OR e.payload->>'app_id' IS DISTINCT FROM e.app_id
            OR e.payload->>'user_id' IS DISTINCT FROM e.user_id
            OR e.payload#>>'{profile,customer_id}' IS DISTINCT FROM e.user_id) AS invalid_keys,
        count(*) FILTER (WHERE e.payload->>'resolution_run_id' IS DISTINCT FROM t.resolution_run_id::text
            OR e.payload->>'workspace_id' IS DISTINCT FROM t.workspace_id::text
            OR nullif(e.payload->>'customer_entity_id','') IS NULL) AS invalid_lineage
    FROM control.customer_identity_export e WHERE e.transfer_id=t.transfer_id
) x ON true
ORDER BY t.publication_seq;

-- No result rows expected. Detect any corruption even if constraints disabled.
SELECT transfer_id,app_id,count(*) AS duplicate_key_groups
FROM (SELECT transfer_id,app_id,user_id FROM control.customer_identity_export
      GROUP BY transfer_id,app_id,user_id HAVING count(*)>1) d
GROUP BY transfer_id,app_id;

-- Successful stitching must not imply public completion. Missing/blocked
-- capture is explicit and retry means a NEW stitch, never changing frozen scope.
SELECT j.job_id AS stitching_job_id,j.ingestion_batch_id,r.run_id AS resolution_run_id,
       coalesce(i.error_code,'MISSING_CAPTURE') AS error_code,j.completed_at
FROM control.stitching_job j
LEFT JOIN identity.resolution_run r ON r.stitching_job_id=j.job_id
LEFT JOIN control.customer_identity_transfer t ON t.stitching_job_id=j.job_id
LEFT JOIN control.customer_identity_capture_issue i ON i.stitching_job_id=j.job_id
WHERE j.status='SUCCEEDED' AND t.transfer_id IS NULL
  AND NOT EXISTS (SELECT 1 FROM control.customer_identity_connector_lineage(ARRAY[j.ingestion_batch_id]))
ORDER BY j.completed_at,j.job_id;

SELECT status,error_code,count(*) AS transfer_count,sum(row_count) AS snapshot_rows,
       min(created_at) AS oldest_capture
FROM control.customer_identity_transfer
GROUP BY status,error_code ORDER BY status,error_code;
COMMIT;
