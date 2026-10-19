-- Forward-only candidate for NEW file uploads; never install 037. This SQL does
-- not update historical raw IDs, current profiles, or immutable observations.
-- Deploy ONLY with a reviewed SQL-aware worker rollout and a separate matcher/
-- backfill release. Existing Lambda registers a legacy source and passes that ID
-- to the bulk writer: after activation its file writes intentionally FAIL CLOSED.
-- Keep admission paused until global staging alias isolation is independently
-- demonstrated. A physical source is NOT a tenant partition in the linker.
BEGIN;
SET LOCAL lock_timeout='10s';
DO $baseline$
DECLARE p record;
BEGIN
    IF (to_regprocedure('control.bind_connector_batch_app_id(uuid,uuid,text)') IS NULL)
       <> (to_regprocedure('control.connector_batch_app_scope(uuid)') IS NULL)
       OR (to_regprocedure('control.connector_batch_app_scope(uuid)') IS NOT NULL AND
           to_regprocedure('control.customer_identity_connector_lineage(uuid[])') IS NULL) THEN
        RAISE EXCEPTION '038 incomplete connector catalog';
    END IF;
    IF to_regclass('control.customer_forward_route') IS NOT NULL
       OR to_regprocedure('control.customer_forward_file_source(uuid,text,text)') IS NOT NULL
       OR to_regprocedure('raw.apply_customer_forward_bulk(uuid,bigint)') IS NOT NULL
       OR to_regprocedure('raw.apply_customer_bulk(uuid,bigint)') IS NULL
       OR to_regprocedure('control.resolve_customer_identity_batches(uuid[])') IS NULL
       OR to_regprocedure('control.customer_app_scope_lineage(uuid,uuid,bigint,text)') IS NULL
       OR to_regclass('identity.customer_entity_member') IS NULL THEN
        RAISE EXCEPTION '038 requires unmodified 019/022/024/026 and no prior activation';
    END IF;
    -- Pinned exact pg_get_functiondef SHA256 from the prior staging READ ONLY
    -- catalog capture. Repeat the live read-only verification before release.
    FOR p IN SELECT oid,proname,prosecdef,provolatile,proconfig,
                    encode(digest(pg_get_functiondef(oid),'sha256'),'hex') AS sha
             FROM pg_proc WHERE oid IN (
               'control.customer_app_scope_lineage(uuid,uuid,bigint,text)'::regprocedure,
               'control.resolve_customer_identity_batches(uuid[])'::regprocedure)
    LOOP
        IF (p.proname='customer_app_scope_lineage' AND
            (p.sha<>'1e31f31ca1e2e2d37d7e5b8a1a3254e5073d227cda1e008d9318741df4341fcf'
             OR NOT p.prosecdef OR p.provolatile<>'s')) OR
           (p.proname='resolve_customer_identity_batches' AND
            (p.sha NOT IN ('702b3c54fa68595bc3b8734d3b90be3c9dbb2d5991d27ddcb6c696056b3f5dc6',
                'ad19087ea02dcb9eecff0c4523e50f84038bce84709cc9f8a22248e9ac2aab0d')
             OR p.prosecdef OR p.provolatile<>'v')) THEN
            RAISE EXCEPTION '038 installed 024/026 function contract drift: %',p.proname;
        END IF;
    END LOOP;
    IF (SELECT count(*) FROM pg_proc WHERE oid IN (
        'control.customer_app_scope_lineage(uuid,uuid,bigint,text)'::regprocedure,
        'control.resolve_customer_identity_batches(uuid[])'::regprocedure)) <> 2 THEN
        RAISE EXCEPTION '038 missing pinned 024/026 functions';
    END IF;
    IF to_regprocedure('control.connector_batch_app_scope(uuid)') IS NOT NULL THEN
        IF NOT EXISTS (SELECT FROM pg_proc WHERE oid=to_regprocedure('control.connector_batch_app_scope(uuid)')
            AND encode(digest(pg_get_functiondef(oid),'sha256'),'hex')=
                'da30cb1b59e023479fe20a44f90c3988514bedabe57b643ea9c8cdc8612fed5a'
            AND prosecdef AND provolatile='s') OR
           NOT EXISTS (SELECT FROM pg_proc WHERE oid=to_regprocedure('control.bind_connector_batch_app_id(uuid,uuid,text)')
            AND encode(digest(pg_get_functiondef(oid),'sha256'),'hex')=
                '2ca65254b952050cb2919f5912eff2e56845187b33ac07f62feb358b95eaaf25'
            AND prosecdef AND provolatile='v') THEN
            RAISE EXCEPTION '038 installed 040 connector contract drift';
        END IF;
    END IF;
    IF EXISTS(SELECT FROM pg_attribute WHERE attrelid='control.source_system'::regclass
         AND attname='source_code' AND atttypmod<>54) THEN
        RAISE EXCEPTION '038 expected source_system.source_code varchar(50)';
    END IF;
END $baseline$;
LOCK TABLE control.source_system,control.source_landing_batch,
    control.ingestion_batch,control.stitching_job IN SHARE ROW EXCLUSIVE MODE;
LOCK TABLE raw.customer_current,raw.customer,raw.customer_batch_member IN SHARE ROW EXCLUSIVE MODE;
-- No old-row provenance preflight: known historical aliases are deliberately
-- retained. The old source-only uniqueness and every FK/target OID stay intact.
CREATE TABLE control.customer_forward_route (
    app_id text NOT NULL CHECK(app_id ~ '^[0-9]+$'),
    landing_source_code varchar(64) NOT NULL,
    source_system_id bigint NOT NULL UNIQUE REFERENCES control.source_system(source_system_id) ON DELETE RESTRICT,
    physical_code varchar(50) NOT NULL UNIQUE,
    PRIMARY KEY(app_id,landing_source_code),
    CHECK(physical_code='cf1-' || md5(length(app_id)::text || ':' || app_id || ':' || landing_source_code))
);
COMMENT ON TABLE control.customer_forward_route IS
 '038 private mapping of literal upload app and landing source to never-shared physical source; no historical raw rewriting. Matcher/backfill not fixed.';
REVOKE ALL ON control.customer_forward_route FROM PUBLIC;

CREATE FUNCTION control.customer_forward_file_source(p_landing uuid,p_code text,p_ref text)
RETURNS bigint LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $body$
DECLARE landing_code text; landing_ref text; app text; physical text;
        routed bigint;
BEGIN
    SELECT l.source_code,l.batch_ref INTO landing_code,landing_ref
      FROM control.source_landing_batch l WHERE l.batch_id=p_landing;
    app := (regexp_match(landing_ref,
        '^[0-9]{4}-[0-9]{2}-[0-9]{2}-[0-9]{6}-([0-9]+)-[0-9a-f]{12}$'))[1];
    IF landing_code IS DISTINCT FROM p_code OR landing_ref IS DISTINCT FROM p_ref OR
       app IS NULL OR length(p_code)>64 OR p_code IS NULL OR p_code='' THEN
        RAISE EXCEPTION '038 forward source requires exact landing lineage';
    END IF;
    physical := 'cf1-' || md5(length(app)::text || ':' || app || ':' || p_code);
    PERFORM pg_advisory_xact_lock(hashtextextended('customer-forward:' || physical,0));
    SELECT r.source_system_id INTO routed FROM control.customer_forward_route r
      JOIN control.source_system s ON s.source_system_id=r.source_system_id
      WHERE r.app_id=app AND r.landing_source_code=p_code AND r.physical_code=physical
        AND s.source_code=physical;
    IF FOUND THEN RETURN routed; END IF;
    -- 038 accepts a new landing code with NO legacy source registration. A
    -- pre-existing physical code is never adopted, even on hash collision.
    IF EXISTS(SELECT FROM control.source_system s WHERE s.source_code=physical)
       OR EXISTS(SELECT FROM control.customer_forward_route r
           WHERE r.app_id=app AND r.landing_source_code=p_code) THEN
        RAISE EXCEPTION '038 physical source collision or route drift';
    END IF;
    INSERT INTO control.source_system(source_code,source_name,source_type,is_active)
      VALUES(physical,'CDP forward file namespace','s3_csv',true)
      RETURNING source_system_id INTO routed;
    INSERT INTO control.customer_forward_route(app_id,landing_source_code,source_system_id,physical_code)
      VALUES(app,p_code,routed,physical);
    RETURN routed;
END $body$;
REVOKE ALL ON FUNCTION control.customer_forward_file_source(uuid,text,text) FROM PUBLIC;
COMMENT ON FUNCTION control.customer_forward_file_source(uuid,text,text) IS
 '038 new-worker route registration: requires durable landing and literal batch_ref; no legacy source insertion required.';
CREATE TABLE control.customer_forward_source_activation (
    singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton),
    contract text NOT NULL CHECK(contract='forward-only/sql-writer/matcher-paused'));
INSERT INTO control.customer_forward_source_activation VALUES (true,'forward-only/sql-writer/matcher-paused');

CREATE FUNCTION control.forward_customer_batch_source() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $body$
DECLARE landing_code text; landing_ref text; original_code text; resolved_app text;
        route record; physical text; new_source bigint;
BEGIN
    IF NEW.metadata ? 'connector_sync_run_id' THEN
        IF EXISTS(SELECT FROM control.customer_forward_route r WHERE r.source_system_id=NEW.source_system_id) THEN
            RAISE EXCEPTION '038 connector cannot claim a physical file source';
        END IF;
        RETURN NEW;
    END IF;
    IF EXISTS(SELECT FROM control.source_system s WHERE s.source_system_id=NEW.source_system_id
              AND (s.source_type='database_connector' OR s.source_code LIKE 'dc1-%')) THEN
        RAISE EXCEPTION '038 upload cannot claim connector source';
    END IF;
    -- Legacy callers MAY submit the legacy source id; the route replaces it
    -- before INSERT. A coordinated worker may submit the already registered
    -- physical id. Neither path may adopt a connector/legacy source as physical.
    IF EXISTS(SELECT FROM control.customer_forward_route r WHERE r.source_system_id=NEW.source_system_id) THEN
        SELECT * INTO route FROM control.customer_forward_route r
          WHERE r.source_system_id=NEW.source_system_id;
        SELECT landing.source_code,landing.batch_ref INTO landing_code,landing_ref
          FROM control.source_landing_batch landing WHERE landing.batch_id=NEW.landing_batch_id;
        resolved_app := (regexp_match(landing_ref,
          '^[0-9]{4}-[0-9]{2}-[0-9]{2}-[0-9]{6}-([0-9]+)-[0-9a-f]{12}$'))[1];
        IF route.app_id IS DISTINCT FROM resolved_app OR
           route.landing_source_code IS DISTINCT FROM landing_code OR
           landing_ref IS DISTINCT FROM NEW.batch_ref OR
           NEW.app_id IS DISTINCT FROM resolved_app OR
           NOT EXISTS (SELECT FROM control.source_system s WHERE s.source_system_id=route.source_system_id
                       AND s.source_code=route.physical_code) THEN
            RAISE EXCEPTION '038 physical registration lineage mismatch';
        END IF;
        PERFORM pg_advisory_xact_lock(hashtextextended('customer-forward:' || route.physical_code,0));
        RETURN NEW;
    END IF;
    SELECT landing.source_code,landing.batch_ref INTO landing_code,landing_ref
      FROM control.source_landing_batch landing
      WHERE landing.batch_id=NEW.landing_batch_id;
    IF NOT FOUND OR landing_ref IS DISTINCT FROM NEW.batch_ref THEN
        RAISE EXCEPTION '038 file landing lineage required';
    END IF;
    resolved_app := (regexp_match(landing_ref,
        '^[0-9]{4}-[0-9]{2}-[0-9]{2}-[0-9]{6}-([0-9]+)-[0-9a-f]{12}$'))[1];
    IF resolved_app IS NULL OR NEW.app_id IS DISTINCT FROM resolved_app THEN
        RAISE EXCEPTION '038 literal file app lineage required';
    END IF;
    SELECT s.source_code INTO original_code FROM control.source_system s
       WHERE s.source_system_id=NEW.source_system_id;
    -- The old Lambda registers the legacy source first; rerouting happens before
    -- row persistence, so it cannot repoint any historical source_system row.
    IF original_code IS DISTINCT FROM landing_code THEN
        RAISE EXCEPTION '038 legacy registration source/landing mismatch';
    END IF;
    physical := 'cf1-' || md5(length(resolved_app)::text || ':' || resolved_app || ':' || landing_code);
    PERFORM pg_advisory_xact_lock(hashtextextended('customer-forward:' || physical,0));
    SELECT * INTO route FROM control.customer_forward_route r
      WHERE r.app_id=resolved_app AND r.landing_source_code=landing_code;
    IF FOUND THEN
        IF route.physical_code IS DISTINCT FROM physical OR NOT EXISTS(
            SELECT FROM control.source_system s
            WHERE s.source_system_id=route.source_system_id AND s.source_code=physical) THEN
            RAISE EXCEPTION '038 physical route catalog drift';
        END IF;
        NEW.source_system_id := route.source_system_id;
    ELSE
        -- A preexisting source (including connector/legacy) with this code is
        -- never adopted; unique violation/collision rolls back registration.
        INSERT INTO control.source_system(source_code,source_name,source_type,is_active)
          VALUES(physical,'CDP forward file namespace','s3_csv',true)
          RETURNING source_system_id INTO new_source;
        INSERT INTO control.customer_forward_route(app_id,landing_source_code,source_system_id,physical_code)
          VALUES(resolved_app,landing_code,new_source,physical);
        NEW.source_system_id := new_source;
    END IF;
    RETURN NEW;
END $body$;
REVOKE ALL ON FUNCTION control.forward_customer_batch_source() FROM PUBLIC;
-- 026's customer_app_id_derive fires alphabetically before this trigger.
CREATE TRIGGER customer_forward_source_route BEFORE INSERT ON control.ingestion_batch
FOR EACH ROW EXECUTE FUNCTION control.forward_customer_batch_source();

CREATE FUNCTION control.guard_customer_forward_batch() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $body$
BEGIN
    -- 040 binds the app only after the durable sync_run is inserted. Leave its
    -- verified binding UPDATE and connector progress updates to 040's guards.
    IF OLD.metadata ? 'connector_sync_run_id' THEN
        IF NOT (NEW.metadata ? 'connector_sync_run_id') OR
           (NEW.source_system_id,NEW.landing_batch_id,NEW.batch_ref,NEW.metadata)
              IS DISTINCT FROM
           (OLD.source_system_id,OLD.landing_batch_id,OLD.batch_ref,OLD.metadata) THEN
            RAISE EXCEPTION '038 connector batch lineage is immutable';
        END IF;
        RETURN NEW;
    END IF;
    IF (NEW.source_system_id,NEW.landing_batch_id,NEW.batch_ref,NEW.app_id,
        NEW.metadata ? 'connector_sync_run_id') IS DISTINCT FROM
       (OLD.source_system_id,OLD.landing_batch_id,OLD.batch_ref,OLD.app_id,
        OLD.metadata ? 'connector_sync_run_id') OR
       NEW.metadata IS DISTINCT FROM OLD.metadata OR
       (NEW.metadata ? 'connector_sync_run_id') OR OLD.metadata IS NULL THEN
        RAISE EXCEPTION '038 batch source/app lineage is immutable';
    END IF;
    RETURN NEW;
END $body$;
CREATE TRIGGER customer_forward_batch_immutable BEFORE UPDATE ON control.ingestion_batch
FOR EACH ROW EXECUTE FUNCTION control.guard_customer_forward_batch();

CREATE FUNCTION control.guard_customer_forward_source() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $body$
BEGIN
    IF EXISTS (SELECT FROM control.customer_forward_route r
               WHERE r.source_system_id=OLD.source_system_id) THEN
        RAISE EXCEPTION '038 physical source cannot be reclassified or removed';
    END IF;
    RETURN OLD;
END $body$;
CREATE TRIGGER customer_forward_source_immutable BEFORE UPDATE OR DELETE ON control.source_system
FOR EACH ROW EXECUTE FUNCTION control.guard_customer_forward_source();

-- Replace the two exact installed definitions, not the old 024/026 SQL files.
-- CREATE OR REPLACE retains OID, owner, ACL, invoker/definer mode, configuration.
DO $lineage$
DECLARE d text; old text; n integer;
BEGIN
    d:=pg_get_functiondef('control.customer_app_scope_lineage(uuid,uuid,bigint,text)'::regprocedure);
    old:='source_code IS DISTINCT FROM landing_source';
    IF (length(d)-length(replace(d,old,'')))/length(old)<>1 THEN
        RAISE EXCEPTION '038 unexpected 026 lineage shape';
    END IF;
    d:=replace(d,old,$sql$(source_code IS DISTINCT FROM landing_source AND NOT EXISTS (
        SELECT FROM control.customer_forward_route r WHERE r.source_system_id=p_source
          AND r.app_id=(regexp_match(p_ref,
            '^[0-9]{4}-[0-9]{2}-[0-9]{2}-[0-9]{6}-([0-9]+)-[0-9a-f]{12}$'))[1]
          AND r.landing_source_code=landing_source))$sql$);
    EXECUTE d;
    d:=pg_get_functiondef('control.resolve_customer_identity_batches(uuid[])'::regprocedure);
    old:='AND ss.source_code=l.source_code';
    IF (length(d)-length(replace(d,old,'')))/length(old)<>1 THEN
        RAISE EXCEPTION '038 unexpected 024 registration shape';
    END IF;
    d:=replace(d,old,$sql$AND (ss.source_code=l.source_code OR EXISTS (
        SELECT FROM control.customer_forward_route r WHERE r.source_system_id=b.source_system_id
          AND r.landing_source_code=l.source_code
          AND r.app_id=(regexp_match(l.batch_ref,
            '^[0-9]{4}-[0-9]{2}-[0-9]{2}-[0-9]{6}-([0-9]+)-[0-9a-f]{12}$'))[1]))$sql$);
    -- The 045 resolver has a separate, verified connector branch; extend only
    -- the upload branch's source comparison and leave its connector joins intact.

    -- A fresh route's physical code is not the landing source code. The exact
    -- route tuple is already checked by the INSERT/SELECT predicates above.
    -- Preserve the old NULL-aware IS DISTINCT FROM behavior for legacy rows.
    old:='OR b.source_code IS DISTINCT FROM b.landing_source';
    IF (length(d)-length(replace(d,old,'')))/length(old)<>1 THEN
        RAISE EXCEPTION '038 unexpected 024 resolution shape';
    END IF;
    d:=replace(d,old,$sql$OR (b.source_code IS DISTINCT FROM b.landing_source AND NOT EXISTS (
        SELECT FROM control.customer_forward_route r WHERE r.source_system_id=b.source_system_id
          AND r.landing_source_code=b.landing_source AND r.app_id=b.parsed_app))$sql$);
    EXECUTE d;
END $lineage$;

-- Keep the original 022 connector function/OID/ACL and the source-only key.
-- Refuse every physically routed file in that legacy entrypoint, including a
-- caller that supplies its new ID. A separately named privileged SQL entrypoint
-- copies exactly 022's typed merge (all ROW comparisons/statement capture).
DO $bulk$
DECLARE d text; old text; n integer;
BEGIN
    d:=pg_get_functiondef('raw.apply_customer_bulk(uuid,bigint)'::regprocedure);
    old:='IF NOT EXISTS (SELECT FROM control.ingestion_batch';
    IF (length(d)-length(replace(d,old,'')))/length(old)<>1 THEN
        RAISE EXCEPTION '038 unexpected 022 bulk shape';
    END IF;
    EXECUTE replace(d,'CREATE OR REPLACE FUNCTION raw.apply_customer_bulk(',
        'CREATE FUNCTION raw.apply_customer_forward_bulk(');
    d:=replace(d,old,$sql$IF EXISTS(SELECT FROM control.customer_forward_route r WHERE r.source_system_id=p_source)
        THEN RAISE EXCEPTION '038 old worker cannot write physical file source'; END IF;
    IF NOT EXISTS (SELECT FROM control.ingestion_batch$sql$);
    EXECUTE d;
    d:=pg_get_functiondef('raw.apply_customer_forward_bulk(uuid,bigint)'::regprocedure);
    d:=replace(d,'CREATE OR REPLACE FUNCTION raw.apply_customer_forward_bulk(',
        'CREATE OR REPLACE FUNCTION raw.apply_customer_forward_bulk(');
    -- 022 writes history via an invoker-rights statement trigger. Secure the
    -- route at the trusted SQL entrypoint and at current-row trigger boundary;
    -- history/membership guards see the nested statement trigger as invoker.
    d:=replace(d,'LANGUAGE plpgsql','LANGUAGE plpgsql SECURITY DEFINER');
    old:='IF NOT EXISTS (SELECT FROM control.ingestion_batch';
    d:=replace(d,old,$sql$IF NOT EXISTS (SELECT FROM control.ingestion_batch b
        JOIN control.customer_forward_route r ON r.source_system_id=b.source_system_id
        WHERE b.batch_id=p_batch AND b.source_system_id=p_source AND b.app_id=r.app_id)
        THEN RAISE EXCEPTION '038 forward file route required'; END IF;
    IF EXISTS(SELECT FROM pg_temp.customer_bulk_stage WHERE app_id IS NOT NULL)
        THEN RAISE EXCEPTION '038 stage app_id must derive from batch'; END IF;
    IF NOT EXISTS (SELECT FROM control.ingestion_batch$sql$);
    EXECUTE d;
END $bulk$;
REVOKE ALL ON FUNCTION raw.apply_customer_forward_bulk(uuid,bigint) FROM PUBLIC;
COMMENT ON FUNCTION raw.apply_customer_forward_bulk(uuid,bigint) IS
 '038 trusted-owner SQL-only forward writer: grant EXECUTE explicitly to coordinated new file worker; never grant raw table/route ownership. Old Lambda still fails closed.';

-- This is a role-bound server check, NOT a client-set GUC. Ordinary direct
-- writers keep their connector path, but cannot create file observations in a
-- routed source. Only the SECURITY DEFINER forward merge runs as route owner.
-- The same guard covers existing-current late history and membership inserts;
-- guarding current alone would let an invoker forge a second batch directly.
CREATE FUNCTION raw.guard_customer_forward_write() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $body$
DECLARE b record; r record; batch uuid;
BEGIN
    batch := CASE WHEN TG_TABLE_NAME='customer_batch_member'
       THEN (to_jsonb(NEW)->>'ingestion_batch_id')::uuid
       ELSE (to_jsonb(NEW)->>'batch_id')::uuid END;
    SELECT source_system_id,app_id,metadata,landing_batch_id INTO b
      FROM control.ingestion_batch WHERE batch_id=batch;
    IF NOT FOUND OR NEW.source_system_id IS DISTINCT FROM b.source_system_id THEN
        RAISE EXCEPTION '038 unrouted or mismatched source write';
    END IF;
    SELECT * INTO r FROM control.customer_forward_route
      WHERE source_system_id=b.source_system_id;
    IF FOUND THEN
        IF b.app_id IS DISTINCT FROM r.app_id OR
           current_user IS DISTINCT FROM pg_get_userbyid((SELECT relowner FROM pg_class
                    WHERE oid='control.customer_forward_route'::regclass)) THEN
            RAISE EXCEPTION '038 direct file write refused: forward SQL writer required';
        END IF;
        -- 039 ranks one logical (literal app,user) winner across source epochs.
        -- Never join to the old source-only current/raw UUID for this new row;
        -- the source-only unique index and batch/source FK provide separation.
        -- Historical aliases remain untouched and unsafe for the global linker.
    ELSIF NOT (b.metadata ? 'connector_sync_run_id') THEN
        -- No post-activation legacy/unrouted file INSERT, even when it carries
        -- the correct legacy source id. Existing rows remain untouched.
        RAISE EXCEPTION '038 legacy file write refused';
    ELSIF to_regprocedure('control.connector_batch_app_scope(uuid)') IS NOT NULL THEN
        IF b.app_id IS NULL OR NOT EXISTS(
            SELECT FROM control.connector_batch_app_scope(batch) scope
            WHERE scope.app_id=b.app_id AND scope.error_code IS NULL) THEN
            RAISE EXCEPTION '038 connector write requires verified app lineage';
        END IF;
    ELSIF b.app_id IS NOT NULL OR NOT EXISTS(
        SELECT FROM connector.sync_run s WHERE s.ingestion_batch_id=batch
            OR s.landing_batch_id=b.landing_batch_id) THEN
        RAISE EXCEPTION '038 connector write requires durable sync lineage';
    END IF;
    RETURN NEW;
END $body$;
CREATE TRIGGER customer_forward_current_guard BEFORE INSERT OR UPDATE ON raw.customer_current
FOR EACH ROW EXECUTE FUNCTION raw.guard_customer_forward_write();
CREATE TRIGGER customer_forward_history_guard BEFORE INSERT ON raw.customer
FOR EACH ROW EXECUTE FUNCTION raw.guard_customer_forward_write();
CREATE TRIGGER customer_forward_member_guard BEFORE INSERT ON raw.customer_batch_member
FOR EACH ROW EXECUTE FUNCTION raw.guard_customer_forward_write();

-- 022 uses source-only current uniqueness. Distinct physical IDs isolate new
-- uploads; immutable historical aliases are not made safe for global matching.
-- Refuse starting new physical jobs while any legacy alias is recorded anywhere.
CREATE FUNCTION control.guard_forward_stitch_alias() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $body$
BEGIN
    IF NEW.status IN ('SUBMITTED','WAITING_FOR_LOCK','VALIDATING','RUNNING','SUCCEEDED')
       AND EXISTS (SELECT FROM control.ingestion_batch b
                   JOIN control.customer_forward_route r USING(source_system_id)
                   WHERE b.batch_id=NEW.ingestion_batch_id)
       AND EXISTS (SELECT FROM raw.customer h JOIN control.ingestion_batch b ON b.batch_id=h.batch_id
          GROUP BY h.raw_customer_id HAVING count(DISTINCT b.app_id)>1)
    THEN RAISE EXCEPTION '038 GLOBAL_ALIAS_STITCH_BLOCKED: old aliased staging remains unsafe';
    END IF;
    RETURN NEW;
END $body$;
CREATE TRIGGER customer_forward_stitch_gate BEFORE INSERT OR UPDATE OF status ON control.stitching_job
FOR EACH ROW EXECUTE FUNCTION control.guard_forward_stitch_alias();

-- Activation lives above the route function. Do not confuse this marker with
-- 037's customer_source_namespace_activation (which must remain absent).
COMMIT;
