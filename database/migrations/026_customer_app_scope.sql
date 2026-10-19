-- Additive upload provenance only. MANUAL rollout; no automatic historical writes.
-- Requires 019 and the coordinated 022 bulk writer. Existing views stay unchanged.
-- app_id is a literal string, NOT a new tenant key; global current/source collisions remain.
-- REVIEW BLOCKER: scripts/recover_customer_history.py rejects the added typed column
-- (TYPED_SCHEMA_MISMATCH). Do not run that old recovery script after this migration.
-- Its exact typed comparisons need a separately reviewed provenance-aware update.
-- 022 generated typed SQL deliberately remains unchanged: it compares every old
-- business column, while new BEFORE triggers independently derive app_id from the
-- batch for current, new history and late bulk observations. Do NOT regenerate
-- 022 to include app_id from a temporary COPY stage (old writers leave it NULL).
-- Rollout: pause/drain writers for DDL; apply manually; backfill every ingestion
-- batch, control first and then each other table, committing each bounded chunk.
-- Inventory all batches (no canary/app filter); persist NULL diagnostic exclusions.
-- Resume only after exact per-batch app_id reconciliation. Current tables remain
-- globally keyed. Add optional (app_id,batch_id) indexes CONCURRENTLY in a separate
-- reviewed operation; no blocking large index build or row rewrite in this DDL.
BEGIN;
SET LOCAL lock_timeout = '10s';
-- Compare complete preexisting definitions after compiling this exact migration.
-- Any drift aborts the enclosing transaction; never silently replaces a different
-- function body, security mode, volatility, configuration or signature.
CREATE TEMP TABLE customer_app_scope_prior ON COMMIT DROP AS
SELECT p.oid,pg_get_functiondef(p.oid) AS definition
FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
WHERE (n.nspname='control' AND p.proname IN ('customer_app_scope_lineage',
    'set_customer_batch_app_id','customer_app_scope_status','backfill_customer_app_id'))
 OR (n.nspname='raw' AND p.proname='set_customer_observation_app_id');

DO $columns$
DECLARE t regclass; a record;
BEGIN
    IF to_regprocedure('raw.apply_customer_bulk(uuid,bigint)') IS NULL THEN
        RAISE EXCEPTION '026 requires installed 022 bulk writer';
    END IF;
    FOREACH t IN ARRAY ARRAY['control.ingestion_batch'::regclass,'raw.customer'::regclass,
        'raw.customer_current'::regclass,'staging.customer_profile_history'::regclass,
        'staging.customer_profile'::regclass] LOOP
        SELECT atttypid,atttypmod,attnotnull,atthasdef,attgenerated,attidentity INTO a
          FROM pg_attribute WHERE attrelid=t AND attname='app_id' AND NOT attisdropped;
        IF FOUND THEN
            IF a.atttypid <> 'text'::regtype OR a.atttypmod <> -1 OR a.attnotnull
                OR a.atthasdef OR a.attgenerated <> '' OR a.attidentity <> '' THEN
                RAISE EXCEPTION '026 incompatible existing %.app_id', t;
            END IF;
        ELSE
            EXECUTE format('ALTER TABLE %s ADD COLUMN app_id text',t);
        END IF;
    END LOOP;
END $columns$;

-- Pure lookup; never creates a workspace or rewrites an ingestion/source value.
CREATE OR REPLACE FUNCTION control.customer_app_scope_lineage(
    p_batch uuid,p_landing uuid,p_source bigint,p_ref text)
RETURNS TABLE(app_id text,error_code text)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $body$
DECLARE landing_ref text; landing_source text; source_code text; is_connector boolean := false;
BEGIN
    IF to_regclass('connector.sync_run') IS NOT NULL THEN
        EXECUTE $sql$SELECT EXISTS(SELECT FROM connector.sync_run s
            WHERE s.ingestion_batch_id=$1 OR s.landing_batch_id=$2
               OR EXISTS(SELECT FROM control.stitching_job j
                  WHERE j.job_id=s.stitching_job_id AND j.ingestion_batch_id=$1))$sql$
            INTO is_connector USING p_batch,p_landing;
    END IF;
    IF is_connector THEN RETURN QUERY SELECT NULL::text,'CONNECTOR_EXCLUDED'::text; RETURN; END IF;
    SELECT l.batch_ref,l.source_code INTO landing_ref,landing_source
      FROM control.source_landing_batch l WHERE l.batch_id=p_landing;
    IF NOT FOUND THEN RETURN QUERY SELECT NULL::text,'MISSING_LANDING_LINEAGE'::text; RETURN; END IF;
    SELECT s.source_code INTO source_code FROM control.source_system s WHERE s.source_system_id=p_source;
    IF NOT FOUND OR p_ref IS DISTINCT FROM landing_ref OR source_code IS DISTINCT FROM landing_source THEN
        RETURN QUERY SELECT NULL::text,'BATCH_LINEAGE_CONFLICT'::text; RETURN;
    END IF;
    app_id := (regexp_match(p_ref,'^[0-9]{4}-[0-9]{2}-[0-9]{2}-[0-9]{6}-([0-9]+)-[0-9a-f]{12}$'))[1];
    error_code := CASE WHEN app_id IS NULL THEN 'INVALID_UPLOAD_BATCH_REF' END;
    RETURN NEXT;
END $body$;
REVOKE ALL ON FUNCTION control.customer_app_scope_lineage(uuid,uuid,bigint,text) FROM PUBLIC;

CREATE OR REPLACE FUNCTION control.set_customer_batch_app_id() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $body$
BEGIN
    IF NEW.metadata ? 'connector_sync_run_id' THEN
        NEW.app_id := NULL;
    ELSE
        SELECT l.app_id INTO NEW.app_id FROM control.customer_app_scope_lineage(
            NEW.batch_id,NEW.landing_batch_id,NEW.source_system_id,NEW.batch_ref) l;
    END IF;
    RETURN NEW;
END $body$;
REVOKE ALL ON FUNCTION control.set_customer_batch_app_id() FROM PUBLIC;

-- Batch provenance is calculated once by the ingestion trigger, not inferred
-- from current customers or source_code. Shared by explicit and COPY writers.
CREATE OR REPLACE FUNCTION raw.set_customer_observation_app_id() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $body$
BEGIN
    SELECT b.app_id INTO NEW.app_id FROM control.ingestion_batch b WHERE b.batch_id=NEW.batch_id;
    RETURN NEW;
END $body$;
REVOKE ALL ON FUNCTION raw.set_customer_observation_app_id() FROM PUBLIC;

DO $triggers$
DECLARE t regclass; name text; definition text; expected text;
BEGIN
    FOREACH t IN ARRAY ARRAY['control.ingestion_batch'::regclass,'raw.customer'::regclass,
        'raw.customer_current'::regclass,'staging.customer_profile_history'::regclass,
        'staging.customer_profile'::regclass] LOOP
        name := 'customer_app_id_derive';
        expected := CASE WHEN t='control.ingestion_batch'::regclass THEN
            'CREATE TRIGGER customer_app_id_derive BEFORE INSERT OR UPDATE OF batch_id, landing_batch_id, source_system_id, batch_ref, metadata, app_id ON control.ingestion_batch FOR EACH ROW EXECUTE FUNCTION control.set_customer_batch_app_id()'
          WHEN t IN ('raw.customer'::regclass,'staging.customer_profile_history'::regclass) THEN
            format('CREATE TRIGGER customer_app_id_derive BEFORE INSERT ON %s FOR EACH ROW EXECUTE FUNCTION raw.set_customer_observation_app_id()',t)
          ELSE format('CREATE TRIGGER customer_app_id_derive BEFORE INSERT OR UPDATE ON %s FOR EACH ROW EXECUTE FUNCTION raw.set_customer_observation_app_id()',t) END;
        SELECT pg_get_triggerdef(oid) INTO definition FROM pg_trigger WHERE tgrelid=t AND tgname=name;
        IF FOUND THEN
            IF definition IS DISTINCT FROM expected OR EXISTS(SELECT FROM pg_trigger
                WHERE tgrelid=t AND tgname=name AND tgenabled <> 'O') THEN
                RAISE EXCEPTION '026 incompatible existing app_id trigger on %',t;
            END IF;
        ELSE EXECUTE expected;
        END IF;
    END LOOP;
END $triggers$;
-- Owner-only read diagnostic; NULL is accompanied by an explicit reason.
CREATE OR REPLACE FUNCTION control.customer_app_scope_status(p_batch uuid)
RETURNS TABLE(app_id text,error_code text)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $body$
DECLARE b record;
BEGIN
    SELECT * INTO b FROM control.ingestion_batch WHERE batch_id=p_batch;
    IF NOT FOUND THEN RETURN QUERY SELECT NULL::text,'UNKNOWN_BATCH'::text; RETURN; END IF;
    IF b.metadata ? 'connector_sync_run_id' THEN RETURN QUERY SELECT NULL::text,'CONNECTOR_EXCLUDED'::text; RETURN; END IF;
    RETURN QUERY SELECT * FROM control.customer_app_scope_lineage(b.batch_id,b.landing_batch_id,b.source_system_id,b.batch_ref);
END $body$;
REVOKE ALL ON FUNCTION control.customer_app_scope_status(uuid) FROM PUBLIC;

-- Explicit operator call, one batch/table/keyset chunk per short transaction.
-- No SECURITY DEFINER escalation and no caller-provided SQL/column names.
-- ACCESS EXCLUSIVE keeps the brief, exactly-scoped trigger suspension invisible
-- to concurrent writers; COMMIT/ROLLBACK restores ordinary immutable history.
-- Only app_id is assigned, never keys, timestamps, payloads or business values.
CREATE OR REPLACE FUNCTION control.backfill_customer_app_id(
    p_table regclass,p_batch uuid,p_after uuid DEFAULT NULL,p_limit integer DEFAULT 10000)
RETURNS TABLE(scanned_count bigint,updated_count bigint,next_after uuid,app_id text,error_code text)
LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp SET lock_timeout='5s' AS $body$
DECLARE key_name text; keys uuid[]; tr record; disabled text[] := '{}'; trigger_name text;
        resolved_app text; reason text; bad boolean; b record;
BEGIN
    IF p_table IS NULL OR p_table NOT IN ('control.ingestion_batch'::regclass,'raw.customer'::regclass,
        'raw.customer_current'::regclass,'staging.customer_profile_history'::regclass,'staging.customer_profile'::regclass)
        OR p_batch IS NULL OR p_limit IS NULL OR p_limit < 1 OR p_limit > 50000 THEN
        RAISE EXCEPTION 'Invalid app_id backfill target, batch or bounded limit';
    END IF;
    IF NOT EXISTS(SELECT FROM pg_class c JOIN pg_roles r ON r.rolname=current_user
        WHERE c.oid=p_table AND (c.relowner=r.oid OR r.rolsuper)) THEN
        RAISE EXCEPTION 'app_id backfill requires table owner';
    END IF;
    EXECUTE format('LOCK TABLE %s IN ACCESS EXCLUSIVE MODE',p_table);
    IF p_table IN ('raw.customer'::regclass,'staging.customer_profile_history'::regclass)
        AND NOT EXISTS(SELECT FROM pg_trigger WHERE tgrelid=p_table
            AND tgname=CASE WHEN p_table='raw.customer'::regclass THEN 'customer_history_immutable'
                            ELSE 'customer_profile_history_immutable' END
            AND tgfoid='raw.reject_customer_history_mutation()'::regprocedure
            AND tgtype=58 AND tgenabled='O') THEN
        RAISE EXCEPTION 'Missing or changed immutable trigger on %',p_table;
    END IF;
    -- Freeze the exact source evidence for the entire chunk, including connector
    -- registration. No inferred ownership, repair, or ledger/current-row fallback.
    LOCK TABLE control.ingestion_batch,control.source_landing_batch,control.source_system,
        control.stitching_job IN SHARE MODE;
    IF to_regclass('connector.sync_run') IS NOT NULL THEN
        LOCK TABLE connector.sync_run IN SHARE MODE;
    END IF;
    SELECT s.app_id,s.error_code INTO resolved_app,reason FROM control.customer_app_scope_status(p_batch) s;
    scanned_count:=0; updated_count:=0; next_after:=p_after; app_id:=resolved_app; error_code:=reason;
    IF reason IS NOT NULL THEN RETURN NEXT; RETURN; END IF;
    key_name := CASE WHEN p_table='control.ingestion_batch'::regclass THEN 'batch_id' ELSE 'raw_customer_id' END;
    EXECUTE format('SELECT array_agg(k ORDER BY k) FROM (SELECT %1$I k FROM %2$s
        WHERE batch_id=$1 AND ($2 IS NULL OR %1$I>$2) ORDER BY %1$I LIMIT $3) s',key_name,p_table)
        INTO keys USING p_batch,p_after,p_limit;
    scanned_count:=coalesce(cardinality(keys),0);
    IF scanned_count=0 THEN RETURN NEXT; RETURN; END IF;
    next_after:=keys[cardinality(keys)];
    EXECUTE format('SELECT EXISTS(SELECT FROM %s WHERE batch_id=$1 AND %I=ANY($2)
        AND app_id IS NOT NULL AND app_id IS DISTINCT FROM $3)',p_table,key_name)
        INTO bad USING p_batch,keys,resolved_app;
    IF bad THEN RAISE EXCEPTION 'Existing app_id conflicts with exact durable batch lineage'; END IF;
    -- Refuse unknown custom UPDATE triggers instead of risking hidden timestamp,
    -- payload, audit or derived-data writes. INSERT-only triggers never run here.
    FOR tr IN SELECT tgname,tgfoid,tgenabled,tgtype FROM pg_trigger
        WHERE tgrelid=p_table AND NOT tgisinternal AND (tgtype & 16)=16 LOOP
        IF tr.tgenabled <> 'O' THEN RAISE EXCEPTION 'Unexpected disabled trigger %',tr.tgname; END IF;
        IF (p_table='raw.customer'::regclass AND tr.tgname='customer_history_immutable'
                AND tr.tgfoid='raw.reject_customer_history_mutation()'::regprocedure AND tr.tgtype=58)
            OR (p_table='staging.customer_profile_history'::regclass AND tr.tgname='customer_profile_history_immutable'
                AND tr.tgfoid='raw.reject_customer_history_mutation()'::regprocedure AND tr.tgtype=58)
            OR (p_table='raw.customer_current'::regclass AND tr.tgname='trg_raw_customer_updated_at'
                AND tr.tgfoid=to_regprocedure('raw.set_customer_updated_at()') AND tr.tgtype=19)
            OR (p_table='raw.customer_current'::regclass AND tr.tgname='customer_history_capture_update_statement'
                AND tr.tgfoid='raw.capture_customer_history_statement()'::regprocedure AND tr.tgtype=16) THEN
            EXECUTE format('ALTER TABLE %s DISABLE TRIGGER %I',p_table,tr.tgname);
            disabled:=array_append(disabled,tr.tgname);
        ELSIF NOT ((tr.tgname='customer_app_id_derive' AND tr.tgfoid IN (
                'raw.set_customer_observation_app_id()'::regprocedure,'control.set_customer_batch_app_id()'::regprocedure))
            OR (p_table='control.ingestion_batch'::regclass AND tr.tgname='customer_batch_order_immutable'
                AND tr.tgfoid='raw.reject_customer_batch_reordering()'::regprocedure)) THEN
            RAISE EXCEPTION 'Unreviewed UPDATE trigger % on %',tr.tgname,p_table;
        END IF;
    END LOOP;
    -- Current-table derivation reads b.app_id. Require the control chunk first.
    IF p_table NOT IN ('control.ingestion_batch'::regclass,'raw.customer'::regclass,'staging.customer_profile_history'::regclass)
        AND NOT EXISTS(SELECT FROM control.ingestion_batch ib WHERE ib.batch_id=p_batch AND ib.app_id=resolved_app) THEN
        RAISE EXCEPTION 'Backfill control.ingestion_batch before current profiles';
    END IF;
    EXECUTE format('UPDATE %s SET app_id=$3 WHERE batch_id=$1 AND %I=ANY($2) AND app_id IS NULL',p_table,key_name)
        USING p_batch,keys,resolved_app;
    GET DIAGNOSTICS updated_count=ROW_COUNT;
    FOREACH trigger_name IN ARRAY disabled LOOP
        EXECUTE format('ALTER TABLE %s ENABLE TRIGGER %I',p_table,trigger_name);
    END LOOP;
    RETURN NEXT;
END $body$;
REVOKE ALL ON FUNCTION control.backfill_customer_app_id(regclass,uuid,uuid,integer) FROM PUBLIC;
COMMENT ON FUNCTION control.backfill_customer_app_id(regclass,uuid,uuid,integer) IS
 'Owner-only, explicit keyset chunk; commit each call and persist next_after. Stop after scanned_count=0, not updated_count=0. Fill control first, then each historical/current table. Connector/unknown skipped with diagnostic. Only app_id changes; no business/timestamp mutation. No automatic invocation.';

COMMENT ON COLUMN raw.customer.app_id IS 'Exact literal numeric app component of this observation batch_ref, with matching durable landing/source lineage; NULL is unresolved or connector. Not current identity ownership.';
COMMENT ON COLUMN staging.customer_profile_history.app_id IS 'Upload app lineage of this normalized historical observation, not the latest global profile.';
COMMENT ON COLUMN raw.customer_current.app_id IS 'Current contributing batch app only. Global source/customer key is unchanged; NOT tenant isolation.';
COMMENT ON COLUMN staging.customer_profile.app_id IS 'Current contributing batch app only. Global standardized identity is unchanged; NOT tenant isolation.';
COMMENT ON COLUMN control.ingestion_batch.app_id IS 'Proven literal file upload app component; never a numeric cast, source_code, workspace or connector app.';
DO $verify_functions$
BEGIN
    IF EXISTS(SELECT FROM pg_temp.customer_app_scope_prior p
        WHERE p.definition IS DISTINCT FROM pg_get_functiondef(p.oid)) THEN
        RAISE EXCEPTION '026 incompatible existing function definition';
    END IF;
END $verify_functions$;
COMMIT;
