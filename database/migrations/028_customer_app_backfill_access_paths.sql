-- MANUAL: requires the exact 027 finalizer, or this already-installed 028 body.
-- Preserve 027 unchanged. Only two validation access paths change; all gates,
-- counts, payload checks, locks and atomic resolution/export/outbox writes remain.
-- EXCEPT compares the same four non-null winner/ledger keys. The separate raw
-- LEFT JOIN still rejects missing observations and wrong-app history; existing
-- winner duplicate-user and record total checks retain multiplicity validation.
-- Endpoint LIMIT 1 fences correlated lookups on the existing UNIQUE
-- (run_id,(input_payload->>'record_id')) index: no duplicates can be hidden.
-- No new indexes, grants, ANALYZE, runtime settings, image or current-table writes.
-- CREATE OR REPLACE preserves the existing function OID, owner and ACL.
BEGIN;
SET LOCAL lock_timeout = '5s';

DO $precondition$
BEGIN
    IF NOT EXISTS (
        SELECT FROM pg_catalog.pg_proc p
        WHERE p.oid=pg_catalog.to_regprocedure('control.finalize_customer_app_backfill()')
            AND pg_catalog.md5(p.prosrc) IN ('b53dd3139ebdcb2dc0ca038b8d4d1381', 'e19dedc9a726ab641c8006bd61576126')
            AND p.prorettype='pg_catalog.trigger'::pg_catalog.regtype
            AND p.prosecdef
            AND p.proconfig=ARRAY['search_path=pg_catalog, pg_temp']::text[]
    ) THEN
        RAISE EXCEPTION '028 requires the exact 027 or 028 customer app backfill finalizer';
    END IF;
END $precondition$;

CREATE OR REPLACE FUNCTION control.finalize_customer_app_backfill()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
DECLARE r identity.resolution_run%ROWTYPE; n bigint; entities bigint;
BEGIN
    IF OLD.status=NEW.status THEN RETURN NEW; END IF;
    IF NEW.status='FAILED' THEN
        UPDATE identity.resolution_run SET status='FAILED',completed_at=NEW.completed_at,
            error_message=NEW.error_code WHERE run_id=NEW.run_id AND status='RUNNING' AND stitching_job_id IS NULL;
        IF NOT FOUND THEN RAISE EXCEPTION 'BACKFILL_INVALID_RESOLUTION'; END IF;
        RETURN NEW;
    END IF;
    IF NEW.status<>'SUCCEEDED' THEN RETURN NEW; END IF;
    -- Same commit-order lock as live 024 capture. No new publication may slip
    -- between the stale-input check and this transaction's generated sequence.
    PERFORM pg_advisory_xact_lock(7413220179::bigint);
    LOCK TABLE raw.customer_batch_member,raw.customer,control.ingestion_batch,
        control.source_landing_batch,control.source_system,control.customer_identity_upload_scope IN SHARE MODE NOWAIT;
    IF to_regclass('connector.sync_run') IS NOT NULL THEN
        EXECUTE 'LOCK TABLE connector.sync_run IN SHARE MODE NOWAIT';
    END IF;
    SELECT * INTO STRICT r FROM identity.resolution_run WHERE run_id=NEW.run_id FOR UPDATE;
    IF r.status<>'RUNNING' OR r.completed_at IS NOT NULL OR r.stitching_job_id IS NOT NULL THEN
        RAISE EXCEPTION 'BACKFILL_INVALID_RESOLUTION';
    END IF;
    -- Include every outbox state, not only PUBLISHED: queued/running/failed work
    -- can still publish. A different app does not invalidate this manifest.
    IF EXISTS(SELECT FROM control.customer_identity_transfer t WHERE t.app_id=NEW.app_id
        AND t.publication_seq>NEW.baseline_publication_seq) THEN
        RAISE EXCEPTION 'BACKFILL_STALE_PUBLICATION';
    END IF;
    -- 026 physical batch ownership is authoritative. Compare the full frozen
    -- manifest against all app batches at cutoff; never silently narrow to raw
    -- rows that survived retention, a connector, or global current profiles.
    IF EXISTS(
        SELECT FROM control.customer_app_backfill_batch m
        JOIN control.ingestion_batch b USING(batch_id)
        WHERE m.run_id=NEW.run_id AND (b.app_id IS DISTINCT FROM NEW.app_id
            OR b.status IS DISTINCT FROM 'completed'
            OR b.created_at>NEW.cutoff OR b.created_at IS DISTINCT FROM m.batch_created_at)
    ) OR EXISTS(
        SELECT FROM control.ingestion_batch b WHERE b.app_id=NEW.app_id AND b.created_at<=NEW.cutoff AND b.status='completed'
        AND NOT EXISTS(SELECT FROM control.customer_app_backfill_batch m WHERE m.run_id=NEW.run_id AND m.batch_id=b.batch_id)
    ) OR EXISTS(SELECT FROM control.customer_identity_connector_lineage(ARRAY(
        SELECT batch_id FROM control.customer_app_backfill_batch WHERE run_id=NEW.run_id))) THEN
        RAISE EXCEPTION 'BACKFILL_MANIFEST_SCOPE';
    END IF;
    IF EXISTS(
        SELECT FROM control.customer_app_backfill_batch b
        LEFT JOIN raw.customer_batch_member m ON m.ingestion_batch_id=b.batch_id
        LEFT JOIN raw.customer h ON (h.batch_id,h.raw_customer_id,h.source_system_id,h.source_customer_id)=
            (m.ingestion_batch_id,m.raw_customer_id,m.source_system_id,m.source_customer_id)
        WHERE b.run_id=NEW.run_id GROUP BY b.batch_id,b.expected_members,b.available_observations
        HAVING count(m.raw_customer_id)<>b.expected_members OR count(h.raw_customer_id)<>b.available_observations
    ) THEN RAISE EXCEPTION 'BACKFILL_COVERAGE_CHANGED'; END IF;
    -- Membership, not available raw, selects the latest winner. Older gaps are
    -- allowed and remain visible in immutable batch coverage. Latest gaps fail.
    WITH keys AS MATERIALIZED (
        SELECT m.*,b.batch_created_at,ib.source_system_id AS batch_source,
            row_number() OVER(PARTITION BY m.source_system_id,m.source_customer_id
                ORDER BY b.batch_created_at DESC,b.batch_id DESC) AS priority
        FROM control.customer_app_backfill_batch b
        JOIN control.ingestion_batch ib USING(batch_id)
        JOIN raw.customer_batch_member m ON m.ingestion_batch_id=b.batch_id
        WHERE b.run_id=NEW.run_id
    ), winners AS MATERIALIZED (SELECT * FROM keys WHERE priority=1)
    SELECT CASE WHEN
        (SELECT count(*) FROM winners)<>NEW.expected_users
        OR EXISTS(SELECT FROM keys WHERE source_customer_id='' OR source_customer_id<>btrim(source_customer_id)
            OR source_system_id<>batch_source)
        OR EXISTS(SELECT FROM winners GROUP BY source_customer_id HAVING count(*)<>1)
        OR EXISTS(SELECT FROM winners w LEFT JOIN raw.customer h
            ON (h.batch_id,h.raw_customer_id,h.source_system_id,h.source_customer_id)=
                (w.ingestion_batch_id,w.raw_customer_id,w.source_system_id,w.source_customer_id)
            WHERE h.raw_customer_id IS NULL OR h.app_id IS DISTINCT FROM NEW.app_id)
        OR EXISTS(SELECT ingestion_batch_id,raw_customer_id,source_system_id,source_customer_id FROM winners
            EXCEPT SELECT source_batch_id,raw_customer_id,source_system_id,user_id
            FROM control.customer_app_backfill_record WHERE run_id=NEW.run_id)
        THEN -1 ELSE (SELECT count(*) FROM winners) END INTO n;
    IF n<0 THEN RAISE EXCEPTION 'BACKFILL_LATEST_INPUT_INCOMPLETE'; END IF;
    SELECT count(*),count(DISTINCT payload->>'customer_entity_id') INTO n,entities
        FROM control.customer_app_backfill_record WHERE run_id=NEW.run_id;
    IF n<>NEW.expected_users OR EXISTS(
        SELECT FROM (SELECT p.*,row_number() OVER(ORDER BY user_id COLLATE "C") AS expected_row
            FROM control.customer_app_backfill_record p WHERE run_id=NEW.run_id) p
        WHERE p.row_number<>p.expected_row OR p.app_id<>NEW.app_id
            OR p.payload->>'app_id' IS DISTINCT FROM NEW.app_id
            OR p.payload->>'user_id' IS DISTINCT FROM p.user_id
            OR p.payload#>>'{profile,customer_id}' IS DISTINCT FROM p.user_id
            OR p.payload->>'resolution_run_id' IS DISTINCT FROM NEW.run_id::text
            OR p.payload->>'workspace_id' IS DISTINCT FROM NEW.workspace_id::text
            OR p.payload->>'raw_customer_id' IS DISTINCT FROM p.raw_customer_id::text
            OR p.payload->>'source_system_id' IS DISTINCT FROM p.source_system_id::text
            OR (p.payload ? 'source_batch_id' AND p.payload->>'source_batch_id' IS DISTINCT FROM p.source_batch_id::text)
            OR p.input_payload#>>'{raw,batch_id}' IS DISTINCT FROM p.source_batch_id::text
            OR p.input_payload#>>'{raw,raw_customer_id}' IS DISTINCT FROM p.raw_customer_id::text
            OR p.input_payload#>>'{raw,source_system_id}' IS DISTINCT FROM p.source_system_id::text
            OR p.input_payload#>>'{raw,source_customer_id}' IS DISTINCT FROM p.user_id
            OR p.input_payload#>>'{raw,app_id}' IS DISTINCT FROM NEW.app_id
            OR jsonb_typeof(p.input_payload->'normalized') IS DISTINCT FROM 'object'
            OR coalesce(p.input_payload->>'record_id','') !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$'
            OR coalesce(p.payload->>'customer_entity_id','') !~ '^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$'
            -- Historical run-local IDs are input evidence, never global staging IDs.
            OR p.payload->>'standardized_record_id' IS NOT NULL
            OR CASE WHEN jsonb_typeof(p.payload->'match_probability')='number'
                THEN (p.payload->>'match_probability')::numeric NOT BETWEEN 0 AND 1 ELSE true END
            OR coalesce(btrim(p.payload->>'match_method'),'')=''
    ) THEN RAISE EXCEPTION 'BACKFILL_INVALID_PAYLOAD'; END IF;
    IF EXISTS(SELECT FROM control.customer_app_backfill_pair p
        LEFT JOIN LATERAL (
            SELECT x.run_id,x.payload FROM control.customer_app_backfill_record x
            WHERE x.run_id=p.run_id AND x.input_payload->>'record_id'=p.left_record_id::text
            LIMIT 1
        ) l ON true
        LEFT JOIN LATERAL (
            SELECT x.run_id,x.payload FROM control.customer_app_backfill_record x
            WHERE x.run_id=p.run_id AND x.input_payload->>'record_id'=p.right_record_id::text
            LIMIT 1
        ) rhs ON true
        WHERE p.run_id=NEW.run_id AND (l.run_id IS NULL OR rhs.run_id IS NULL
            OR l.payload->>'customer_entity_id' IS DISTINCT FROM rhs.payload->>'customer_entity_id')) THEN
        RAISE EXCEPTION 'BACKFILL_INVALID_PAIR';
    END IF;
    IF r.source_record_count<>n OR r.selected_record_count<>n OR r.standardized_record_count<>n
        -- candidate_pair_count counts probabilistic blockers only; accepted
        -- deterministic edges can legitimately exceed that count.
        OR r.entity_count<>entities OR r.candidate_pair_count<0
        OR r.accepted_pair_count<>(SELECT count(*) FROM control.customer_app_backfill_pair WHERE run_id=NEW.run_id)
        OR r.accepted_pair_count<0 OR r.error_message IS NOT NULL
        OR btrim(r.algorithm)='' OR btrim(r.normalization_version)='' THEN
        RAISE EXCEPTION 'BACKFILL_INCOMPLETE_REPORT';
    END IF;
    UPDATE identity.resolution_run SET status='SUCCEEDED',completed_at=NEW.completed_at WHERE run_id=NEW.run_id;
    INSERT INTO control.customer_identity_transfer(transfer_id,resolution_run_id,backfill_run_id,
        stitching_job_id,ingestion_batch_id,workspace_id,app_id,status,row_count)
        VALUES(NEW.run_id,NEW.run_id,NEW.run_id,NULL,NULL,NEW.workspace_id,NEW.app_id,'PENDING',n);
    -- Nested below the status trigger: satisfies 024's unchanged export guard.
    INSERT INTO control.customer_identity_export(transfer_id,row_number,app_id,user_id,payload)
        SELECT run_id,row_number,app_id,user_id,payload FROM control.customer_app_backfill_record
        WHERE run_id=NEW.run_id ORDER BY row_number;
    RETURN NEW;
END $$;

COMMIT;
