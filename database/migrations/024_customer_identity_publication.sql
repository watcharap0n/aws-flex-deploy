-- Additive post-stitch publication. Apply manually; no backfill or deployment.
-- Never alter ingestion, matching, 019-023, or existing pipeline status views.
-- SECURITY/ROLLOUT: install as a trusted non-login schema owner. The one
-- SECURITY DEFINER entry is a trigger with a fixed pg_catalog/pg_temp path;
-- all business objects are schema-qualified and no caller text becomes SQL.
-- Explicitly grant the worker SELECT(export,transfer), column-scoped UPDATE
-- (status,attempts,error_code,source_sha256,published_at,updated_at) on transfer.
-- Do not grant it schema CREATE, registry writes, INSERT, DELETE or TRUNCATE.
-- Existing stitching roles require no new table privileges through the trigger.
-- Upload scope UUIDs are INTERNAL partition identifiers, NOT platform.workspace
-- IDs. Keep the workspace_id wire column for the existing worker/ClickHouse DDL.
-- LIMITS: membership must be a complete trustworthy ownership journal. Existing
-- lost pre-014 history cannot be inferred. Global matching is NOT tenant-safe:
-- this exporter refuses mixed/unknown components; it does not fix matching.
-- Acquire every ownership SHARE lock with NOWAIT: ingestion can already own
-- ingestion_batch before appending membership (the opposite lock order).
-- Contention rolls back capture locks/payload only, records a sanitized BLOCKED
-- CAPTURE_LOCK_CONTENDED, and preserves stitching/ingestion transactions.
-- Re-stitch in a NEW job/run after contention; never retry capture of a stale
-- run from mutable identity state. BLOCKED transfers cannot become runnable.
-- Successful SHARE locks last through outer commit and CAN delay new unrelated
-- ingestion writers; ordinary SELECT readers remain compatible. This is not a
-- nonblocking-ingestion guarantee. Identity writers must retain the existing
-- global advisory lock, acquired before their identity/stitch-job writes.
-- Local synthetic measured
-- 10k=1.370s and 100k=6.374s capture+commit (not worker; no production SLA).
-- Benchmark actual largest batches/storage before enabling production capture.
-- Client cancel/backend failure cannot guarantee any durable journal; ordinary
-- validation/SQL failures are isolated, and reconciliation detects missing rows.
BEGIN;
SET LOCAL lock_timeout = '5s';

-- Automatically populated inside capture's rollback boundary. Literal numeric
-- app IDs are authoritative: no integer cast, trimming or leading-zero removal.
-- No platform FK, fake platform record, global default or app allowlist.
CREATE TABLE control.customer_identity_upload_scope (
    app_id text PRIMARY KEY CHECK (app_id ~ '^[0-9]+$'),
    workspace_id uuid NOT NULL UNIQUE DEFAULT gen_random_uuid(),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
COMMENT ON TABLE control.customer_identity_upload_scope IS
    'Private internal file-upload app scopes; workspace_id is NOT a platform.workspace UUID.';

CREATE TABLE control.customer_identity_transfer (
    transfer_id uuid PRIMARY KEY,
    resolution_run_id uuid NOT NULL UNIQUE REFERENCES identity.resolution_run(run_id),
    stitching_job_id uuid NOT NULL UNIQUE REFERENCES control.stitching_job(job_id),
    ingestion_batch_id uuid REFERENCES control.ingestion_batch(batch_id),
    workspace_id uuid,
    app_id text,
    publication_seq bigint GENERATED ALWAYS AS IDENTITY UNIQUE,
    status text NOT NULL CHECK (status IN ('PENDING','RUNNING','FAILED','BLOCKED','PUBLISHED')),
    row_count bigint NOT NULL DEFAULT 0 CHECK (row_count >= 0),
    source_sha256 text CHECK (source_sha256 IS NULL OR source_sha256 ~ '^[0-9a-f]{64}$'),
    published_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    error_code text,
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    CHECK (status = 'BLOCKED' OR (workspace_id IS NOT NULL AND app_id IS NOT NULL AND btrim(app_id) <> ''))
);
-- No invented resolution UUID for a success notification without a real run.
CREATE TABLE control.customer_identity_capture_issue (
    stitching_job_id uuid PRIMARY KEY REFERENCES control.stitching_job(job_id),
    status text NOT NULL DEFAULT 'BLOCKED' CHECK (status='BLOCKED'),
    error_code text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX customer_identity_transfer_pending
    ON control.customer_identity_transfer(publication_seq) WHERE status IN ('PENDING','RUNNING','FAILED');
CREATE TABLE control.customer_identity_export (
    transfer_id uuid NOT NULL REFERENCES control.customer_identity_transfer(transfer_id),
    row_number bigint NOT NULL CHECK (row_number > 0),
    app_id text NOT NULL CHECK (btrim(app_id) <> ''),
    user_id text NOT NULL CHECK (btrim(user_id) <> ''),
    payload jsonb NOT NULL CHECK (jsonb_typeof(payload) = 'object'),
    PRIMARY KEY (transfer_id,row_number),
    UNIQUE (transfer_id,app_id,user_id)
);

-- Exact accepted file format: YYYY-MM-DD-HHMMSS-<decimal app_id>-<12 lower hex>.
-- Date/time are syntactic lineage only; never treated as an ownership source.
-- The literal parsed app is authoritative; source_code never supplies ownership.
-- Optional connector control plane is discovered at call time, including when
-- installed after 024. Dynamic SQL only bridges absent relations; no values
-- or identifiers supplied by a caller are interpolated.
CREATE FUNCTION control.customer_identity_connector_lineage(batch_ids uuid[])
RETURNS TABLE (ingestion_batch_id uuid)
LANGUAGE plpgsql STABLE SET search_path = pg_catalog, pg_temp AS $connector$
BEGIN
    IF to_regclass('connector.sync_run') IS NULL THEN RETURN; END IF;
    RETURN QUERY EXECUTE $sql$
        -- Any connector lineage excludes a batch, including inconsistent
        -- landing/job links and retries. A file-looking ref cannot override it.
        SELECT b.batch_id FROM control.ingestion_batch b
        WHERE b.batch_id=ANY($1) AND (
            EXISTS(SELECT 1 FROM connector.sync_run s WHERE s.ingestion_batch_id=b.batch_id)
            OR EXISTS(SELECT 1 FROM connector.sync_run s WHERE s.landing_batch_id=b.landing_batch_id)
            OR EXISTS(SELECT 1 FROM connector.sync_run s
                JOIN control.stitching_job j ON j.job_id=s.stitching_job_id
                WHERE j.ingestion_batch_id=b.batch_id))
    $sql$ USING batch_ids;
END $connector$;

CREATE FUNCTION control.resolve_customer_identity_batches(batch_ids uuid[])
RETURNS TABLE (ingestion_batch_id uuid,workspace_id uuid,app_id text,error_code text)
LANGUAGE sql VOLATILE SET search_path = pg_catalog, pg_temp AS $resolve$
    -- Set-based bootstrap, private to capture. The enclosing capture savepoint
    -- removes registrations as well as exports on any rejected component.
    INSERT INTO control.customer_identity_upload_scope(app_id)
    SELECT DISTINCT (regexp_match(l.batch_ref,'^[0-9]{4}-[0-9]{2}-[0-9]{2}-[0-9]{6}-([0-9]+)-[0-9a-f]{12}$'))[1]
    FROM control.ingestion_batch b
    JOIN control.source_landing_batch l ON l.batch_id=b.landing_batch_id
    JOIN control.source_system ss USING(source_system_id)
    WHERE b.batch_id=ANY(batch_ids) AND b.batch_ref=l.batch_ref
      AND ss.source_code=l.source_code
      AND l.batch_ref ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}-[0-9]{6}-[0-9]+-[0-9a-f]{12}$'
      AND NOT EXISTS(SELECT 1 FROM control.customer_identity_connector_lineage(batch_ids) c
                     WHERE c.ingestion_batch_id=b.batch_id)
    ON CONFLICT (app_id) DO NOTHING;

    WITH batches AS MATERIALIZED (
        SELECT wanted.batch_id,b.batch_ref,l.batch_ref AS landing_ref,
               b.source_system_id,ss.source_code,l.source_code AS landing_source,
               (regexp_match(l.batch_ref,'^[0-9]{4}-[0-9]{2}-[0-9]{2}-[0-9]{6}-([0-9]+)-[0-9a-f]{12}$'))[1] AS parsed_app
        FROM (SELECT DISTINCT unnest(batch_ids) AS batch_id) wanted
        LEFT JOIN control.ingestion_batch b USING(batch_id)
        LEFT JOIN control.source_landing_batch l ON l.batch_id=b.landing_batch_id
        LEFT JOIN control.source_system ss USING(source_system_id)
    ), connectors AS MATERIALIZED (
        SELECT * FROM control.customer_identity_connector_lineage(batch_ids)
    )
    SELECT b.batch_id,r.workspace_id,r.app_id,
        CASE WHEN c.ingestion_batch_id IS NOT NULL THEN 'CONNECTOR_EXCLUDED'
             WHEN b.source_system_id IS NULL THEN 'UNKNOWN_BATCH'
             WHEN b.batch_ref IS DISTINCT FROM b.landing_ref
               OR b.source_code IS DISTINCT FROM b.landing_source
               THEN 'BATCH_LINEAGE_CONFLICT'
             WHEN r.workspace_id IS NULL THEN 'INVALID_UPLOAD_BATCH_REF' END
    FROM batches b LEFT JOIN connectors c ON c.ingestion_batch_id=b.batch_id
    LEFT JOIN control.customer_identity_upload_scope r
        ON c.ingestion_batch_id IS NULL AND r.app_id=b.parsed_app
$resolve$;

CREATE FUNCTION control.capture_customer_identity_snapshot(job_id uuid)
RETURNS void LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $capture$
DECLARE j control.stitching_job%ROWTYPE; run_id uuid; run_completed_at timestamptz;
    owner_scope record; n bigint; validation_error text;
BEGIN
    SELECT * INTO STRICT j FROM control.stitching_job WHERE control.stitching_job.job_id=capture_customer_identity_snapshot.job_id;
    IF EXISTS(SELECT 1 FROM control.customer_identity_connector_lineage(ARRAY[j.ingestion_batch_id])) THEN RETURN; END IF;
    SELECT r.run_id,r.completed_at INTO STRICT run_id,run_completed_at FROM identity.resolution_run r
        WHERE r.stitching_job_id=j.job_id AND r.status='SUCCEEDED';
    IF j.status <> 'SUCCEEDED' THEN RETURN; END IF;
    IF EXISTS (SELECT 1 FROM control.customer_identity_transfer t WHERE t.stitching_job_id=j.job_id) THEN RETURN; END IF;
    -- Same lock as identity_stitcher.db.exclusive_job_lock. Session owner can
    -- acquire it transactionally too. Released only at enclosing commit/rollback.
    PERFORM pg_advisory_xact_lock(7413220179::bigint);
    -- Prevent ownership/staging phantoms between preflight and export. These
    -- table SHARE locks last until the outer stitch commit; no raw PII read.
    -- Never wait while owning a prefix of these locks: ingestion takes them
    -- in a different order. Failure unwinds ALL capture locks in the caller's
    -- savepoint, without a deadlock victim or rollback in the ingestion writer.
    -- Global matching already holds the advisory lock; ingestion does not.
    LOCK TABLE raw.customer_batch_member, staging.customer_profile,
        control.customer_identity_upload_scope, control.ingestion_batch,
        control.source_landing_batch, control.source_system IN SHARE MODE NOWAIT;
    IF to_regclass('connector.sync_run') IS NOT NULL THEN
        EXECUTE 'LOCK TABLE connector.sync_run IN SHARE MODE NOWAIT';
    END IF;
    -- Recheck after locking provenance against concurrent connector registration.
    IF EXISTS(SELECT 1 FROM control.customer_identity_connector_lineage(ARRAY[j.ingestion_batch_id])) THEN RETURN; END IF;
    SELECT * INTO owner_scope FROM control.resolve_customer_identity_batches(ARRAY[j.ingestion_batch_id]);
    IF owner_scope.error_code IS NOT NULL THEN
        RAISE EXCEPTION USING ERRCODE=CASE owner_scope.error_code
            WHEN 'INVALID_UPLOAD_BATCH_REF' THEN 'P2410' ELSE 'P2411' END,
            MESSAGE='Identity publication scope rejected';
    END IF;
    -- Restrict the component expansion to selected entities; never scan pair
    -- history. Resolve each DISTINCT relevant batch once, not once per user.
    WITH selected AS MATERIALIZED (
        SELECT b.raw_customer_id,b.source_system_id,b.source_customer_id,
               p.standardized_record_id,m.customer_entity_id,
               m.source_system_id AS mapped_source,m.source_customer_id AS mapped_user
        FROM raw.customer_batch_member b
        LEFT JOIN staging.customer_profile p ON p.raw_customer_id=b.raw_customer_id
        LEFT JOIN identity.customer_entity_member m ON m.standardized_record_id=p.standardized_record_id
        WHERE b.ingestion_batch_id=j.ingestion_batch_id
    ), components AS MATERIALIZED (
        SELECT DISTINCT customer_entity_id FROM selected WHERE customer_entity_id IS NOT NULL
    ), members AS MATERIALIZED (
        SELECT m.*,p.raw_customer_id,p.batch_id,p.source_customer_id AS profile_user,
               p.source_system_id AS profile_source,p.standardized_at,
               e.last_resolution_run_id,e.member_count
        FROM components c
        JOIN identity.customer_entity e USING(customer_entity_id)
        JOIN identity.customer_entity_member m USING(customer_entity_id)
        LEFT JOIN staging.customer_profile p USING(standardized_record_id)
    ), evidence AS MATERIALIZED (
        SELECT m.raw_customer_id,b.ingestion_batch_id,b.source_system_id,b.source_customer_id,
               b.source_system_id IS DISTINCT FROM m.source_system_id
                 OR b.source_customer_id IS DISTINCT FROM m.source_customer_id AS identity_conflict
        FROM members m LEFT JOIN raw.customer_batch_member b USING(raw_customer_id)
        UNION
        SELECT m.raw_customer_id,m.batch_id,m.profile_source,m.profile_user,
               m.profile_source IS DISTINCT FROM m.source_system_id
                 OR m.profile_user IS DISTINCT FROM m.source_customer_id FROM members m
    ), owners AS MATERIALIZED (
        SELECT * FROM control.resolve_customer_identity_batches(
            ARRAY(SELECT DISTINCT ingestion_batch_id FROM evidence WHERE ingestion_batch_id IS NOT NULL))
    )
    SELECT CASE
        WHEN EXISTS(SELECT 1 FROM selected WHERE btrim(source_customer_id)='') THEN 'P2412'
        WHEN EXISTS(SELECT 1 FROM selected GROUP BY source_customer_id HAVING count(*)>1) THEN 'P2413'
        WHEN EXISTS(SELECT 1 FROM selected WHERE standardized_record_id IS NULL OR customer_entity_id IS NULL)
          OR (SELECT count(*) FROM selected) <> j.selected_record_count
          OR (SELECT count(*) FROM selected) <> j.member_record_count THEN 'P2414'
        WHEN EXISTS(SELECT 1 FROM members WHERE resolution_run_id<>run_id
            OR last_resolution_run_id IS DISTINCT FROM run_id
            OR run_completed_at IS NULL OR standardized_at > run_completed_at)
          OR EXISTS(SELECT 1 FROM members GROUP BY customer_entity_id,member_count HAVING count(*)<>member_count)
            THEN 'P2415'
        WHEN EXISTS(SELECT 1 FROM members WHERE raw_customer_id IS NULL
            OR profile_user IS DISTINCT FROM source_customer_id OR profile_source IS DISTINCT FROM source_system_id)
          OR EXISTS(SELECT 1 FROM selected WHERE source_system_id IS DISTINCT FROM mapped_source
              OR source_customer_id IS DISTINCT FROM mapped_user)
          OR EXISTS(SELECT 1 FROM evidence e JOIN control.ingestion_batch b ON b.batch_id=e.ingestion_batch_id
              WHERE b.source_system_id IS DISTINCT FROM e.source_system_id)
          OR EXISTS(SELECT 1 FROM evidence WHERE identity_conflict)
            THEN 'P2416'
        WHEN EXISTS(SELECT 1 FROM evidence e LEFT JOIN owners o USING(ingestion_batch_id)
            WHERE o.error_code IS NOT NULL OR o.workspace_id IS DISTINCT FROM owner_scope.workspace_id
               OR o.app_id IS DISTINCT FROM owner_scope.app_id)
          OR EXISTS(SELECT 1 FROM members m WHERE NOT EXISTS(
              SELECT 1 FROM raw.customer_batch_member b WHERE b.raw_customer_id=m.raw_customer_id
                AND b.ingestion_batch_id=m.batch_id)) THEN 'P2417'
    END INTO validation_error;
    IF validation_error IS NOT NULL THEN
        RAISE EXCEPTION USING ERRCODE=validation_error,MESSAGE='Identity publication validation rejected';
    END IF;
    SELECT count(*) INTO n FROM raw.customer_batch_member WHERE ingestion_batch_id=j.ingestion_batch_id;
    INSERT INTO control.customer_identity_transfer
        (transfer_id,resolution_run_id,stitching_job_id,ingestion_batch_id,workspace_id,app_id,status,row_count)
        VALUES(run_id,run_id,j.job_id,j.ingestion_batch_id,owner_scope.workspace_id,owner_scope.app_id,'PENDING',n);
    INSERT INTO control.customer_identity_export(transfer_id,row_number,app_id,user_id,payload)
    SELECT run_id,row_number() OVER (ORDER BY p.source_customer_id COLLATE "C"),owner_scope.app_id,p.source_customer_id,
        jsonb_build_object(
            'customer_entity_id',e.customer_entity_id,'match_probability',m.match_probability,
            'match_method',m.match_method,'source_system_id',p.source_system_id,
            'standardized_record_id',p.standardized_record_id,'raw_customer_id',p.raw_customer_id,
            'resolution_run_id',run_id,'workspace_id',owner_scope.workspace_id,
            'app_id',owner_scope.app_id,'user_id',p.source_customer_id,
            'profile',jsonb_build_object(
                'customer_id',p.source_customer_id,'customer_key_id',NULL,'profile_source_record_id',NULL,
                'first_name',e.canonical_first_name,'middle_name',e.canonical_middle_name,
                'last_name',e.canonical_last_name,'full_name',e.canonical_full_name,
                'email',e.canonical_email,'phone',e.canonical_phone,
                'address_line_1',e.canonical_address,'district',e.canonical_district,
                'province',e.canonical_province,'postal_code',e.canonical_postal_code,
                'country_code',e.canonical_country_code,'date_of_birth',e.canonical_date_of_birth,
                'gender',e.canonical_gender,
                'source_created_at',to_char(p.source_created_at AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
                'source_updated_at',to_char(p.source_updated_at AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
                'profile_updated_at',to_char(p.source_updated_at AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
                'standardized_at',to_char(p.standardized_at AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
                'first_seen_at',to_char(e.first_seen_at AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
                'last_seen_at',to_char(e.last_seen_at AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS.US"Z"')
            ))
    FROM raw.customer_batch_member b
    JOIN staging.customer_profile p ON p.raw_customer_id=b.raw_customer_id
    JOIN identity.customer_entity_member m ON m.standardized_record_id=p.standardized_record_id
    JOIN identity.customer_entity e ON e.customer_entity_id=m.customer_entity_id
    WHERE b.ingestion_batch_id=j.ingestion_batch_id;
    GET DIAGNOSTICS n = ROW_COUNT;
    IF n <> j.selected_record_count THEN
        RAISE EXCEPTION USING ERRCODE='P2414', MESSAGE='Identity export count mismatch';
    END IF;
END $capture$;

CREATE FUNCTION control.capture_customer_identity_after_stitch()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $trigger$
DECLARE run_id uuid; reason text;
BEGIN
    -- Exclusion precedes resolution lookup, journaling, locks and registration.
    IF EXISTS(SELECT 1 FROM control.customer_identity_connector_lineage(ARRAY[NEW.ingestion_batch_id])) THEN RETURN NEW; END IF;
    SELECT r.run_id INTO run_id FROM identity.resolution_run r
        WHERE r.stitching_job_id=NEW.job_id AND r.status='SUCCEEDED';
    -- Preserve success without fabricating a resolution run or PII payload.
    IF run_id IS NULL THEN
        INSERT INTO control.customer_identity_capture_issue(stitching_job_id,error_code)
            VALUES(NEW.job_id,'NO_SUCCESSFUL_RESOLUTION') ON CONFLICT DO NOTHING;
        RETURN NEW;
    END IF;
    -- Acquire outside the validation savepoint so a rejected capture retains
    -- serialization until outer COMMIT. Lock failure falls to issue journal,
    -- never assigns publication_seq outside the global commit-order lock.
    PERFORM pg_advisory_xact_lock(7413220179::bigint);
    BEGIN
        PERFORM control.capture_customer_identity_snapshot(NEW.job_id);
    EXCEPTION WHEN OTHERS THEN
        -- The nested block rolls back every snapshot row on failure. Never
        -- persist SQLERRM, context, DETAIL, source values or arbitrary messages.
        reason := CASE SQLSTATE
            WHEN '55P03' THEN 'CAPTURE_LOCK_CONTENDED'
            WHEN 'P2410' THEN 'INVALID_UPLOAD_BATCH_REF'
            WHEN 'P2411' THEN 'BATCH_LINEAGE_CONFLICT'
            WHEN 'P2412' THEN 'BLANK_SOURCE_USER'
            WHEN 'P2413' THEN 'DUPLICATE_APP_USER'
            WHEN 'P2414' THEN 'INCOMPLETE_MAPPING'
            WHEN 'P2415' THEN 'STALE_RESOLUTION'
            WHEN 'P2416' THEN 'SOURCE_IDENTITY_CONFLICT'
            WHEN 'P2417' THEN 'MIXED_OR_UNKNOWN_OWNER'
            ELSE 'CAPTURE_SQL_ERROR' END;
        INSERT INTO control.customer_identity_transfer
            (transfer_id,resolution_run_id,stitching_job_id,ingestion_batch_id,status,error_code,row_count)
            VALUES(run_id,run_id,NEW.job_id,NEW.ingestion_batch_id,'BLOCKED',reason,0)
            ON CONFLICT (stitching_job_id) DO NOTHING;
    END;
    RETURN NEW;
EXCEPTION WHEN OTHERS THEN
    -- Even outbox storage/privilege failures must not poison stitching. A
    -- separate non-PII issue journal covers a broken outbox. If the journal is
    -- unavailable too, reconciliation detects SUCCEEDED jobs with no capture.
    BEGIN
        INSERT INTO control.customer_identity_capture_issue(stitching_job_id,error_code)
            VALUES(NEW.job_id,'CAPTURE_SQL_ERROR') ON CONFLICT DO NOTHING;
    EXCEPTION WHEN OTHERS THEN NULL;
    END;
    RETURN NEW;
END $trigger$;
CREATE TRIGGER customer_identity_after_stitch
    AFTER UPDATE OF status ON control.stitching_job FOR EACH ROW
    WHEN (NEW.status='SUCCEEDED')
    EXECUTE FUNCTION control.capture_customer_identity_after_stitch();
CREATE FUNCTION control.reject_customer_identity_snapshot_mutation()
RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    RAISE EXCEPTION 'Customer identity publication snapshot is immutable';
END $$;
CREATE TRIGGER customer_identity_upload_scope_immutable
    BEFORE UPDATE OR DELETE OR TRUNCATE ON control.customer_identity_upload_scope
    FOR EACH STATEMENT EXECUTE FUNCTION control.reject_customer_identity_snapshot_mutation();
-- No INSERT grant belongs on the worker role. This trigger also rejects
-- accidental appends through an overprivileged client; capture runs nested
-- beneath the successful-job trigger. Schema owners can of course disable any
-- trigger, so owner credentials must never be used by publication workers.
CREATE FUNCTION control.guard_customer_identity_export_insert()
RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    IF pg_trigger_depth() < 2 THEN
        RAISE EXCEPTION 'Customer identity snapshots may only be captured at stitch commit';
    END IF;
    RETURN NULL;
END $$;
CREATE TRIGGER customer_identity_export_capture_only
    BEFORE INSERT ON control.customer_identity_export FOR EACH STATEMENT
    EXECUTE FUNCTION control.guard_customer_identity_export_insert();
CREATE TRIGGER customer_identity_export_immutable
    BEFORE UPDATE OR DELETE OR TRUNCATE ON control.customer_identity_export
    FOR EACH STATEMENT EXECUTE FUNCTION control.reject_customer_identity_snapshot_mutation();
CREATE TRIGGER customer_identity_transfer_no_delete
    BEFORE DELETE OR TRUNCATE ON control.customer_identity_transfer
    FOR EACH STATEMENT EXECUTE FUNCTION control.reject_customer_identity_snapshot_mutation();
CREATE FUNCTION control.freeze_customer_identity_transfer_scope()
RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $$
BEGIN
    IF (NEW.transfer_id,NEW.resolution_run_id,NEW.stitching_job_id,NEW.ingestion_batch_id,
        NEW.workspace_id,NEW.app_id,NEW.publication_seq,NEW.row_count,NEW.created_at)
        IS DISTINCT FROM
       (OLD.transfer_id,OLD.resolution_run_id,OLD.stitching_job_id,OLD.ingestion_batch_id,
        OLD.workspace_id,OLD.app_id,OLD.publication_seq,OLD.row_count,OLD.created_at) THEN
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
CREATE TRIGGER customer_identity_transfer_frozen_scope
    BEFORE UPDATE ON control.customer_identity_transfer FOR EACH ROW
    EXECUTE FUNCTION control.freeze_customer_identity_transfer_scope();

-- Make only these newly created objects private, even when the installing
-- owner's global or per-schema DEFAULT PRIVILEGES grant named application
-- roles access. PUBLIC-only revocation is insufficient for PII and registry
-- writes. Enumerate actual ACL grantees; leave existing objects/defaults alone.
-- This transaction exposes no intermediate inherited ACLs to other sessions.
DO $private_acl$
DECLARE acl record;
BEGIN
    FOR acl IN
        SELECT DISTINCT CASE WHEN c.relkind='S' THEN 'SEQUENCE' ELSE 'TABLE' END AS kind,
            format('%I.%I',n.nspname,c.relname) AS object_name,a.grantee
        FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
        CROSS JOIN LATERAL aclexplode(coalesce(c.relacl,
            acldefault(CASE WHEN c.relkind='S' THEN 'S'::"char" ELSE 'r'::"char" END,c.relowner))) a
        WHERE c.oid IN ('control.customer_identity_upload_scope'::regclass,
            'control.customer_identity_transfer'::regclass,
            'control.customer_identity_capture_issue'::regclass,
            'control.customer_identity_export'::regclass,
            pg_get_serial_sequence('control.customer_identity_transfer','publication_seq')::regclass)
            AND a.grantee<>c.relowner
        UNION ALL
        SELECT DISTINCT 'FUNCTION',p.oid::regprocedure::text,a.grantee
        FROM pg_proc p
        CROSS JOIN LATERAL aclexplode(coalesce(p.proacl,acldefault('f',p.proowner))) a
        WHERE p.oid IN ('control.customer_identity_connector_lineage(uuid[])'::regprocedure,
            'control.resolve_customer_identity_batches(uuid[])'::regprocedure,
            'control.capture_customer_identity_snapshot(uuid)'::regprocedure,
            'control.capture_customer_identity_after_stitch()'::regprocedure,
            'control.reject_customer_identity_snapshot_mutation()'::regprocedure,
            'control.guard_customer_identity_export_insert()'::regprocedure,
            'control.freeze_customer_identity_transfer_scope()'::regprocedure)
            AND a.grantee<>p.proowner
    LOOP
        EXECUTE format('REVOKE ALL PRIVILEGES ON %s %s FROM %s CASCADE',
            acl.kind,acl.object_name,
            CASE WHEN acl.grantee=0 THEN 'PUBLIC' ELSE quote_ident(pg_get_userbyid(acl.grantee)) END);
    END LOOP;
END $private_acl$;
-- After installation, explicitly grant USAGE ON SCHEMA control plus:
-- worker: SELECT ON control.customer_identity_export,control.customer_identity_transfer;
-- worker: UPDATE(status,attempts,error_code,source_sha256,published_at,updated_at)
--         ON control.customer_identity_transfer (no sequence or function grants).
-- no application/worker registry write grants; capture bootstraps upload scopes.
-- trusted reconciliation operator: SELECT ON control.customer_identity_capture_issue
--         and control.customer_identity_transfer. Never grant to PUBLIC.
COMMIT;
