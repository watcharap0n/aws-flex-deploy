-- Forward-only, MANUAL opt-in for NEW file uploads. Not an approval to deploy 036.
-- Pause and drain landing/Lambda, connector worker + dispatcher, and admission/stitcher.
-- Snapshot catalog/data; size index builds. Do not run against an aliased historical
-- raw ID: its old membership, quality, staging and identity FKs need separate
-- evidence-backed recovery. Never rewrite or fabricate historical observations.
-- Set this explicit session opt-in only after inspecting the preflight below:
--   SET cdp.customer_source_namespace_opt_in = 'reviewed-file-upload-only';
-- One transaction: preflight, unique-index replacement, trigger, writer activation.
-- A missing gate/constraint/lineage aborts *all* DDL. No replication-role bypass.
BEGIN;
SET LOCAL lock_timeout='10s';
DO $gate$
BEGIN
    IF to_regprocedure('control.bind_connector_batch_app_id(uuid,uuid,text)') IS NOT NULL
       OR to_regprocedure('control.connector_batch_app_scope(uuid)') IS NOT NULL THEN
        RAISE EXCEPTION '037 incompatible with connector app lineage; coordinated cutover required';
    END IF;
    IF current_setting('cdp.customer_source_namespace_opt_in',true)
          IS DISTINCT FROM 'reviewed-file-upload-only' THEN
        RAISE EXCEPTION '037 requires explicit reviewed-file-upload-only opt-in';
    END IF;
    IF to_regprocedure('raw.apply_customer_bulk(uuid,bigint)') IS NULL
       OR to_regprocedure('control.customer_app_scope_status(uuid)') IS NULL
       OR to_regclass('identity.customer_entity_member') IS NULL THEN
        RAISE EXCEPTION '037 requires 019/022/026 and installed identity member model';
    END IF;
END $gate$;
LOCK TABLE raw.customer_current,raw.customer,raw.customer_batch_member,
    staging.customer_profile,staging.customer_profile_history,
    identity.customer_entity_member,control.ingestion_batch IN ACCESS EXCLUSIVE MODE;
LOCK TABLE control.source_landing_batch,control.source_system,control.stitching_job IN SHARE MODE;
DO $lock_connector$
BEGIN
    IF to_regclass('connector.sync_run') IS NOT NULL THEN
        LOCK TABLE connector.sync_run IN SHARE MODE;
    END IF;
END $lock_connector$;
DO $preflight$
BEGIN
    -- Every observation and current row must have complete, unchanged provenance.
    -- Historical source-key collisions that shared a raw ID cannot be split by
    -- index surgery: STOP even if the current row now belongs to only one app.
    IF EXISTS (
        SELECT FROM (
            SELECT raw_customer_id,coalesce(app_id,'<connector>') AS owner FROM raw.customer
            UNION ALL SELECT raw_customer_id,coalesce(app_id,'<connector>') FROM raw.customer_current
        ) owners GROUP BY raw_customer_id HAVING count(DISTINCT owner)>1
    ) THEN
        RAISE EXCEPTION '037 historical raw ID alias across apps/connectors: recover immutable history and downstream references before activation';
    END IF;
    IF EXISTS (
        SELECT FROM raw.customer h JOIN control.ingestion_batch b ON b.batch_id=h.batch_id
        CROSS JOIN LATERAL control.customer_app_scope_status(h.batch_id) scope
        WHERE (scope.error_code IS DISTINCT FROM 'CONNECTOR_EXCLUDED'
               AND (scope.error_code IS NOT NULL OR scope.app_id IS NULL))
           OR (scope.error_code='CONNECTOR_EXCLUDED' AND (h.app_id IS NOT NULL OR b.app_id IS NOT NULL))
           OR (scope.error_code IS NULL AND
               (h.app_id IS DISTINCT FROM scope.app_id OR b.app_id IS DISTINCT FROM scope.app_id))
    ) OR EXISTS (
        SELECT FROM raw.customer_current c JOIN control.ingestion_batch b ON b.batch_id=c.batch_id
        CROSS JOIN LATERAL control.customer_app_scope_status(c.batch_id) scope
        WHERE (scope.error_code IS DISTINCT FROM 'CONNECTOR_EXCLUDED'
               AND (scope.error_code IS NOT NULL OR scope.app_id IS NULL))
           OR (scope.error_code='CONNECTOR_EXCLUDED' AND (c.app_id IS NOT NULL OR b.app_id IS NOT NULL))
           OR (scope.error_code IS NULL AND
               (c.app_id IS DISTINCT FROM scope.app_id OR b.app_id IS DISTINCT FROM scope.app_id))
    ) THEN
        RAISE EXCEPTION '037 historical/current app provenance unresolved; finish exact per-batch 026 backfill/recovery first';
    END IF;
    IF EXISTS (
        SELECT FROM raw.customer_batch_member m
        LEFT JOIN raw.customer h ON h.batch_id=m.ingestion_batch_id AND h.raw_customer_id=m.raw_customer_id
        LEFT JOIN raw.customer_current c ON c.raw_customer_id=m.raw_customer_id
        WHERE h.raw_customer_id IS NULL OR c.raw_customer_id IS NULL
           OR (h.source_system_id,h.source_customer_id) IS DISTINCT FROM
              (m.source_system_id,m.source_customer_id)
           OR (c.source_system_id,c.source_customer_id) IS DISTINCT FROM
              (m.source_system_id,m.source_customer_id)
           OR h.app_id IS DISTINCT FROM c.app_id
    ) OR EXISTS (
        SELECT FROM raw.customer h LEFT JOIN raw.customer_batch_member m
          ON m.ingestion_batch_id=h.batch_id AND m.raw_customer_id=h.raw_customer_id
        WHERE m.raw_customer_id IS NULL
    ) THEN
        RAISE EXCEPTION '037 missing/contradictory immutable history or batch membership: recover before activation';
    END IF;
    IF EXISTS (
        SELECT FROM staging.customer_profile p
        LEFT JOIN raw.customer_current c ON c.raw_customer_id=p.raw_customer_id
        WHERE c.raw_customer_id IS NULL OR p.app_id IS DISTINCT FROM c.app_id
          OR (p.source_system_id,p.source_customer_id) IS DISTINCT FROM
             (c.source_system_id,c.source_customer_id)
    ) OR EXISTS (
        SELECT FROM staging.customer_profile_history p
        LEFT JOIN raw.customer h ON h.batch_id=p.batch_id AND h.raw_customer_id=p.raw_customer_id
        WHERE h.raw_customer_id IS NULL OR p.app_id IS DISTINCT FROM h.app_id
          OR (p.source_system_id,p.source_customer_id) IS DISTINCT FROM
             (h.source_system_id,h.source_customer_id)
    ) OR EXISTS (
        SELECT FROM identity.customer_entity_member m
        LEFT JOIN staging.customer_profile p USING(standardized_record_id)
        WHERE p.standardized_record_id IS NULL OR
           (m.source_system_id,m.source_customer_id) IS DISTINCT FROM
           (p.source_system_id,p.source_customer_id)
    ) THEN
        RAISE EXCEPTION '037 staging/identity lineage mismatch: recover before activation';
    END IF;
    -- Old connector's source-only SELECTs are valid ONLY when its keys never
    -- overlap file keys. This is maintained by the guard trigger below.
    IF EXISTS (
        SELECT FROM raw.customer_current file JOIN raw.customer_current connector
          ON (file.source_system_id,file.source_customer_id)=
             (connector.source_system_id,connector.source_customer_id)
        WHERE file.app_id IS NOT NULL AND connector.app_id IS NULL
    ) THEN
        RAISE EXCEPTION '037 connector/file source overlap: connector requires coordinated scoped reader rollout';
    END IF;
END $preflight$;

-- Preserve UUID PK and all FK target OIDs. Replace only the two known unique
-- source-key arbiters. Refuse a drifted/duplicated index instead of guessing.
ALTER TABLE identity.customer_entity_member ADD COLUMN app_id text;
DO $indexes$
DECLARE table_name regclass; old_index regclass; constraint_name text; index_name text; n integer;
BEGIN
    FOREACH table_name IN ARRAY ARRAY['raw.customer_current'::regclass,
                                       'identity.customer_entity_member'::regclass] LOOP
        SELECT count(*), min(i.indexrelid::oid)::regclass INTO n,old_index
        FROM pg_index i JOIN pg_attribute a ON a.attrelid=table_name AND a.attnum=i.indkey[0]
        JOIN pg_attribute b ON b.attrelid=table_name AND b.attnum=i.indkey[1]
        WHERE i.indrelid=table_name AND i.indisunique AND NOT i.indisprimary
          AND i.indnkeyatts=2 AND i.indexprs IS NULL AND i.indpred IS NULL
          AND a.attname='source_system_id' AND b.attname='source_customer_id';
        IF n<>1 THEN RAISE EXCEPTION '037 expected exactly one old source arbiter on %, found %',table_name,n; END IF;
        IF table_name='raw.customer_current'::regclass THEN
            EXECUTE 'CREATE UNIQUE INDEX customer_current_app_source_namespace
                ON raw.customer_current(app_id,source_system_id,source_customer_id)
                WHERE app_id IS NOT NULL';
            EXECUTE 'CREATE UNIQUE INDEX customer_current_connector_source_namespace
                ON raw.customer_current(source_system_id,source_customer_id)
                WHERE app_id IS NULL';
        ELSE
            EXECUTE 'CREATE UNIQUE INDEX customer_entity_member_app_source_namespace
                ON identity.customer_entity_member(app_id,source_system_id,source_customer_id)
                WHERE app_id IS NOT NULL';
            EXECUTE 'CREATE UNIQUE INDEX customer_entity_member_connector_source_namespace
                ON identity.customer_entity_member(source_system_id,source_customer_id)
                WHERE app_id IS NULL';
        END IF;
        SELECT conname INTO constraint_name FROM pg_constraint
          WHERE conrelid=table_name AND conindid=old_index AND contype='u';
        IF constraint_name IS NOT NULL THEN
            EXECUTE format('ALTER TABLE %s DROP CONSTRAINT %I',table_name,constraint_name);
        ELSE
            SELECT format('%I.%I',n.nspname,c.relname) INTO index_name
            FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE c.oid=old_index;
            EXECUTE format('DROP INDEX %s',index_name);
        END IF;
    END LOOP;
END $indexes$;
-- Both file and connector member rows derive their namespace from the associated
-- current profile; unchanged persistence SQL can still globally join by entity UUID.
CREATE FUNCTION identity.set_customer_member_app_namespace() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $body$
DECLARE p record;
BEGIN
    SELECT profile.app_id,profile.source_system_id,profile.source_customer_id,
           current_row.app_id AS current_app
    INTO p FROM staging.customer_profile profile
    JOIN raw.customer_current current_row USING(raw_customer_id)
    WHERE profile.standardized_record_id=NEW.standardized_record_id;
    IF NOT FOUND OR p.app_id IS DISTINCT FROM p.current_app OR
       (NEW.source_system_id,NEW.source_customer_id) IS DISTINCT FROM
       (p.source_system_id,p.source_customer_id) THEN
        RAISE EXCEPTION '037 identity member source namespace mismatch';
    END IF;
    NEW.app_id := p.app_id;
    RETURN NEW;
END $body$;
CREATE TRIGGER customer_member_app_namespace BEFORE INSERT OR UPDATE
ON identity.customer_entity_member FOR EACH ROW EXECUTE FUNCTION identity.set_customer_member_app_namespace();
UPDATE identity.customer_entity_member m SET app_id=p.app_id
FROM staging.customer_profile p WHERE p.standardized_record_id=m.standardized_record_id;

-- 026 derives app_id BEFORE this trigger (alphabetical name ordering). Reject
-- source reclassification, broken lineage and file/connector key overlaps.
CREATE FUNCTION raw.guard_customer_source_namespace() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $body$
DECLARE scope record; collision boolean;
BEGIN
    SELECT * INTO scope FROM control.customer_app_scope_status(NEW.batch_id);
    IF NOT FOUND OR (scope.error_code IS DISTINCT FROM 'CONNECTOR_EXCLUDED' AND
       (scope.error_code IS NOT NULL OR scope.app_id IS NULL)) OR
       NEW.app_id IS DISTINCT FROM scope.app_id OR
       (TG_OP='UPDATE' AND (OLD.app_id,OLD.source_system_id,OLD.source_customer_id)
                            IS DISTINCT FROM (NEW.app_id,NEW.source_system_id,NEW.source_customer_id)) THEN
        RAISE EXCEPTION '037 current source namespace or batch lineage mismatch';
    END IF;
    -- Refuse uncoordinated legacy/direct file writers after the old source-only
    -- unique key is removed; the reviewed ingestion path sets this transaction-local.
    IF NEW.app_id IS NOT NULL AND
       current_setting('cdp.customer_source_namespace_writer',true) IS DISTINCT FROM 'file-v1' THEN
        RAISE EXCEPTION '037 file writer not activated';
    END IF;
    -- The bulk function serializes by source, but direct connector INSERT/UPDATE
    -- did not. Take the same xact lock before the overlap probe so READ COMMITTED
    -- sees the other writer's committed row after waiting. Lock is reentrant for
    -- bulk calls already holding it; hash collisions only add contention.
    PERFORM pg_advisory_xact_lock(
        hashtextextended('customer-bulk:' || NEW.source_system_id::text,0));
    SELECT EXISTS(SELECT FROM raw.customer_current c
      WHERE c.raw_customer_id IS DISTINCT FROM NEW.raw_customer_id
        AND c.source_system_id=NEW.source_system_id
        AND c.source_customer_id=NEW.source_customer_id
        AND ((c.app_id IS NULL AND NEW.app_id IS NOT NULL)
          OR (c.app_id IS NOT NULL AND NEW.app_id IS NULL))) INTO collision;
    IF collision THEN
        RAISE EXCEPTION '037 connector/file source overlap: scoped connector reader required';
    END IF;
    RETURN NEW;
END $body$;
CREATE TRIGGER customer_source_namespace_guard BEFORE INSERT OR UPDATE
ON raw.customer_current FOR EACH ROW EXECUTE FUNCTION raw.guard_customer_source_namespace();

CREATE FUNCTION staging.guard_customer_profile_namespace() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $body$
DECLARE c record;
BEGIN
    SELECT app_id,source_system_id,source_customer_id INTO c
    FROM raw.customer_current WHERE raw_customer_id=NEW.raw_customer_id;
    IF NOT FOUND OR NEW.app_id IS DISTINCT FROM c.app_id OR
       (NEW.source_system_id,NEW.source_customer_id) IS DISTINCT FROM
       (c.source_system_id,c.source_customer_id) THEN
        RAISE EXCEPTION '037 standardized current source namespace mismatch';
    END IF;
    RETURN NEW;
END $body$;
CREATE TRIGGER customer_source_namespace_profile_guard BEFORE INSERT OR UPDATE
ON staging.customer_profile FOR EACH ROW EXECUTE FUNCTION staging.guard_customer_profile_namespace();
CREATE FUNCTION control.guard_customer_batch_namespace() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $body$
BEGIN
    IF (NEW.app_id,NEW.landing_batch_id,NEW.source_system_id,NEW.batch_ref,
        NEW.metadata ? 'connector_sync_run_id') IS DISTINCT FROM
       (OLD.app_id,OLD.landing_batch_id,OLD.source_system_id,OLD.batch_ref,
        OLD.metadata ? 'connector_sync_run_id') THEN
        RAISE EXCEPTION '037 ingestion source/app lineage cannot change';
    END IF;
    RETURN NEW;
END $body$;
CREATE TRIGGER customer_source_namespace_batch_guard BEFORE UPDATE
ON control.ingestion_batch FOR EACH ROW EXECUTE FUNCTION control.guard_customer_batch_namespace();

-- Retain 022's generated typed ROW comparison and all current/history/membership
-- SQL. Amend only the source lookup and conflict arbiter, then activate a marker
-- required by the new Lambda. Fail closed on a different installed function shape.
DO $rewrite$
DECLARE d text; p record; old text; v_count integer;
BEGIN
    SELECT oid,pg_get_functiondef(oid) AS definition,prosecdef,provolatile,pronargs
    INTO p FROM pg_proc WHERE oid='raw.apply_customer_bulk(uuid,bigint)'::regprocedure;
    IF p.prosecdef OR p.provolatile<>'v' OR p.pronargs<>2 THEN
        RAISE EXCEPTION '037 bulk writer contract drift';
    END IF;
    d:=p.definition;
    old:='DECLARE selected_count bigint; t timestamptz := clock_timestamp(); timings jsonb := ''{}'';';
    IF strpos(d,old)=0 THEN RAISE EXCEPTION '037 unexpected bulk declaration'; END IF;
    d:=replace(d,old,old||E'\n    v_app text; v_error text;');
    old:='IF NOT EXISTS (SELECT FROM control.ingestion_batch';
    IF strpos(d,old)=0 THEN RAISE EXCEPTION '037 unexpected bulk lineage gate'; END IF;
    old:='    IF EXISTS (SELECT FROM pg_temp.customer_bulk_stage';
    IF strpos(d,old)=0 THEN RAISE EXCEPTION '037 unexpected bulk stage gate'; END IF;
    d:=replace(d,old,$replacement$
    SELECT b.app_id INTO v_app FROM control.ingestion_batch b WHERE b.batch_id=p_batch;
    IF NOT FOUND OR
       NOT EXISTS (SELECT FROM control.customer_source_namespace_activation) THEN
        RAISE EXCEPTION '037 namespace activation/lineage gate is not satisfied';
    END IF;
    IF v_app IS NOT NULL AND
       current_setting('cdp.customer_source_namespace_writer',true) IS DISTINCT FROM 'file-v1' THEN
        RAISE EXCEPTION '037 file writer not activated (old writer refused)';
    END IF;
    IF EXISTS (SELECT FROM pg_temp.customer_bulk_stage
               WHERE app_id IS NOT NULL) THEN
        RAISE EXCEPTION '037 stage app_id must derive from immutable batch lineage';
    END IF;
    IF EXISTS (SELECT FROM pg_temp.customer_bulk_stage$replacement$);
    -- Both new-row anti-join and all subsequent joins/update/membership must be scoped.
    old:='c.source_system_id=p_source AND c.source_customer_id=';
    v_count:=(length(d)-length(replace(d,old,'')))/length(old);
    IF v_count<>6 THEN RAISE EXCEPTION '037 expected six source-key lookups, found %',v_count; END IF;
    d:=replace(d,old,'c.app_id IS NOT DISTINCT FROM v_app AND '||old);
    old:='ON CONFLICT (source_system_id,source_customer_id) DO NOTHING';
    IF strpos(d,old)=0 THEN RAISE EXCEPTION '037 unexpected old source conflict target'; END IF;
    d:=replace(d,old,'ON CONFLICT DO NOTHING');
    EXECUTE d;
END $rewrite$;
-- Statement trigger must also reject an app-key change by direct UPDATE.
DO $statement_guard$
DECLARE d text; old text;
BEGIN
    SELECT pg_get_functiondef('raw.capture_customer_history_statement()'::regprocedure) INTO d;
    old:='OR (n.source_system_id,n.source_customer_id) IS DISTINCT FROM';
    IF strpos(d,old)=0 THEN RAISE EXCEPTION '037 unexpected direct-writer trigger'; END IF;
    d:=replace(d,old,'OR n.app_id IS DISTINCT FROM o.app_id'||E'\n               '||old);
    EXECUTE d;
END $statement_guard$;
CREATE TABLE control.customer_source_namespace_activation (
    activation_id boolean PRIMARY KEY DEFAULT true CHECK (activation_id),
    contract text NOT NULL CHECK(contract='file-v1/connector-overlap-refused'));
INSERT INTO control.customer_source_namespace_activation(activation_id,contract)
VALUES (true,'file-v1/connector-overlap-refused');
COMMENT ON TABLE control.customer_source_namespace_activation IS
 '037 applied after exact historical alias/lineage preflight; file-v1 Lambda marker required. Not proof of production recovery or connector rollout.';
COMMIT;
