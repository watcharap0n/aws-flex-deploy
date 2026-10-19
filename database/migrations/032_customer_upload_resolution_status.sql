-- Manual additive projection; install 031 -> 033 -> 032 after 025/027/028.
-- Optional 029 evidence is preserved. No execution, transfer, history, capture,
-- or source writes. Parent owns rollout; numeric filename order is NOT sufficient.
BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='120s';

-- Rank intent once. Aggregate frozen manifest metadata set-wise, never scan raw
-- history per status row. The installed 027+031 finalizers validate its evidence.
CREATE VIEW control.customer_upload_resolution_status AS
WITH latest AS (
    SELECT DISTINCT ON (stitching_job_id) *
    FROM control.customer_upload_resolution_attempt
    ORDER BY stitching_job_id,attempt DESC
), boundary AS (
    SELECT DISTINCT ON (run_id) run_id,batch_id,batch_created_at
    FROM control.customer_app_backfill_batch
    ORDER BY run_id,batch_created_at DESC,batch_id DESC
), linked AS (
    SELECT a.ingestion_batch_id,a.stitching_job_id,a.run_id,
        a.attempt AS scoped_resolution_attempt,
        -- A committed successful ledger wins even after a lost client ACK.
        -- A terminal/transient journal wins over a missing or BUILDING ledger.
        CASE WHEN r.status IN ('SUCCEEDED','FAILED') THEN r.status
            ELSE coalesce(continuation.status,r.status,'PENDING') END AS scoped_resolution_status,
        CASE WHEN r.status IN ('SUCCEEDED','FAILED') THEN r.error_code
            ELSE coalesce(continuation.error_code,r.error_code) END AS scoped_resolution_error_code,
        continuation.status AS scoped_resolution_continuation_status,
        continuation.retry_count AS scoped_resolution_retry_count,
        continuation.next_attempt_at AS scoped_resolution_next_attempt_at,
        r.cutoff AS scoped_resolution_cutoff,
        r.input_sha256 AS scoped_resolution_input_sha256,
        r.expected_users AS scoped_resolution_app_user_count,
        boundary.batch_id AS scoped_resolution_last_input_batch_id,
        boundary.batch_created_at AS scoped_resolution_last_input_created_at,
        m.expected_members AS scoped_resolution_selected_expected_members,
        m.available_observations AS scoped_resolution_selected_available_observations,
        m.expected_members-m.available_observations AS scoped_resolution_selected_missing_observations,
        coalesce(m.batch_created_at=b.created_at AND m.batch_created_at<=r.cutoff
            AND m.expected_members=m.available_observations
            AND m.expected_members=j.selected_record_count,false) AS scoped_resolution_selected_coverage_complete,
        coalesce(r.app_id=a.app_id AND scope.workspace_id=r.workspace_id
            AND rr.stitching_job_id IS NULL AND rr.run_id IS NOT NULL,false) AS bound_resolution,
        rr.status AS resolution_status,
        b.status AS ingestion_status,j.status AS stitching_status,
        coalesce(b.records_rejected,0)>0 OR j.ready_record_count<j.standardized_record_count AS has_warnings,
        t.transfer_id,t.status AS transfer_status,t.workspace_id AS transfer_workspace_id,
        t.app_id AS transfer_app_id,t.publication_seq AS transfer_publication_seq,
        t.published_at AS transfer_published_at,t.error_code AS transfer_error_code,
        t.updated_at AS transfer_updated_at,t.source_sha256,
        greatest(a.created_at,r.created_at,r.completed_at,t.updated_at,continuation.updated_at) AS effective_publication_updated_at
    FROM latest a
    JOIN control.stitching_job j ON j.job_id=a.stitching_job_id AND j.ingestion_batch_id=a.ingestion_batch_id
    JOIN control.ingestion_batch b ON b.batch_id=a.ingestion_batch_id AND b.app_id=a.app_id
    LEFT JOIN control.customer_app_backfill_run r ON r.run_id=a.run_id
    LEFT JOIN control.customer_upload_resolution_attempt_status continuation ON continuation.run_id=a.run_id
    LEFT JOIN identity.resolution_run rr ON rr.run_id=a.run_id
    LEFT JOIN control.customer_identity_upload_scope scope ON scope.app_id=a.app_id
    LEFT JOIN control.customer_app_backfill_batch m ON m.run_id=a.run_id AND m.batch_id=a.ingestion_batch_id
    LEFT JOIN boundary ON boundary.run_id=a.run_id
    LEFT JOIN control.customer_identity_transfer t ON t.backfill_run_id=a.run_id
        AND t.transfer_id=a.run_id AND t.resolution_run_id=a.run_id
        AND t.stitching_job_id IS NULL AND t.ingestion_batch_id IS NULL
        AND t.app_id=a.app_id AND t.app_id=r.app_id
        AND t.workspace_id=r.workspace_id AND t.workspace_id=scope.workspace_id
        AND t.row_count=r.expected_users
    WHERE NOT control.customer_publication_is_connector(b.batch_id)
)
SELECT ingestion_batch_id,stitching_job_id,run_id,
    scoped_resolution_attempt,scoped_resolution_status,scoped_resolution_error_code,
    scoped_resolution_continuation_status,scoped_resolution_retry_count,scoped_resolution_next_attempt_at,
    scoped_resolution_cutoff,scoped_resolution_input_sha256,scoped_resolution_app_user_count,
    scoped_resolution_last_input_batch_id,scoped_resolution_last_input_created_at,
    scoped_resolution_selected_expected_members,scoped_resolution_selected_available_observations,
    scoped_resolution_selected_missing_observations,scoped_resolution_selected_coverage_complete,
    transfer_id,transfer_status,transfer_workspace_id,transfer_app_id,transfer_publication_seq,
    transfer_published_at,transfer_error_code,transfer_updated_at,effective_publication_updated_at,
    CASE
        WHEN ingestion_status='failed' OR stitching_status='FAILED' THEN 'FAILED'
        WHEN ingestion_status IS DISTINCT FROM 'completed' THEN 'INGESTING'
        WHEN stitching_status IS DISTINCT FROM 'SUCCEEDED' THEN 'STITCHING'
        WHEN scoped_resolution_status='PENDING' THEN 'SCOPED_RESOLUTION_PENDING'
        WHEN scoped_resolution_status='FAILED' THEN 'SCOPED_RESOLUTION_FAILED'
        WHEN scoped_resolution_status='RETRY_WAIT' THEN 'SCOPED_RESOLUTION_RETRY_WAIT'
        WHEN scoped_resolution_status='RUNNING' THEN 'SCOPED_RESOLVING'
        WHEN NOT bound_resolution THEN 'SCOPED_RESOLUTION_BLOCKED'
        WHEN resolution_status='FAILED' THEN 'SCOPED_RESOLUTION_FAILED'
        WHEN scoped_resolution_status='BUILDING' THEN 'SCOPED_RESOLVING'
        WHEN resolution_status IS DISTINCT FROM 'SUCCEEDED' THEN 'SCOPED_RESOLUTION_BLOCKED'
        WHEN NOT scoped_resolution_selected_coverage_complete THEN 'TRANSFER_BLOCKED'
        WHEN transfer_status='PUBLISHED' AND transfer_published_at IS NOT NULL AND source_sha256 IS NOT NULL
            THEN CASE WHEN has_warnings THEN 'COMPLETED_WITH_WARNINGS' ELSE 'COMPLETED' END
        WHEN transfer_status='PUBLISHED' THEN 'TRANSFER_BLOCKED'
        WHEN transfer_status='RUNNING' THEN 'TRANSFERRING'
        WHEN transfer_status='FAILED' THEN 'TRANSFER_FAILED'
        WHEN transfer_status='BLOCKED' THEN 'TRANSFER_BLOCKED'
        ELSE 'TRANSFER_PENDING'
    END::text AS overall_status,
    CASE
        WHEN scoped_resolution_status='PENDING' THEN NULL
        WHEN scoped_resolution_status IN ('FAILED','RETRY_WAIT','RUNNING') THEN scoped_resolution_error_code
        WHEN NOT bound_resolution THEN 'SCOPED_RESOLUTION_BINDING_MISMATCH'
        WHEN scoped_resolution_status='SUCCEEDED' AND resolution_status IS DISTINCT FROM 'SUCCEEDED'
            THEN 'SCOPED_RESOLUTION_NOT_SUCCEEDED'
        WHEN scoped_resolution_status='SUCCEEDED' AND NOT scoped_resolution_selected_coverage_complete
            THEN 'SELECTED_UPLOAD_HISTORY_INCOMPLETE'
        WHEN transfer_status='PUBLISHED' AND (transfer_published_at IS NULL OR source_sha256 IS NULL)
            THEN 'PUBLISHED_TRANSFER_UNVERIFIED'
        ELSE transfer_error_code
    END::text AS effective_publication_error_code
FROM linked;

-- Capture installed 025 definition rather than duplicate its execution, retry,
-- exclusion and capture-issue rules. Keep OID/owner/ACL/column positions.
DO $$
DECLARE definition text; projection text;
BEGIN
    definition:=rtrim(pg_get_viewdef('control.customer_publication_status'::regclass,true),E';\n ');
    SELECT string_agg(CASE
        WHEN a.attname='overall_status' THEN
            'CASE WHEN s.run_id IS NOT NULL AND p.stitching_status=''SUCCEEDED'' '
            || 'AND p.overall_status NOT IN (''FAILED'',''INGESTING'') THEN s.overall_status ELSE p.overall_status END AS overall_status'
        WHEN a.attname IN ('transfer_id','transfer_status','transfer_workspace_id','transfer_app_id',
            'transfer_publication_seq','transfer_published_at','transfer_error_code','transfer_updated_at') THEN
            format('CASE WHEN s.run_id IS NOT NULL THEN s.%I ELSE p.%I END AS %I',a.attname,a.attname,a.attname)
        ELSE format('p.%I',a.attname) END,', ' ORDER BY a.attnum)
    INTO projection FROM pg_attribute a WHERE a.attrelid='control.customer_publication_status'::regclass
        AND a.attnum>0 AND NOT a.attisdropped;
    EXECUTE 'CREATE OR REPLACE VIEW control.customer_publication_status AS SELECT '||projection
        || ', CASE WHEN s.run_id IS NOT NULL THEN ''SCOPED_UPLOAD_RESOLUTION'' ELSE ''GLOBAL_STITCHING'' END::text AS effective_publication_origin'
        || ', coalesce(s.run_id,p.resolution_run_id) AS effective_publication_resolution_run_id'
        || ', CASE WHEN s.run_id IS NOT NULL THEN ''LATEST_APP_USERS_AT_CUTOFF'' ELSE ''ORIGINAL_STITCHING_SNAPSHOT'' END::text AS effective_publication_scope'
        || ', s.scoped_resolution_attempt,s.scoped_resolution_status,s.scoped_resolution_error_code'
        || ', s.scoped_resolution_continuation_status,s.scoped_resolution_retry_count,s.scoped_resolution_next_attempt_at'
        || ', s.scoped_resolution_cutoff,s.scoped_resolution_input_sha256,s.scoped_resolution_app_user_count'
        || ', s.scoped_resolution_last_input_batch_id,s.scoped_resolution_last_input_created_at'
        || ', s.scoped_resolution_selected_expected_members,s.scoped_resolution_selected_available_observations'
        || ', s.scoped_resolution_selected_missing_observations,s.scoped_resolution_selected_coverage_complete'
        || ', CASE WHEN s.run_id IS NOT NULL THEN s.effective_publication_error_code ELSE p.transfer_error_code END AS effective_publication_error_code'
        || ', greatest(s.effective_publication_updated_at,p.transfer_updated_at) AS effective_publication_updated_at'
        || ', p.transfer_id AS original_transfer_id,p.transfer_status AS original_transfer_status'
        || ', p.transfer_error_code AS original_transfer_error_code'
        || ' FROM ('||definition||') p LEFT JOIN control.customer_upload_resolution_status s'
        || ' ON s.ingestion_batch_id=p.ingestion_batch_id AND s.stitching_job_id=p.stitching_job_id';
END $$;

DO $$
DECLARE definition text; projection text; extras text;
BEGIN
    definition:=rtrim(pg_get_viewdef('control.customer_pipeline_status'::regclass,true),E';\n ');
    SELECT string_agg(CASE WHEN a.attname='updated_at' THEN
        'greatest(p.updated_at,t.effective_publication_updated_at) AS updated_at'
        ELSE format('p.%I',a.attname) END,', ' ORDER BY a.attnum)
    INTO projection FROM pg_attribute a WHERE a.attrelid='control.customer_pipeline_status'::regclass
        AND a.attnum>0 AND NOT a.attisdropped;
    SELECT string_agg(format('t.%I',a.attname),', ' ORDER BY a.attnum)
    INTO extras FROM pg_attribute a WHERE a.attrelid='control.customer_publication_status'::regclass
        AND a.attnum>0 AND NOT a.attisdropped
        AND (a.attname LIKE 'effective_publication_%' OR a.attname LIKE 'scoped_resolution_%'
            OR a.attname IN ('original_transfer_id','original_transfer_status','original_transfer_error_code'));
    EXECUTE 'CREATE OR REPLACE VIEW control.customer_pipeline_status AS SELECT '||projection||', '||extras
        ||' FROM ('||definition||') p LEFT JOIN control.customer_publication_status t'
        ||' ON t.ingestion_batch_id=p.ingestion_batch_id AND t.stitching_job_id=p.stitching_job_id'
        ||' AND t.resolution_run_id IS NOT DISTINCT FROM p.resolution_run_id';
END $$;

-- Existing readers/owners of either public seam only receive read access.
DO $$
DECLARE reader record;
BEGIN
    FOR reader IN
        SELECT DISTINCT a.grantee FROM pg_class c,
            LATERAL aclexplode(coalesce(c.relacl,acldefault('r',c.relowner))) a
        WHERE c.oid IN ('control.customer_pipeline_status'::regclass,'control.customer_publication_status'::regclass)
            AND a.privilege_type='SELECT'
        UNION SELECT relowner FROM pg_class WHERE oid IN
            ('control.customer_pipeline_status'::regclass,'control.customer_publication_status'::regclass)
    LOOP
        EXECUTE format('GRANT SELECT ON control.customer_upload_resolution_status TO %s',
            CASE WHEN reader.grantee=0 THEN 'PUBLIC' ELSE quote_ident(pg_get_userbyid(reader.grantee)) END);
        EXECUTE format('GRANT EXECUTE ON FUNCTION control.customer_publication_is_connector(uuid) TO %s',
            CASE WHEN reader.grantee=0 THEN 'PUBLIC' ELSE quote_ident(pg_get_userbyid(reader.grantee)) END);
    END LOOP;
END $$;
COMMENT ON VIEW control.customer_upload_resolution_status IS
    'Independent upload continuation, latest intent per original job. Terminal preledger journal is not a fabricated resolution. Completion requires bound successful app ledger and PUBLISHED transfer with exact frozen selected-upload coverage.';
COMMENT ON COLUMN control.customer_publication_status.resolution_run_id IS
    'Original global stitching resolution; never relabeled as the independent scoped publication resolution.';
COMMENT ON COLUMN control.customer_publication_status.effective_publication_scope IS
    'LATEST_APP_USERS_AT_CUTOFF is app latest-state publication, not every historical upload observation. App user counts repeat across uploads and must not be summed.';
COMMENT ON COLUMN control.customer_pipeline_status.effective_publication_resolution_run_id IS
    'Effective transfer resolution, or the pinned future scoped intent UUID before its genuine resolution exists. Original resolution_run_id is unchanged.';
COMMENT ON COLUMN control.customer_pipeline_status.scoped_resolution_last_input_batch_id IS
    'Frozen last manifest input ordered by (batch_created_at,batch_id), not the current latest app upload. Read together with scoped_resolution_last_input_created_at and cutoff.';
COMMIT;
