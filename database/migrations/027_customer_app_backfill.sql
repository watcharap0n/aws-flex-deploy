-- Additive historical app resolution. Requires 019/022/024/025/026.
-- MANUAL installation; no automatic backfill, current identity writes or view changes.
-- Register a real RUNNING identity.resolution_run (no stitching_job_id), then the
-- BUILDING manifest/records. Set genuine resolution report counts while RUNNING,
-- then SELECT control.finish_customer_app_backfill(run_id) in the same transaction.
-- Finalization, resolution success, immutable snapshot and PENDING outbox commit
-- together. Failures propagate and roll back: never label partial work successful.
-- The input SHA256 is the trusted Python canonical input checksum, NOT jsonb::text.
-- All new objects are private even under inherited named-role default ACLs.
-- Publication workers retain only their existing 024 column-scoped outbox grants.
-- Dedicated trusted backfill role (not the publication worker), explicit grants:
-- USAGE ON SCHEMA control,raw,identity;
-- SELECT ON raw.customer,raw.customer_batch_member,control.ingestion_batch,
--   control.source_landing_batch,control.source_system,
--   control.customer_identity_upload_scope,control.customer_identity_transfer,
--   identity.resolution_run;
-- SELECT,INSERT ON control.customer_app_backfill_run,
--   control.customer_app_backfill_batch,control.customer_app_backfill_record,
--   control.customer_app_backfill_pair;
-- UPDATE(status,error_code) ON control.customer_app_backfill_run;
-- INSERT ON identity.resolution_run;
-- UPDATE(source_record_count,selected_record_count,standardized_record_count,
--   candidate_pair_count,accepted_pair_count,entity_count,metrics)
--   ON identity.resolution_run;
-- EXECUTE ON FUNCTION control.finish_customer_app_backfill(uuid);
-- EXECUTE ON FUNCTION control.customer_app_scope_status(uuid);
-- INSERT(app_id,workspace_id) ON control.customer_identity_upload_scope;
-- Do NOT grant resolution status/completed_at UPDATE, current identity writes,
-- schema CREATE or export INSERT. Scope registration is restricted to this
-- trusted backfill role, never the publication worker. Only the fixed-path
-- finalization trigger is SECURITY DEFINER among the new 027 functions.
-- This trusted role writes genuine resolution reports; do not share with callers
-- who may edit other runs' report counts (existing resolution_run has no RLS).
BEGIN;
SET LOCAL lock_timeout = '5s';

DO $$ BEGIN
    IF to_regprocedure('control.backfill_customer_app_id(regclass,uuid,uuid,integer)') IS NULL
       OR NOT EXISTS(SELECT FROM pg_attribute WHERE attrelid='control.ingestion_batch'::regclass
           AND attname='app_id' AND atttypid='text'::regtype AND NOT attisdropped) THEN
        RAISE EXCEPTION '027 requires physical app provenance migration 026';
    END IF;
END $$;

CREATE TABLE control.customer_app_backfill_run (
    run_id uuid PRIMARY KEY REFERENCES identity.resolution_run(run_id),
    app_id text NOT NULL CHECK (app_id ~ '^[0-9]+$'),
    workspace_id uuid NOT NULL REFERENCES control.customer_identity_upload_scope(workspace_id),
    status text NOT NULL DEFAULT 'BUILDING' CHECK (status IN ('BUILDING','SUCCEEDED','FAILED')),
    expected_users bigint NOT NULL CHECK (expected_users >= 0),
    input_sha256 text NOT NULL CHECK (input_sha256 ~ '^[0-9a-f]{64}$'),
    cutoff timestamptz NOT NULL,
    baseline_publication_seq bigint NOT NULL CHECK (baseline_publication_seq >= 0),
    config jsonb NOT NULL CHECK (jsonb_typeof(config)='object'),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    completed_at timestamptz,
    error_code text
);
CREATE TABLE control.customer_app_backfill_batch (
    run_id uuid NOT NULL REFERENCES control.customer_app_backfill_run(run_id),
    batch_id uuid NOT NULL REFERENCES control.ingestion_batch(batch_id),
    expected_members bigint NOT NULL CHECK (expected_members >= 0),
    available_observations bigint NOT NULL CHECK (available_observations BETWEEN 0 AND expected_members),
    batch_created_at timestamptz NOT NULL,
    PRIMARY KEY(run_id,batch_id)
);
CREATE TABLE control.customer_app_backfill_record (
    run_id uuid NOT NULL REFERENCES control.customer_app_backfill_run(run_id),
    row_number bigint NOT NULL CHECK (row_number > 0),
    app_id text NOT NULL CHECK (app_id ~ '^[0-9]+$'),
    source_system_id bigint NOT NULL,
    user_id text NOT NULL CHECK (user_id <> '' AND user_id=btrim(user_id)),
    raw_customer_id uuid NOT NULL,
    source_batch_id uuid NOT NULL,
    input_payload jsonb NOT NULL CHECK (jsonb_typeof(input_payload)='object'),
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload)='object'),
    PRIMARY KEY(run_id,row_number),
    UNIQUE(run_id,app_id,user_id),
    FOREIGN KEY(run_id,source_batch_id) REFERENCES control.customer_app_backfill_batch(run_id,batch_id)
);
COMMENT ON TABLE control.customer_app_backfill_record IS
    'Immutable app-scoped historical membership/profile result. Entity UUID lives in payload, never global current identity tables.';
COMMENT ON TABLE control.customer_app_backfill_batch IS
    'Frozen coverage: expected_members minus available_observations explicitly reports older missing raw history; latest required observations must all exist.';
CREATE UNIQUE INDEX customer_app_backfill_record_internal_id ON control.customer_app_backfill_record
    (run_id,(input_payload->>'record_id'));
CREATE TABLE control.customer_app_backfill_pair (
    run_id uuid NOT NULL REFERENCES control.customer_app_backfill_run(run_id),
    left_record_id uuid NOT NULL,
    right_record_id uuid NOT NULL,
    probability double precision NOT NULL CHECK (probability BETWEEN 0 AND 1),
    method text NOT NULL CHECK (btrim(method)<>''),
    evidence jsonb NOT NULL CHECK (jsonb_typeof(evidence)='object'),
    PRIMARY KEY(run_id,left_record_id,right_record_id),
    CHECK (left_record_id<right_record_id)
);
COMMENT ON TABLE control.customer_app_backfill_pair IS
    'Trusted accepted-pair evidence over run-local input_payload.record_id UUIDs; never exported to ClickHouse or current identity.match_pair.';

ALTER TABLE control.customer_identity_transfer
    ADD COLUMN backfill_run_id uuid UNIQUE REFERENCES control.customer_app_backfill_run(run_id),
    ALTER COLUMN stitching_job_id DROP NOT NULL,
    ADD CONSTRAINT customer_identity_transfer_exclusive_origin CHECK (
        (stitching_job_id IS NOT NULL AND ingestion_batch_id IS NOT NULL AND backfill_run_id IS NULL)
        OR (stitching_job_id IS NULL AND ingestion_batch_id IS NULL AND backfill_run_id IS NOT NULL
            AND backfill_run_id=resolution_run_id AND transfer_id=backfill_run_id)
    );
-- Preserve the complete 024 body, adding only the immutable backfill reference.
CREATE OR REPLACE FUNCTION control.freeze_customer_identity_transfer_scope()
RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    IF (NEW.transfer_id,NEW.resolution_run_id,NEW.stitching_job_id,NEW.ingestion_batch_id,
        NEW.workspace_id,NEW.app_id,NEW.publication_seq,NEW.row_count,NEW.created_at,NEW.backfill_run_id)
        IS DISTINCT FROM
       (OLD.transfer_id,OLD.resolution_run_id,OLD.stitching_job_id,OLD.ingestion_batch_id,
        OLD.workspace_id,OLD.app_id,OLD.publication_seq,OLD.row_count,OLD.created_at,OLD.backfill_run_id) THEN
        RAISE EXCEPTION 'Customer identity transfer scope is immutable';
    END IF;
    IF OLD.source_sha256 IS NOT NULL AND NEW.source_sha256 IS DISTINCT FROM OLD.source_sha256 THEN
        RAISE EXCEPTION 'Customer identity snapshot checksum is immutable';
    END IF;
    IF OLD.status='BLOCKED' AND NEW.status<>'BLOCKED' THEN
        RAISE EXCEPTION 'Blocked capture requires a new stitching run';
    END IF;
    RETURN NEW;
END $$;

CREATE FUNCTION control.guard_customer_app_backfill_run()
RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $$
BEGIN
    IF TG_OP='INSERT' THEN
        IF NEW.status<>'BUILDING' OR NEW.completed_at IS NOT NULL OR NEW.error_code IS NOT NULL
            OR NOT EXISTS(SELECT FROM identity.resolution_run r WHERE r.run_id=NEW.run_id
                AND r.status='RUNNING' AND r.stitching_job_id IS NULL AND r.completed_at IS NULL)
            OR NOT EXISTS(SELECT FROM control.customer_identity_upload_scope s
                WHERE (s.app_id,s.workspace_id)=(NEW.app_id,NEW.workspace_id)) THEN
            RAISE EXCEPTION 'BACKFILL_INVALID_RUN';
        END IF;
    ELSE
        IF (to_jsonb(NEW)-ARRAY['status','completed_at','error_code']) IS DISTINCT FROM
           (to_jsonb(OLD)-ARRAY['status','completed_at','error_code'])
           OR (OLD.status<>'BUILDING' AND NEW IS DISTINCT FROM OLD) THEN
            RAISE EXCEPTION 'Customer app backfill run is immutable';
        END IF;
        IF OLD.status='BUILDING' AND NEW.status IN ('SUCCEEDED','FAILED') THEN
            NEW.completed_at := clock_timestamp();
        END IF;
        IF NEW.status='SUCCEEDED' AND NEW.error_code IS NOT NULL THEN
            RAISE EXCEPTION 'BACKFILL_INVALID_RUN';
        END IF;
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER customer_app_backfill_run_guard BEFORE INSERT OR UPDATE
    ON control.customer_app_backfill_run FOR EACH ROW EXECUTE FUNCTION control.guard_customer_app_backfill_run();
CREATE TRIGGER customer_app_backfill_run_no_delete BEFORE DELETE OR TRUNCATE
    ON control.customer_app_backfill_run FOR EACH STATEMENT EXECUTE FUNCTION control.reject_customer_identity_snapshot_mutation();

-- Statement-level parent locking: one check per run, not one lookup per user.
-- FOR SHARE serializes staging against finalization's UPDATE of the parent.
CREATE FUNCTION control.guard_customer_app_backfill_append()
RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $$
DECLARE r record;
BEGIN
    FOR r IN SELECT b.status FROM control.customer_app_backfill_run b
        WHERE b.run_id IN (SELECT DISTINCT n.run_id FROM new_rows n) FOR SHARE LOOP
        IF r.status<>'BUILDING' THEN RAISE EXCEPTION 'Customer app backfill ledger is finalized'; END IF;
    END LOOP;
    RETURN NULL;
END $$;
CREATE TRIGGER customer_app_backfill_batch_append AFTER INSERT ON control.customer_app_backfill_batch
    REFERENCING NEW TABLE AS new_rows FOR EACH STATEMENT EXECUTE FUNCTION control.guard_customer_app_backfill_append();
CREATE TRIGGER customer_app_backfill_record_append AFTER INSERT ON control.customer_app_backfill_record
    REFERENCING NEW TABLE AS new_rows FOR EACH STATEMENT EXECUTE FUNCTION control.guard_customer_app_backfill_append();
CREATE TRIGGER customer_app_backfill_pair_append AFTER INSERT ON control.customer_app_backfill_pair
    REFERENCING NEW TABLE AS new_rows FOR EACH STATEMENT EXECUTE FUNCTION control.guard_customer_app_backfill_append();
CREATE TRIGGER customer_app_backfill_batch_immutable BEFORE UPDATE OR DELETE OR TRUNCATE ON control.customer_app_backfill_batch
    FOR EACH STATEMENT EXECUTE FUNCTION control.reject_customer_identity_snapshot_mutation();
CREATE TRIGGER customer_app_backfill_record_immutable BEFORE UPDATE OR DELETE OR TRUNCATE ON control.customer_app_backfill_record
    FOR EACH STATEMENT EXECUTE FUNCTION control.reject_customer_identity_snapshot_mutation();
CREATE TRIGGER customer_app_backfill_pair_immutable BEFORE UPDATE OR DELETE OR TRUNCATE ON control.customer_app_backfill_pair
    FOR EACH STATEMENT EXECUTE FUNCTION control.reject_customer_identity_snapshot_mutation();

CREATE FUNCTION control.finalize_customer_app_backfill()
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
            LEFT JOIN control.customer_app_backfill_record p ON p.run_id=NEW.run_id
                AND (p.source_batch_id,p.raw_customer_id,p.source_system_id,p.user_id)=
                    (w.ingestion_batch_id,w.raw_customer_id,w.source_system_id,w.source_customer_id)
            WHERE h.raw_customer_id IS NULL OR h.app_id IS DISTINCT FROM NEW.app_id OR p.run_id IS NULL)
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
        LEFT JOIN control.customer_app_backfill_record l ON l.run_id=p.run_id
            AND l.input_payload->>'record_id'=p.left_record_id::text
        LEFT JOIN control.customer_app_backfill_record rhs ON rhs.run_id=p.run_id
            AND rhs.input_payload->>'record_id'=p.right_record_id::text
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
CREATE TRIGGER customer_app_backfill_finalize AFTER UPDATE OF status ON control.customer_app_backfill_run
    FOR EACH ROW EXECUTE FUNCTION control.finalize_customer_app_backfill();

CREATE FUNCTION control.finish_customer_app_backfill(p_run uuid)
RETURNS void LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $$
DECLARE state text;
BEGIN
    SELECT status INTO STRICT state FROM control.customer_app_backfill_run WHERE run_id=p_run FOR UPDATE;
    IF state='SUCCEEDED' THEN RETURN; END IF;
    IF state<>'BUILDING' THEN RAISE EXCEPTION 'BACKFILL_NOT_BUILDING'; END IF;
    UPDATE control.customer_app_backfill_run SET status='SUCCEEDED' WHERE run_id=p_run;
END $$;

-- Revoke actual inherited ACL grantees, not just PUBLIC. Do not alter defaults
-- or existing worker privileges on the preexisting export/outbox objects.
DO $private_acl$
DECLARE acl record;
BEGIN
    FOR acl IN
        SELECT DISTINCT 'TABLE' AS kind,format('%I.%I',n.nspname,c.relname) AS object_name,a.grantee
        FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        CROSS JOIN LATERAL aclexplode(coalesce(c.relacl,acldefault('r',c.relowner))) a
        WHERE c.oid IN ('control.customer_app_backfill_run'::regclass,
            'control.customer_app_backfill_batch'::regclass,'control.customer_app_backfill_record'::regclass,
            'control.customer_app_backfill_pair'::regclass)
            AND a.grantee<>c.relowner
        UNION ALL
        SELECT DISTINCT 'FUNCTION',p.oid::regprocedure::text,a.grantee FROM pg_proc p
        CROSS JOIN LATERAL aclexplode(coalesce(p.proacl,acldefault('f',p.proowner))) a
        WHERE p.oid IN ('control.guard_customer_app_backfill_run()'::regprocedure,
            'control.guard_customer_app_backfill_append()'::regprocedure,
            'control.finalize_customer_app_backfill()'::regprocedure,
            'control.finish_customer_app_backfill(uuid)'::regprocedure)
            AND a.grantee<>p.proowner
    LOOP
        EXECUTE format('REVOKE ALL PRIVILEGES ON %s %s FROM %s CASCADE',acl.kind,acl.object_name,
            CASE WHEN acl.grantee=0 THEN 'PUBLIC' ELSE quote_ident(pg_get_userbyid(acl.grantee)) END);
    END LOOP;
END $private_acl$;
COMMIT;
