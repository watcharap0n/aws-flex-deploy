-- MANUAL, forward-only historical global publication lane. Requires 019/024/026/027/
-- 028/039/041. This deliberately does not change a capture trigger, matcher,
-- current/raw/staging/identity rows or any existing transfer/export. In particular
-- a legacy P2416/BLOCKED job remains BLOCKED. Not a production deployment step.
-- Install only after catalog review, while writers are drained; grant a separate
-- trusted resolver SELECT of immutable typed observations and INSERT on new ledgers,
-- UPDATE(status) on customer_global_run and EXECUTE finish_customer_global_run.
-- Never give these mutation grants to identity_publication_worker. That worker
-- receives SELECT of the three immutable run/member/record ledgers only.
--
-- Interface: create a real RUNNING identity.resolution_run without stitching_job_id;
-- register BUILDING customer_global_run anchored to one completed selected batch.
-- Register every completed batch of target_app_id and protected_app_id at cutoff
-- in customer_global_batch. Append ONE member per latest (app,source,user),
-- including protected-app members; immutable raw/typed fingerprints come from
-- encode(digest(to_jsonb(observation)::text,'sha256'),'hex') on the pinned snapshot.
-- Append ONE customer_global_record per selected batch (app,user), with a unique
-- run-local record_id UUID linking to member.record_id. Only selected app profiles
-- are exported; protected app members are proof, not public replacement profiles.
-- Set genuine resolution_run report counts; finish within a single transaction.
-- PENDING outbox, complete member/export, and resolution success commit together.
BEGIN;
SET LOCAL lock_timeout='5s';

DO $preflight$
BEGIN
    IF to_regclass('control.customer_app_backfill_run') IS NULL
       OR to_regclass('control.customer_identity_member_export') IS NULL
       OR to_regclass('staging.customer_profile_history') IS NULL
       OR to_regprocedure('control.finish_customer_app_backfill(uuid)') IS NULL
       OR to_regprocedure('control.customer_app_scope_status(uuid)') IS NULL
       OR NOT EXISTS(SELECT FROM pg_attribute WHERE attrelid='control.customer_identity_transfer'::regclass
           AND attname='member_count' AND NOT attisdropped)
       OR NOT EXISTS(SELECT FROM pg_attribute WHERE attrelid='control.customer_identity_transfer'::regclass
           AND attname='backfill_run_id' AND NOT attisdropped)
       OR NOT EXISTS(SELECT FROM pg_constraint WHERE conrelid='control.customer_identity_transfer'::regclass
           AND conname='customer_identity_transfer_exclusive_origin' AND contype='c')
       OR to_regclass('control.customer_global_run') IS NOT NULL
       OR to_regprocedure('public.digest(text,text)') IS NULL THEN
        RAISE EXCEPTION '048 historical global baseline mismatch; do not replace legacy lane';
    END IF;
END $preflight$;

CREATE TABLE control.customer_global_run (
    run_id uuid PRIMARY KEY REFERENCES identity.resolution_run(run_id),
    target_batch_id uuid NOT NULL REFERENCES control.ingestion_batch(batch_id),
    target_app_id text NOT NULL CHECK (target_app_id ~ '^[0-9]+$'),
    protected_app_id text NOT NULL DEFAULT '1282009980020330'
        CHECK (protected_app_id='1282009980020330'),
    workspace_id uuid NOT NULL REFERENCES control.customer_identity_upload_scope(workspace_id),
    status text NOT NULL DEFAULT 'BUILDING' CHECK (status IN ('BUILDING','SUCCEEDED')),
    expected_users bigint NOT NULL CHECK (expected_users > 0),
    expected_members bigint NOT NULL CHECK (expected_members >= expected_users),
    input_sha256 text NOT NULL CHECK (input_sha256 ~ '^[0-9a-f]{64}$'),
    cutoff timestamptz NOT NULL,
    baseline_publication_seq bigint NOT NULL CHECK (baseline_publication_seq >= 0),
    config jsonb NOT NULL CHECK (jsonb_typeof(config)='object'),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    completed_at timestamptz,
    CHECK (target_app_id<>protected_app_id)
);
CREATE TABLE control.customer_global_batch (
    run_id uuid NOT NULL REFERENCES control.customer_global_run(run_id),
    batch_id uuid NOT NULL REFERENCES control.ingestion_batch(batch_id),
    app_id text NOT NULL CHECK (app_id ~ '^[0-9]+$'),
    source_system_id bigint NOT NULL,
    batch_created_at timestamptz NOT NULL,
    expected_members bigint NOT NULL CHECK (expected_members>=0),
    raw_observations bigint NOT NULL CHECK (raw_observations BETWEEN 0 AND expected_members),
    normalized_observations bigint NOT NULL CHECK (normalized_observations BETWEEN 0 AND expected_members),
    PRIMARY KEY(run_id,batch_id)
);
CREATE TABLE control.customer_global_member (
    run_id uuid NOT NULL REFERENCES control.customer_global_run(run_id),
    app_id text NOT NULL CHECK (app_id ~ '^[0-9]+$'),
    workspace_id uuid NOT NULL,
    provenance_status text NOT NULL DEFAULT 'VERIFIED_UPLOAD' CHECK (provenance_status='VERIFIED_UPLOAD'),
    source_system_id bigint NOT NULL,
    user_id text NOT NULL CHECK (user_id<>'' AND user_id=btrim(user_id)),
    raw_customer_id uuid NOT NULL,
    source_batch_id uuid NOT NULL,
    customer_entity_id uuid NOT NULL,
    match_probability numeric(8,7) NOT NULL CHECK (match_probability BETWEEN 0 AND 1),
    match_method text NOT NULL CHECK (btrim(match_method)<>''),
    record_id uuid NOT NULL,
    raw_snapshot_id uuid NOT NULL,
    typed_standardized_record_id uuid NOT NULL,
    raw_sha256 text NOT NULL CHECK (raw_sha256 ~ '^[0-9a-f]{64}$'),
    typed_sha256 text NOT NULL CHECK (typed_sha256 ~ '^[0-9a-f]{64}$'),
    PRIMARY KEY (run_id,app_id,source_system_id,user_id),
    UNIQUE(run_id,record_id),
    FOREIGN KEY(run_id,source_batch_id) REFERENCES control.customer_global_batch(run_id,batch_id)
);
CREATE INDEX customer_global_member_app_user ON control.customer_global_member(run_id,app_id,user_id)
    INCLUDE (customer_entity_id,record_id,workspace_id,raw_customer_id);
-- Accepted matcher edges are version-local proof. Each cross-app attachment
-- must have an independently verified typed anchor, not a reused raw UUID.
CREATE TABLE control.customer_global_pair (
    run_id uuid NOT NULL REFERENCES control.customer_global_run(run_id),
    left_record_id uuid NOT NULL,
    right_record_id uuid NOT NULL,
    probability numeric(8,7) NOT NULL CHECK (probability BETWEEN 0 AND 1),
    method text NOT NULL CHECK (method IN ('deterministic','splink')),
    evidence jsonb NOT NULL CHECK (jsonb_typeof(evidence)='object'),
    PRIMARY KEY(run_id,left_record_id,right_record_id),
    CHECK (left_record_id < right_record_id),
    FOREIGN KEY(run_id,left_record_id) REFERENCES control.customer_global_member(run_id,record_id),
    FOREIGN KEY(run_id,right_record_id) REFERENCES control.customer_global_member(run_id,record_id)
);
CREATE INDEX customer_global_pair_right ON control.customer_global_pair(run_id,right_record_id);
CREATE TABLE control.customer_global_record (
    run_id uuid NOT NULL REFERENCES control.customer_global_run(run_id),
    row_number bigint NOT NULL CHECK (row_number>0),
    app_id text NOT NULL CHECK (app_id ~ '^[0-9]+$'),
    source_system_id bigint NOT NULL,
    user_id text NOT NULL CHECK (user_id<>'' AND user_id=btrim(user_id)),
    raw_customer_id uuid NOT NULL,
    source_batch_id uuid NOT NULL,
    input_payload jsonb NOT NULL CHECK (jsonb_typeof(input_payload)='object'),
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload)='object'),
    PRIMARY KEY(run_id,row_number),
    UNIQUE(run_id,app_id,user_id),
    FOREIGN KEY(run_id,app_id,source_system_id,user_id)
      REFERENCES control.customer_global_member(run_id,app_id,source_system_id,user_id)
);
COMMENT ON TABLE control.customer_global_member IS
 'Version-local (app,source,user) proof, immutable batch/raw/typed fingerprints. A shared raw UUID is NEVER proof of one person.';
COMMENT ON TABLE control.customer_global_record IS
 'Selected upload app/user export only. input_payload records the typed evidence; payload is publication-worker compatible.';

-- The three origin forms are exclusive. The prior 027 constraint is replaced,
-- without updating one existing row or changing a prior transfer identifier.
ALTER TABLE control.customer_identity_transfer
    ADD COLUMN global_run_id uuid UNIQUE REFERENCES control.customer_global_run(run_id),
    DROP CONSTRAINT customer_identity_transfer_exclusive_origin,
    ADD CONSTRAINT customer_identity_transfer_exclusive_origin CHECK (
        (stitching_job_id IS NOT NULL AND ingestion_batch_id IS NOT NULL
            AND backfill_run_id IS NULL AND global_run_id IS NULL)
        OR (stitching_job_id IS NULL AND ingestion_batch_id IS NULL
            AND backfill_run_id IS NOT NULL AND global_run_id IS NULL
            AND backfill_run_id=resolution_run_id AND transfer_id=backfill_run_id)
        OR (stitching_job_id IS NULL AND ingestion_batch_id IS NULL
            AND backfill_run_id IS NULL AND global_run_id IS NOT NULL
            AND global_run_id=resolution_run_id AND transfer_id=global_run_id)
    );
-- 041 preserved transfer metadata but omitted 027's backfill_run_id. Pin the
-- exact 041 source body before extending the frozen tuple by both origin UUIDs.
DO $freeze$
DECLARE p record; definition text; prior text; replacement text;
BEGIN
    SELECT prosrc,prosecdef,proconfig,provolatile,proowner,proacl INTO p
    FROM pg_proc WHERE oid=to_regprocedure('control.freeze_customer_identity_transfer_scope()');
    prior := 'NEW.workspace_id,NEW.app_id,NEW.publication_seq,NEW.member_count,NEW.selected_row_count,NEW.created_at)';
    IF p.prosrc IS NULL OR p.prosecdef OR p.provolatile<>'v'
       OR p.proconfig IS DISTINCT FROM ARRAY['search_path=pg_catalog, pg_temp']::text[]
       OR length(p.prosrc)-length(replace(p.prosrc,prior,''))<>length(prior)
       OR length(p.prosrc)-length(replace(p.prosrc,replace(prior,'NEW.','OLD.'),''))<>length(prior)
       OR strpos(p.prosrc,'OLD.row_count=0 AND OLD.status=''PENDING''')=0 THEN
        RAISE EXCEPTION '048 frozen transfer baseline drift; do not replace';
    END IF;
    replacement := 'NEW.workspace_id,NEW.app_id,NEW.publication_seq,NEW.member_count,NEW.selected_row_count,NEW.backfill_run_id,NEW.global_run_id,NEW.created_at)';
    definition := pg_get_functiondef(to_regprocedure('control.freeze_customer_identity_transfer_scope()'));
    definition := replace(definition,prior,replacement);
    definition := replace(definition,replace(prior,'NEW.','OLD.'),replace(replacement,'NEW.','OLD.'));
    EXECUTE definition;
END $freeze$;

CREATE FUNCTION control.guard_customer_global_run()
RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $body$
DECLARE seq bigint;
BEGIN
    IF TG_OP='INSERT' THEN
        PERFORM pg_advisory_xact_lock(7413220179::bigint);
        SELECT coalesce(max(publication_seq),0) INTO seq FROM control.customer_identity_transfer;
        IF NEW.status<>'BUILDING' OR NEW.completed_at IS NOT NULL
           OR seq<>NEW.baseline_publication_seq
           OR NOT EXISTS(SELECT FROM identity.resolution_run r WHERE r.run_id=NEW.run_id
                AND r.status='RUNNING' AND r.stitching_job_id IS NULL AND r.completed_at IS NULL)
           OR NOT EXISTS(SELECT FROM control.customer_identity_upload_scope s
                WHERE (s.app_id,s.workspace_id)=(NEW.target_app_id,NEW.workspace_id))
           OR NOT EXISTS(SELECT FROM control.ingestion_batch b
                CROSS JOIN LATERAL control.customer_app_scope_status(b.batch_id) s
                WHERE b.batch_id=NEW.target_batch_id AND b.status='completed'
                    AND b.app_id=NEW.target_app_id AND s.app_id=NEW.target_app_id
                    AND s.error_code IS NULL AND b.created_at<=NEW.cutoff) THEN
            RAISE EXCEPTION 'GLOBAL_INVALID_RUN';
        END IF;
    ELSE
        IF (to_jsonb(NEW)-ARRAY['status','completed_at']) IS DISTINCT FROM
           (to_jsonb(OLD)-ARRAY['status','completed_at'])
           OR OLD.status<>'BUILDING' OR NEW.status<>'SUCCEEDED'
           OR NEW.completed_at IS NOT NULL THEN
            RAISE EXCEPTION 'GLOBAL_RUN_IMMUTABLE';
        END IF;
        NEW.completed_at:=clock_timestamp();
    END IF;
    RETURN NEW;
END $body$;
CREATE TRIGGER customer_global_run_guard BEFORE INSERT OR UPDATE ON control.customer_global_run
    FOR EACH ROW EXECUTE FUNCTION control.guard_customer_global_run();
CREATE TRIGGER customer_global_run_no_delete BEFORE DELETE OR TRUNCATE ON control.customer_global_run
    FOR EACH STATEMENT EXECUTE FUNCTION control.reject_customer_identity_snapshot_mutation();

CREATE FUNCTION control.guard_customer_global_append()
RETURNS trigger LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $body$
DECLARE v record;
BEGIN
    FOR v IN SELECT r.status FROM control.customer_global_run r
        WHERE r.run_id IN (SELECT DISTINCT run_id FROM new_rows) FOR SHARE LOOP
        IF v.status<>'BUILDING' THEN RAISE EXCEPTION 'GLOBAL_LEDGER_FINALIZED'; END IF;
    END LOOP;
    RETURN NULL;
END $body$;
CREATE TRIGGER customer_global_batch_append AFTER INSERT ON control.customer_global_batch
    REFERENCING NEW TABLE AS new_rows FOR EACH STATEMENT EXECUTE FUNCTION control.guard_customer_global_append();
CREATE TRIGGER customer_global_member_append AFTER INSERT ON control.customer_global_member
    REFERENCING NEW TABLE AS new_rows FOR EACH STATEMENT EXECUTE FUNCTION control.guard_customer_global_append();
CREATE TRIGGER customer_global_pair_append AFTER INSERT ON control.customer_global_pair
    REFERENCING NEW TABLE AS new_rows FOR EACH STATEMENT EXECUTE FUNCTION control.guard_customer_global_append();
CREATE TRIGGER customer_global_record_append AFTER INSERT ON control.customer_global_record
    REFERENCING NEW TABLE AS new_rows FOR EACH STATEMENT EXECUTE FUNCTION control.guard_customer_global_append();
CREATE TRIGGER customer_global_batch_immutable BEFORE UPDATE OR DELETE OR TRUNCATE ON control.customer_global_batch
    FOR EACH STATEMENT EXECUTE FUNCTION control.reject_customer_identity_snapshot_mutation();
CREATE TRIGGER customer_global_member_immutable BEFORE UPDATE OR DELETE OR TRUNCATE ON control.customer_global_member
    FOR EACH STATEMENT EXECUTE FUNCTION control.reject_customer_identity_snapshot_mutation();
CREATE TRIGGER customer_global_pair_immutable BEFORE UPDATE OR DELETE OR TRUNCATE ON control.customer_global_pair
    FOR EACH STATEMENT EXECUTE FUNCTION control.reject_customer_identity_snapshot_mutation();
CREATE TRIGGER customer_global_record_immutable BEFORE UPDATE OR DELETE OR TRUNCATE ON control.customer_global_record
    FOR EACH STATEMENT EXECUTE FUNCTION control.reject_customer_identity_snapshot_mutation();

CREATE FUNCTION control.finalize_customer_global_run()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $body$
DECLARE report identity.resolution_run%ROWTYPE; n bigint; member_n bigint; entities bigint;
        selected_n bigint; invalid_members boolean;
BEGIN
    -- The enclosing UPDATE serializes writes through the parent row lock.
    PERFORM pg_advisory_xact_lock(7413220179::bigint);
    LOCK TABLE raw.customer_batch_member,raw.customer,staging.customer_profile_history,
        control.ingestion_batch,control.source_landing_batch,control.source_system,
        control.customer_identity_upload_scope,control.customer_identity_transfer,
        control.customer_identity_export IN SHARE MODE NOWAIT;
    IF to_regclass('connector.sync_run') IS NOT NULL THEN
        EXECUTE 'LOCK TABLE connector.sync_run IN SHARE MODE NOWAIT';
    END IF;
    SELECT * INTO STRICT report FROM identity.resolution_run WHERE run_id=NEW.run_id FOR UPDATE;
    IF report.status<>'RUNNING' OR report.completed_at IS NOT NULL OR report.stitching_job_id IS NOT NULL
       OR report.error_message IS NOT NULL THEN RAISE EXCEPTION 'GLOBAL_INVALID_RESOLUTION'; END IF;
    IF EXISTS(SELECT FROM control.customer_identity_transfer t
        WHERE t.publication_seq>NEW.baseline_publication_seq) THEN
        RAISE EXCEPTION 'GLOBAL_STALE_PUBLICATION';
    END IF;
    -- No app filter inferred from the latest global raw/customer_current row.
    -- Freeze every completed batch at cutoff in BOTH participating apps.
    IF EXISTS(SELECT FROM control.customer_global_batch m
        JOIN control.ingestion_batch b USING(batch_id)
        CROSS JOIN LATERAL control.customer_app_scope_status(b.batch_id) scope
        WHERE m.run_id=NEW.run_id AND (
            m.app_id NOT IN (NEW.target_app_id,NEW.protected_app_id)
            OR b.app_id IS DISTINCT FROM m.app_id
            OR b.source_system_id IS DISTINCT FROM m.source_system_id
            OR b.created_at IS DISTINCT FROM m.batch_created_at
            OR b.created_at>NEW.cutoff OR b.status IS DISTINCT FROM 'completed'
            OR scope.app_id IS DISTINCT FROM m.app_id OR scope.error_code IS NOT NULL)
    ) OR EXISTS(SELECT FROM control.ingestion_batch b
        WHERE b.app_id IN (NEW.target_app_id,NEW.protected_app_id)
            AND b.status='completed' AND b.created_at<=NEW.cutoff
            AND NOT EXISTS(SELECT FROM control.customer_global_batch m
                WHERE m.run_id=NEW.run_id AND m.batch_id=b.batch_id))
       OR NOT EXISTS(SELECT FROM control.customer_global_batch
            WHERE run_id=NEW.run_id AND batch_id=NEW.target_batch_id)
       OR NOT EXISTS(SELECT FROM control.customer_global_batch
            WHERE run_id=NEW.run_id AND app_id=NEW.protected_app_id)
       OR EXISTS(SELECT FROM control.customer_identity_connector_lineage(ARRAY(
            SELECT batch_id FROM control.customer_global_batch WHERE run_id=NEW.run_id))) THEN
        RAISE EXCEPTION 'GLOBAL_MANIFEST_INCOMPLETE';
    END IF;
    IF EXISTS(SELECT FROM control.customer_global_batch b
        LEFT JOIN raw.customer_batch_member m ON m.ingestion_batch_id=b.batch_id
        LEFT JOIN raw.customer h ON (h.batch_id,h.raw_customer_id,h.source_system_id,h.source_customer_id)=
            (m.ingestion_batch_id,m.raw_customer_id,m.source_system_id,m.source_customer_id)
        LEFT JOIN staging.customer_profile_history s
            ON (s.batch_id,s.raw_customer_id,s.source_system_id,s.source_customer_id)=
               (m.ingestion_batch_id,m.raw_customer_id,m.source_system_id,m.source_customer_id)
        WHERE b.run_id=NEW.run_id GROUP BY b.batch_id,b.expected_members,
            b.raw_observations,b.normalized_observations
        HAVING count(m.raw_customer_id)<>b.expected_members
            OR count(h.raw_customer_id)<>b.raw_observations
            OR count(s.raw_customer_id)<>b.normalized_observations
            OR count(h.raw_customer_id)<>b.expected_members
            OR count(s.raw_customer_id)<>b.expected_members) THEN
        RAISE EXCEPTION 'GLOBAL_TYPED_COVERAGE';
    END IF;
    -- Winners come from immutable membership, not raw UUID, source-only current,
    -- or mutable staging.customer_profile; all observed tuples must be complete.
    WITH ranked AS MATERIALIZED (
        SELECT b.app_id,m.source_system_id,m.source_customer_id,m.raw_customer_id,
            m.ingestion_batch_id,b.batch_created_at,b.source_system_id AS batch_source,
            row_number() OVER(PARTITION BY b.app_id,m.source_system_id,m.source_customer_id
                ORDER BY b.batch_created_at DESC,b.batch_id DESC) AS priority
        FROM control.customer_global_batch b
        JOIN raw.customer_batch_member m ON m.ingestion_batch_id=b.batch_id
        WHERE b.run_id=NEW.run_id
    ), winners AS MATERIALIZED (SELECT * FROM ranked WHERE priority=1)
    SELECT (SELECT count(*) FROM winners)<>NEW.expected_members
        OR EXISTS(SELECT FROM ranked WHERE source_customer_id='' OR source_customer_id<>btrim(source_customer_id)
            OR source_system_id IS DISTINCT FROM batch_source)
        OR EXISTS(SELECT app_id,source_system_id,source_customer_id,raw_customer_id,ingestion_batch_id
            FROM winners EXCEPT SELECT app_id,source_system_id,user_id,raw_customer_id,source_batch_id
            FROM control.customer_global_member WHERE run_id=NEW.run_id)
        OR EXISTS(SELECT app_id,source_system_id,user_id,raw_customer_id,source_batch_id
            FROM control.customer_global_member WHERE run_id=NEW.run_id
            EXCEPT SELECT app_id,source_system_id,source_customer_id,raw_customer_id,ingestion_batch_id
            FROM winners) INTO invalid_members;
    SELECT count(*) INTO member_n FROM control.customer_global_member WHERE run_id=NEW.run_id;
    IF invalid_members OR member_n<>NEW.expected_members THEN
        RAISE EXCEPTION 'GLOBAL_MEMBERS_INCOMPLETE'; END IF;
    -- Re-read exact typed provenance and compare both copied fingerprints. A
    -- raw UUID reused in another app is harmless ONLY when BOTH independent
    -- immutable (batch,raw,source,user,app) observations exist and are correct.
    IF EXISTS(SELECT FROM control.customer_global_member m
        JOIN control.customer_global_batch b ON (b.run_id,b.batch_id)=(m.run_id,m.source_batch_id)
        LEFT JOIN raw.customer_batch_member member
            ON (member.ingestion_batch_id,member.raw_customer_id,member.source_system_id,member.source_customer_id)=
               (m.source_batch_id,m.raw_customer_id,m.source_system_id,m.user_id)
        LEFT JOIN raw.customer h ON (h.batch_id,h.raw_customer_id)=(m.source_batch_id,m.raw_customer_id)
        LEFT JOIN staging.customer_profile_history s
            ON (s.batch_id,s.raw_customer_id)=(m.source_batch_id,m.raw_customer_id)
        LEFT JOIN control.customer_identity_upload_scope scope ON scope.app_id=m.app_id
        WHERE m.run_id=NEW.run_id AND (
            member.raw_customer_id IS NULL OR h.raw_customer_id IS NULL OR s.raw_customer_id IS NULL
            OR b.app_id IS DISTINCT FROM m.app_id
            OR m.app_id NOT IN (NEW.target_app_id,NEW.protected_app_id)
            OR scope.workspace_id IS DISTINCT FROM m.workspace_id
            OR h.app_id IS DISTINCT FROM m.app_id OR s.app_id IS DISTINCT FROM m.app_id
            OR (h.source_system_id,h.source_customer_id) IS DISTINCT FROM (m.source_system_id,m.user_id)
            OR (s.source_system_id,s.source_customer_id) IS DISTINCT FROM (m.source_system_id,m.user_id)
            OR h.snapshot_id IS DISTINCT FROM m.raw_snapshot_id
            OR s.standardized_record_id IS DISTINCT FROM m.typed_standardized_record_id
            OR s.standardization_status IS NULL
            OR s.standardization_status NOT IN ('READY','INSUFFICIENT_IDENTIFIERS')
                OR s.normalization_version IS NULL
            OR m.raw_sha256 IS DISTINCT FROM encode(public.digest(to_jsonb(h)::text,'sha256'),'hex')
            OR m.typed_sha256 IS DISTINCT FROM encode(public.digest(to_jsonb(s)::text,'sha256'),'hex'))
    ) THEN RAISE EXCEPTION 'GLOBAL_TYPED_PROVENANCE'; END IF;
    -- Preserve the currently visible published entity per (app,user).
    -- Prior committed versions may have another entity: keep their immutable
    -- evidence untouched and reserve their IDs in the executor's registry.
    IF EXISTS(WITH latest AS (
        SELECT DISTINCT ON (e.app_id,e.user_id)
            e.app_id,e.user_id,e.payload->>'customer_entity_id' AS entity_id
        FROM control.customer_identity_transfer t
        JOIN control.customer_identity_export e ON e.transfer_id=t.transfer_id
        WHERE t.status='PUBLISHED' AND e.app_id IN (NEW.protected_app_id,NEW.target_app_id)
        ORDER BY e.app_id,e.user_id,t.publication_seq DESC,e.transfer_id DESC
    ) SELECT FROM latest e
        LEFT JOIN control.customer_global_member m ON m.run_id=NEW.run_id
            AND m.app_id=e.app_id AND m.user_id=e.user_id
        WHERE m.run_id IS NULL OR m.customer_entity_id::text IS DISTINCT FROM e.entity_id
            OR EXISTS(SELECT FROM control.customer_global_member other_member
                WHERE other_member.run_id=NEW.run_id AND other_member.app_id=e.app_id
                    AND other_member.user_id=e.user_id
                    AND other_member.customer_entity_id IS DISTINCT FROM m.customer_entity_id)
    ) THEN RAISE EXCEPTION 'GLOBAL_PUBLISHED_MAPPING_CHANGED'; END IF;
    -- Protect target app's existing mapping against a second publication of
    -- the same (app,user): that requires a separately reviewed version policy.
    IF EXISTS(SELECT FROM control.customer_global_record rec
        JOIN control.customer_identity_export previous_export ON
            (previous_export.app_id,previous_export.user_id)=(rec.app_id,rec.user_id)
        JOIN control.customer_identity_transfer t ON t.transfer_id=previous_export.transfer_id
        WHERE rec.run_id=NEW.run_id AND t.status='PUBLISHED') THEN
        RAISE EXCEPTION 'GLOBAL_TARGET_ALREADY_PUBLISHED';
    END IF;
    SELECT count(*),count(DISTINCT customer_entity_id) INTO member_n,entities
        FROM control.customer_global_member WHERE run_id=NEW.run_id;
    -- Equal entity IDs across apps require a real accepted matcher edge.
    -- Recheck exact immutable typed anchors at finalization; a shared raw UUID
    -- or client-supplied JSON evidence cannot substitute for an independent key.
    IF EXISTS(SELECT FROM control.customer_global_pair p
        JOIN control.customer_global_member left_member
          ON left_member.run_id=p.run_id AND left_member.record_id=p.left_record_id
        JOIN control.customer_global_member right_member
          ON right_member.run_id=p.run_id AND right_member.record_id=p.right_record_id
        LEFT JOIN staging.customer_profile_history left_typed
          ON (left_typed.batch_id,left_typed.raw_customer_id)=
             (left_member.source_batch_id,left_member.raw_customer_id)
        LEFT JOIN staging.customer_profile_history right_typed
          ON (right_typed.batch_id,right_typed.raw_customer_id)=
             (right_member.source_batch_id,right_member.raw_customer_id)
        WHERE p.run_id=NEW.run_id AND (
            left_member.customer_entity_id IS DISTINCT FROM right_member.customer_entity_id
            OR p.method NOT IN ('deterministic','splink')
            OR left_typed.standardized_record_id IS DISTINCT FROM left_member.typed_standardized_record_id
            OR right_typed.standardized_record_id IS DISTINCT FROM right_member.typed_standardized_record_id
            OR (left_member.app_id<>right_member.app_id AND
                (left_typed.phone_is_valid IS TRUE AND right_typed.phone_is_valid IS TRUE
                AND left_typed.phone_normalized<>''
                AND left_typed.phone_normalized=right_typed.phone_normalized
                AND ((left_typed.email_is_valid IS TRUE AND right_typed.email_is_valid IS TRUE
                    AND left_typed.email_normalized<>''
                    AND left_typed.email_normalized=right_typed.email_normalized)
                  OR (left_typed.full_name_normalized<>''
                    AND left_typed.full_name_normalized=right_typed.full_name_normalized
                    AND left_typed.date_of_birth IS NOT NULL
                    AND left_typed.date_of_birth=right_typed.date_of_birth))) IS NOT TRUE)
        )
    ) THEN RAISE EXCEPTION 'GLOBAL_CROSS_APP_PAIR_UNPROVEN'; END IF;
    IF EXISTS(SELECT FROM control.customer_global_member a
        JOIN control.customer_global_member b ON a.run_id=b.run_id
            AND a.app_id<>b.app_id AND a.customer_entity_id=b.customer_entity_id
        WHERE a.run_id=NEW.run_id
          AND NOT EXISTS(SELECT FROM control.customer_global_pair p
              WHERE p.run_id=a.run_id
                AND (p.left_record_id,p.right_record_id)=
                    (least(a.record_id,b.record_id),greatest(a.record_id,b.record_id)))
    ) THEN RAISE EXCEPTION 'GLOBAL_CROSS_APP_ENTITY_UNPROVEN'; END IF;
    SELECT count(*) INTO selected_n FROM raw.customer_batch_member
        WHERE ingestion_batch_id=NEW.target_batch_id;
    SELECT count(*) INTO n FROM control.customer_global_record WHERE run_id=NEW.run_id;
    IF selected_n<>NEW.expected_users OR n<>NEW.expected_users OR EXISTS(
        SELECT FROM (SELECT rec.*,row_number() OVER(ORDER BY rec.app_id COLLATE "C",rec.user_id COLLATE "C") expected_row
            FROM control.customer_global_record rec WHERE run_id=NEW.run_id) rec
        JOIN control.customer_global_member m
           ON (m.run_id,m.app_id,m.source_system_id,m.user_id)=
              (rec.run_id,rec.app_id,rec.source_system_id,rec.user_id)
        JOIN raw.customer h ON (h.batch_id,h.raw_customer_id)=(rec.source_batch_id,rec.raw_customer_id)
        JOIN staging.customer_profile_history s
           ON (s.batch_id,s.raw_customer_id)=(rec.source_batch_id,rec.raw_customer_id)
        WHERE rec.row_number<>rec.expected_row OR rec.app_id<>NEW.target_app_id
            OR (rec.source_batch_id,rec.raw_customer_id) IS DISTINCT FROM
               (NEW.target_batch_id,m.raw_customer_id)
            OR rec.payload->>'app_id' IS DISTINCT FROM rec.app_id
            OR rec.payload->>'user_id' IS DISTINCT FROM rec.user_id
            OR rec.payload#>>'{profile,customer_id}' IS DISTINCT FROM rec.user_id
            OR rec.payload->>'workspace_id' IS DISTINCT FROM NEW.workspace_id::text
            OR rec.payload->>'resolution_run_id' IS DISTINCT FROM NEW.run_id::text
            OR rec.payload->>'raw_customer_id' IS DISTINCT FROM rec.raw_customer_id::text
            OR rec.payload->>'standardized_record_id' IS DISTINCT FROM m.record_id::text
            OR rec.payload->>'customer_entity_id' IS DISTINCT FROM m.customer_entity_id::text
            OR rec.payload->>'source_system_id' IS DISTINCT FROM rec.source_system_id::text
            OR rec.payload->>'match_method' IS DISTINCT FROM m.match_method
            OR CASE WHEN jsonb_typeof(rec.payload->'match_probability')='number'
                THEN (rec.payload->>'match_probability')::numeric IS DISTINCT FROM m.match_probability ELSE true END
            OR rec.input_payload->>'record_id' IS DISTINCT FROM m.record_id::text
            OR rec.input_payload#>>'{raw,batch_id}' IS DISTINCT FROM rec.source_batch_id::text
            OR rec.input_payload#>>'{raw,raw_customer_id}' IS DISTINCT FROM rec.raw_customer_id::text
            OR rec.input_payload#>>'{raw,source_system_id}' IS DISTINCT FROM rec.source_system_id::text
            OR rec.input_payload#>>'{raw,source_customer_id}' IS DISTINCT FROM rec.user_id
            OR rec.input_payload#>>'{raw,app_id}' IS DISTINCT FROM rec.app_id
            OR rec.input_payload#>>'{raw,first_name}' IS DISTINCT FROM h.first_name
            OR rec.input_payload#>>'{normalized,first_name_normalized}' IS DISTINCT FROM s.first_name_normalized
            OR rec.payload#>>'{profile,first_name}' IS DISTINCT FROM s.first_name_raw
            OR jsonb_typeof(rec.input_payload->'normalized') IS DISTINCT FROM 'object'
            OR rec.payload ? 'source_batch_id' OR rec.payload ? 'evidence'
    ) OR EXISTS(SELECT m.source_system_id,m.source_customer_id,m.raw_customer_id FROM raw.customer_batch_member m
        WHERE m.ingestion_batch_id=NEW.target_batch_id
        EXCEPT SELECT source_system_id,user_id,raw_customer_id
            FROM control.customer_global_record WHERE run_id=NEW.run_id)
    THEN RAISE EXCEPTION 'GLOBAL_SELECTED_EXPORT_INVALID'; END IF;
    IF report.source_record_count<>member_n OR report.standardized_record_count<>member_n
       OR report.selected_record_count<>n OR report.entity_count<>entities
       OR report.accepted_pair_count<>(SELECT count(*) FROM control.customer_global_pair
            WHERE run_id=NEW.run_id)
       OR report.candidate_pair_count<(
            SELECT count(*) FROM control.customer_global_pair
            WHERE run_id=NEW.run_id AND method='splink')
       OR btrim(report.algorithm)='' OR btrim(report.normalization_version)='' THEN
        RAISE EXCEPTION 'GLOBAL_INCOMPLETE_REPORT';
    END IF;
    UPDATE identity.resolution_run SET status='SUCCEEDED',completed_at=NEW.completed_at WHERE run_id=NEW.run_id;
    INSERT INTO control.customer_identity_transfer
        (transfer_id,resolution_run_id,global_run_id,stitching_job_id,ingestion_batch_id,
         workspace_id,app_id,status,row_count,member_count,selected_row_count)
    VALUES (NEW.run_id,NEW.run_id,NEW.run_id,NULL,NULL,NEW.workspace_id,
            NEW.target_app_id,'PENDING',n,member_n,n);
    -- Nested in AFTER status trigger: existing 024 capture-only export guard
    -- stays enabled; never insert outside this fixed-path finalization transaction.
    INSERT INTO control.customer_identity_export(transfer_id,row_number,app_id,user_id,payload)
        SELECT run_id,row_number,app_id,user_id,payload
        FROM control.customer_global_record WHERE run_id=NEW.run_id ORDER BY row_number;
    RETURN NEW;
END $body$;
CREATE TRIGGER customer_global_finalize AFTER UPDATE OF status ON control.customer_global_run
    FOR EACH ROW EXECUTE FUNCTION control.finalize_customer_global_run();

CREATE FUNCTION control.finish_customer_global_run(p_run uuid)
RETURNS void LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $body$
DECLARE state text;
BEGIN
    SELECT status INTO STRICT state FROM control.customer_global_run WHERE run_id=p_run FOR UPDATE;
    IF state='SUCCEEDED' THEN RETURN; END IF;
    IF state<>'BUILDING' THEN RAISE EXCEPTION 'GLOBAL_NOT_BUILDING'; END IF;
    UPDATE control.customer_global_run SET status='SUCCEEDED' WHERE run_id=p_run;
END $body$;

-- Discard named-role/PUBLIC default ACLs for newly created private objects.
-- Leave all existing 024/027/041 worker privileges exactly as they were.
DO $private_acl$
DECLARE acl record;
BEGIN
    FOR acl IN
        SELECT DISTINCT 'TABLE' AS kind,c.oid::regclass::text AS object_name,a.grantee
        FROM pg_class c CROSS JOIN LATERAL aclexplode(coalesce(c.relacl,acldefault('r',c.relowner))) a
        WHERE c.oid IN ('control.customer_global_run'::regclass,'control.customer_global_batch'::regclass,
           'control.customer_global_member'::regclass,'control.customer_global_pair'::regclass,
           'control.customer_global_record'::regclass)
          AND a.grantee<>c.relowner
        UNION ALL
        SELECT DISTINCT 'FUNCTION',p.oid::regprocedure::text,a.grantee FROM pg_proc p
        CROSS JOIN LATERAL aclexplode(coalesce(p.proacl,acldefault('f',p.proowner))) a
        WHERE p.oid IN ('control.guard_customer_global_run()'::regprocedure,
           'control.guard_customer_global_append()'::regprocedure,
           'control.finalize_customer_global_run()'::regprocedure,
           'control.finish_customer_global_run(uuid)'::regprocedure)
          AND a.grantee<>p.proowner
    LOOP
        EXECUTE format('REVOKE ALL PRIVILEGES ON %s %s FROM %s CASCADE',acl.kind,acl.object_name,
          CASE WHEN acl.grantee=0 THEN 'PUBLIC' ELSE quote_ident(pg_get_userbyid(acl.grantee)) END);
    END LOOP;
END $private_acl$;
DO $worker$
BEGIN
    IF EXISTS(SELECT FROM pg_roles WHERE rolname='identity_publication_worker' AND rolcanlogin AND NOT rolsuper) THEN
        GRANT SELECT ON control.customer_global_run,control.customer_global_member,
            control.customer_global_pair,control.customer_global_record TO identity_publication_worker;
    END IF;
END $worker$;
COMMIT;
