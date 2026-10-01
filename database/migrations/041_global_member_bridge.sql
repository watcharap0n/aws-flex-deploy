-- Forward-only global publication capture; manual catalog-pinned installation.
-- Install only after comparing the pinned baseline below
-- against the deployed catalog, and only on NEW successful ordinary global runs.
-- Old BLOCKED transfers remain immutable. This changes no matching or source IDs.
-- Do not install as an automatic startup migration or replay a prior job.
-- The global entity ID remains global; per-app profile fields come exclusively
-- from the selected source's staging profile. Current source keys are still
-- globally keyed by (source_system_id,source_customer_id): overlapping app
-- keys can overwrite current/profile state. Such collisions MUST stay BLOCKED
-- until a separately coordinated namespace/ingestion/stitcher cutover exists.
-- Only a NEW successful stitch job may capture. Existing 031 intents remain.
-- A mixed-app global entity is expected. A source key shared across apps
-- under the SAME raw UUID remains an alias collision, not a positive match.
-- Unknown/connector contributor lineage remains blocked until explicitly
-- governed; known app provenance is not an exclusive owner of a person.
-- Pin either the deployed 024 body or the validated 036 body before replacement.
-- Reject unknown bodies; the migration is not idempotent after replacement.
BEGIN;
SET LOCAL lock_timeout='5s';
DO $baseline$
DECLARE p record;
BEGIN
    SELECT oid,prosecdef,provolatile,proparallel,prokind,proconfig,pronargs,
           pg_get_functiondef(oid) AS definition INTO p FROM pg_proc
    WHERE oid=to_regprocedure('control.capture_customer_identity_snapshot(uuid)');
    IF p.oid IS NULL OR p.prosecdef OR p.provolatile<>'v' OR p.proparallel<>'u'
       OR p.prokind<>'f' OR p.pronargs<>1
       OR p.proconfig IS DISTINCT FROM ARRAY['search_path=pg_catalog, pg_temp']::text[]
       OR encode(digest(p.definition,'sha256'),'hex') NOT IN (
          'b49877d40d8d70a0f88bf610ff88ced8794056cc6f92ccea7df14ebcf00cb020',
          '74733cd2db61874fca2f9c6473daa43c8a83852e5e700422599ef8ec4cb201be') THEN
        RAISE EXCEPTION '041 baseline capture definition drift; refusing replacement';
    END IF;
END $baseline$;
ALTER TABLE control.customer_identity_transfer
  ADD COLUMN member_count bigint NOT NULL DEFAULT 0 CHECK (member_count >= 0),
  ADD COLUMN selected_row_count bigint NOT NULL DEFAULT 0 CHECK (selected_row_count >= 0);
CREATE TABLE control.customer_identity_member_export (
    transfer_id uuid NOT NULL REFERENCES control.customer_identity_transfer(transfer_id),
    standardized_record_id uuid NOT NULL,
    raw_customer_id uuid NOT NULL,
    batch_id uuid NOT NULL,
    provenance_status text NOT NULL CHECK (provenance_status IN ('VERIFIED_UPLOAD','UNATTRIBUTED')),
    app_id text CHECK (app_id IS NULL OR app_id ~ '^[0-9]+$'),
    workspace_id uuid,
    source_system_id bigint NOT NULL,
    source_customer_id text NOT NULL CHECK (btrim(source_customer_id) <> ''),
    customer_entity_id uuid NOT NULL,
    match_probability numeric(8,7) NOT NULL,
    match_method text NOT NULL,
    resolution_run_id uuid NOT NULL,
    PRIMARY KEY(transfer_id,standardized_record_id),
    CHECK ((provenance_status='VERIFIED_UPLOAD' AND app_id IS NOT NULL AND workspace_id IS NOT NULL)
        OR (provenance_status='UNATTRIBUTED' AND app_id IS NULL AND workspace_id IS NULL)),
    UNIQUE(transfer_id,app_id,source_system_id,source_customer_id)
);
-- A cross-app transfer proves each (app, user) without knowing source_system_id
-- until the private bridge is read. The uniqueness index above cannot serve
-- that lookup because source_system_id lies before source_customer_id.
CREATE INDEX customer_identity_member_export_app_user_idx
    ON control.customer_identity_member_export(transfer_id,app_id,source_customer_id)
    INCLUDE (workspace_id,source_system_id,customer_entity_id,raw_customer_id,standardized_record_id);
-- The exact old row/document immutability contract applies to bridge rows.
CREATE TRIGGER customer_identity_member_export_immutable
    BEFORE UPDATE OR DELETE OR TRUNCATE ON control.customer_identity_member_export
    FOR EACH STATEMENT EXECUTE FUNCTION control.reject_customer_identity_snapshot_mutation();
-- Do not grant PUBLIC/default named roles inherited SELECT on cross-app bridge.
DO $private_acl$
DECLARE acl record;
BEGIN
    FOR acl IN SELECT DISTINCT a.grantee
      FROM pg_class c CROSS JOIN LATERAL aclexplode(coalesce(c.relacl,acldefault('r',c.relowner))) a
      WHERE c.oid='control.customer_identity_member_export'::regclass AND a.grantee<>c.relowner
    LOOP
      EXECUTE format('REVOKE ALL PRIVILEGES ON TABLE control.customer_identity_member_export FROM %s CASCADE',
        CASE WHEN acl.grantee=0 THEN 'PUBLIC' ELSE quote_ident(pg_get_userbyid(acl.grantee)) END);
    END LOOP;
END $private_acl$;
-- Preserve immutable transfer metadata when the worker moves operational state.
CREATE OR REPLACE FUNCTION control.freeze_customer_identity_transfer_scope()
RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $freeze$
BEGIN
    IF (NEW.transfer_id,NEW.resolution_run_id,NEW.stitching_job_id,NEW.ingestion_batch_id,
        NEW.workspace_id,NEW.app_id,NEW.publication_seq,NEW.member_count,NEW.selected_row_count,NEW.created_at)
        IS DISTINCT FROM
       (OLD.transfer_id,OLD.resolution_run_id,OLD.stitching_job_id,OLD.ingestion_batch_id,
        OLD.workspace_id,OLD.app_id,OLD.publication_seq,OLD.member_count,OLD.selected_row_count,OLD.created_at)
       OR (NEW.row_count IS DISTINCT FROM OLD.row_count AND NOT
           (OLD.row_count=0 AND OLD.status='PENDING' AND pg_trigger_depth()>1
            AND NEW.row_count>=OLD.selected_row_count)) THEN
        RAISE EXCEPTION 'Customer identity transfer scope is immutable';
    END IF;
    IF OLD.source_sha256 IS NOT NULL AND NEW.source_sha256 IS DISTINCT FROM OLD.source_sha256 THEN
        RAISE EXCEPTION 'Customer identity snapshot checksum is immutable';
    END IF;
    IF OLD.status='BLOCKED' AND NEW.status<>'BLOCKED' THEN
        RAISE EXCEPTION 'Blocked capture requires a new stitching run';
    END IF;
    RETURN NEW;
END $freeze$;
-- A trigger fails if its caller tries to insert outside the success transaction.
CREATE FUNCTION control.guard_customer_identity_member_insert()
RETURNS trigger LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $guard$
BEGIN
    IF pg_trigger_depth()<2 THEN
       RAISE EXCEPTION 'Global membership export requires a successful stitch transaction';
    END IF;
    RETURN NULL;
END $guard$;
CREATE TRIGGER customer_identity_member_capture_only
    BEFORE INSERT ON control.customer_identity_member_export FOR EACH STATEMENT
    EXECUTE FUNCTION control.guard_customer_identity_member_insert();
REVOKE ALL ON FUNCTION control.guard_customer_identity_member_insert() FROM PUBLIC;
CREATE OR REPLACE FUNCTION control.capture_customer_identity_snapshot(job_id uuid)
RETURNS void LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $capture$
DECLARE j control.stitching_job%ROWTYPE; run_id uuid; run_completed_at timestamptz;
    owner_scope record; n bigint; selected_count bigint; validation_error text;
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
    LOCK TABLE raw.customer_batch_member, raw.customer, raw.customer_current, staging.customer_profile,
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
               p.standardized_record_id,p.batch_id AS profile_batch,p.app_id AS profile_app,
               m.customer_entity_id,
               m.source_system_id AS mapped_source,m.source_customer_id AS mapped_user
        FROM raw.customer_batch_member b
        LEFT JOIN staging.customer_profile p ON p.raw_customer_id=b.raw_customer_id
        LEFT JOIN identity.customer_entity_member m ON m.standardized_record_id=p.standardized_record_id
        WHERE b.ingestion_batch_id=j.ingestion_batch_id
    ), components AS MATERIALIZED (
        SELECT DISTINCT customer_entity_id FROM selected WHERE customer_entity_id IS NOT NULL
    ), members AS MATERIALIZED (
        SELECT m.*,p.raw_customer_id,p.batch_id,p.source_customer_id AS profile_user,
               p.source_system_id AS profile_source,p.app_id AS profile_app,p.standardized_at,
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
          OR EXISTS(SELECT 1 FROM selected WHERE profile_batch IS DISTINCT FROM j.ingestion_batch_id
                 OR profile_app IS DISTINCT FROM owner_scope.app_id)
          OR EXISTS(SELECT 1 FROM selected s
              JOIN raw.customer_batch_member prior ON prior.raw_customer_id=s.raw_customer_id
              JOIN control.ingestion_batch prior_batch ON prior_batch.batch_id=prior.ingestion_batch_id
              CROSS JOIN LATERAL control.customer_app_scope_status(prior.ingestion_batch_id) prior_scope
              WHERE prior.ingestion_batch_id<>j.ingestion_batch_id
                 AND (prior_batch.app_id IS DISTINCT FROM owner_scope.app_id
                      OR prior_scope.app_id IS DISTINCT FROM owner_scope.app_id
                      OR prior_scope.error_code IS NOT NULL))
          OR EXISTS(SELECT 1 FROM selected s
              LEFT JOIN raw.customer h ON (h.batch_id,h.raw_customer_id,h.source_system_id,h.source_customer_id)=
                  (j.ingestion_batch_id,s.raw_customer_id,s.source_system_id,s.source_customer_id)
              LEFT JOIN raw.customer_current c ON c.raw_customer_id=s.raw_customer_id
              WHERE h.raw_customer_id IS NULL OR h.app_id IS DISTINCT FROM owner_scope.app_id
                 OR c.source_system_id IS DISTINCT FROM s.source_system_id
                 OR c.source_customer_id IS DISTINCT FROM s.source_customer_id
                 OR c.app_id IS DISTINCT FROM owner_scope.app_id) THEN 'P2416'
        WHEN EXISTS(SELECT 1 FROM evidence e LEFT JOIN owners o USING(ingestion_batch_id)
            WHERE e.ingestion_batch_id IS NULL
               OR e.raw_customer_id IS NULL
               OR (o.error_code IS NULL AND o.app_id IS NULL)
               OR (o.error_code IS NULL AND EXISTS(SELECT 1 FROM control.ingestion_batch ib
                  WHERE ib.batch_id=e.ingestion_batch_id AND ib.app_id IS DISTINCT FROM o.app_id))
               OR (o.error_code IS NULL AND EXISTS(SELECT 1 FROM control.customer_app_scope_status(e.ingestion_batch_id) s
                  WHERE s.error_code IS NOT NULL OR s.app_id IS DISTINCT FROM o.app_id)))
          -- Different apps in one PERSON are allowed, but one globally keyed
          -- raw UUID may not silently stand for distinct app/source identities.
          OR EXISTS(SELECT 1 FROM evidence e JOIN owners o USING(ingestion_batch_id)
              WHERE e.raw_customer_id IS NOT NULL AND o.error_code IS NULL
              GROUP BY e.raw_customer_id HAVING count(DISTINCT o.app_id)>1)
          OR EXISTS(SELECT 1 FROM members m WHERE NOT EXISTS(
              SELECT 1 FROM raw.customer_batch_member b WHERE b.raw_customer_id=m.raw_customer_id
                AND b.ingestion_batch_id=m.batch_id))
          OR EXISTS(SELECT 1 FROM members m LEFT JOIN owners o ON o.ingestion_batch_id=m.batch_id
              WHERE o.error_code IS NULL AND m.profile_app IS DISTINCT FROM o.app_id) THEN 'P2417'
    END INTO validation_error;
    IF validation_error IS NOT NULL THEN
        RAISE EXCEPTION USING ERRCODE=validation_error,MESSAGE='Identity publication validation rejected';
    END IF;
    SELECT count(*) INTO selected_count FROM raw.customer_batch_member WHERE ingestion_batch_id=j.ingestion_batch_id;
    SELECT count(*) INTO n
    FROM identity.customer_entity_member m WHERE m.customer_entity_id IN (
        SELECT DISTINCT x.customer_entity_id FROM raw.customer_batch_member b
        JOIN staging.customer_profile p USING(raw_customer_id)
        JOIN identity.customer_entity_member x USING(standardized_record_id)
        WHERE b.ingestion_batch_id=j.ingestion_batch_id);
    INSERT INTO control.customer_identity_transfer
        (transfer_id,resolution_run_id,stitching_job_id,ingestion_batch_id,workspace_id,app_id,status,row_count,member_count,selected_row_count)
        VALUES(run_id,run_id,j.job_id,j.ingestion_batch_id,owner_scope.workspace_id,owner_scope.app_id,'PENDING',0,n,selected_count);
    -- Mixed-app people are valid. Unknown contributor provenance is retained in
    -- an unattributed PRIVATE bridge row, never assigned to the selected app.
    -- Missing/mismatched membership, alias collisions and connector lineage still
    -- fail closed; unknown never becomes a source-owned profile without proof.
    WITH components AS MATERIALIZED (
        SELECT DISTINCT m.customer_entity_id
        FROM raw.customer_batch_member b
        JOIN staging.customer_profile p USING(raw_customer_id)
        JOIN identity.customer_entity_member m USING(standardized_record_id)
        WHERE b.ingestion_batch_id=j.ingestion_batch_id
    ), affected AS MATERIALIZED (
        SELECT m.standardized_record_id,m.customer_entity_id,m.match_probability,
               m.match_method,m.resolution_run_id,p.raw_customer_id,p.batch_id,
               p.source_system_id,p.source_customer_id,p.app_id
        FROM components c JOIN identity.customer_entity_member m USING(customer_entity_id)
        JOIN staging.customer_profile p USING(standardized_record_id)
    ), owners AS MATERIALIZED (
        SELECT * FROM control.resolve_customer_identity_batches(
           ARRAY(SELECT DISTINCT batch_id FROM affected))
    )
    INSERT INTO control.customer_identity_member_export
        (transfer_id,standardized_record_id,raw_customer_id,batch_id,provenance_status,app_id,
         workspace_id,source_system_id,source_customer_id,customer_entity_id,match_probability,
         match_method,resolution_run_id)
    SELECT run_id,a.standardized_record_id,a.raw_customer_id,a.batch_id,
           CASE WHEN o.error_code IS NULL AND o.app_id=a.app_id THEN 'VERIFIED_UPLOAD' ELSE 'UNATTRIBUTED' END,
           CASE WHEN o.error_code IS NULL AND o.app_id=a.app_id THEN o.app_id END,
           CASE WHEN o.error_code IS NULL AND o.app_id=a.app_id THEN o.workspace_id END,
           a.source_system_id,a.source_customer_id,a.customer_entity_id,a.match_probability,
           a.match_method,a.resolution_run_id
    FROM affected a LEFT JOIN owners o ON o.ingestion_batch_id=a.batch_id;
    GET DIAGNOSTICS selected_count = ROW_COUNT;
    IF selected_count <> n THEN
        RAISE EXCEPTION USING ERRCODE='P2414',MESSAGE='Incomplete global member export';
    END IF;
    INSERT INTO control.customer_identity_export(transfer_id,row_number,app_id,user_id,payload)
    SELECT run_id,row_number() OVER (ORDER BY o.app_id COLLATE "C",p.source_customer_id COLLATE "C"),
        o.app_id,p.source_customer_id,
        jsonb_build_object(
            'customer_entity_id',e.customer_entity_id,'match_probability',m.match_probability,
            'match_method',m.match_method,'source_system_id',p.source_system_id,
            'standardized_record_id',p.standardized_record_id,'raw_customer_id',p.raw_customer_id,
            'resolution_run_id',run_id,'workspace_id',o.workspace_id,
            'app_id',o.app_id,'user_id',p.source_customer_id,
            'profile',jsonb_build_object(
                'customer_id',p.source_customer_id,'customer_key_id',NULL,'profile_source_record_id',NULL,
                'first_name',p.first_name_raw,'middle_name',p.middle_name_raw,
                'last_name',p.last_name_raw,'full_name',p.full_name_raw,
                'email',p.email_raw,'phone',p.phone_raw,
                'address_line_1',p.address_raw,'district',p.district_normalized,
                'province',p.province_normalized,'postal_code',p.postal_code_normalized,
                'country_code',p.country_code,'date_of_birth',p.date_of_birth,
                'gender',p.gender_normalized,
                'source_created_at',to_char(p.source_created_at AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
                'source_updated_at',to_char(p.source_updated_at AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
                'profile_updated_at',to_char(p.source_updated_at AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
                'standardized_at',to_char(p.standardized_at AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
                'first_seen_at',to_char(p.source_created_at AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
                'last_seen_at',to_char(p.source_updated_at AT TIME ZONE 'UTC','YYYY-MM-DD"T"HH24:MI:SS.US"Z"')
            ))
    FROM control.customer_identity_member_export bridge
    JOIN staging.customer_profile p ON p.standardized_record_id=bridge.standardized_record_id
    JOIN identity.customer_entity_member m ON m.standardized_record_id=p.standardized_record_id
    JOIN identity.customer_entity e ON e.customer_entity_id=m.customer_entity_id
    JOIN control.customer_identity_upload_scope o ON o.app_id=bridge.app_id
    WHERE bridge.transfer_id=run_id AND bridge.provenance_status='VERIFIED_UPLOAD'
      AND p.raw_customer_id=bridge.raw_customer_id AND p.batch_id=bridge.batch_id
      AND p.app_id=bridge.app_id AND p.source_system_id=bridge.source_system_id
      AND p.source_customer_id=bridge.source_customer_id;
    GET DIAGNOSTICS selected_count = ROW_COUNT;
    IF selected_count <> (SELECT count(*) FROM control.customer_identity_member_export
          WHERE transfer_id=run_id AND provenance_status='VERIFIED_UPLOAD')
       OR selected_count < j.selected_record_count THEN
        RAISE EXCEPTION USING ERRCODE='P2414', MESSAGE='Identity export count mismatch';
    END IF;
    -- Worker-visible row_count is the complete set of verified source profiles;
    -- selected_row_count retains the original upload's immutable cardinality.
    UPDATE control.customer_identity_transfer SET row_count=selected_count WHERE transfer_id=run_id;
END $capture$;

COMMIT;
