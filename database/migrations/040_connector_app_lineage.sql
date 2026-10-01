-- Additive connector app provenance. Apply only with 005_workspace_app_id and
-- the 019/022/026 runtime, before allowing app-bound syncs to dispatch.
-- This release does not install 037/038, backfill connector observations, or
-- enable upload-only identity publication. Existing terminal connector sources
-- are detached from connections, not deleted; their batch/raw IDs stay intact.
BEGIN;
SET LOCAL lock_timeout = '5s';
DO $preflight$
DECLARE api_role text := nullif(current_setting('cdp.connector_api_role',true),'');
        worker_role text := nullif(current_setting('cdp.connector_worker_role',true),'');
        owner_only boolean := current_setting('cdp.connector_owner_runtime',true)
                              = 'verified-owner-only';
BEGIN
    IF owner_only THEN
        IF api_role IS NOT NULL OR worker_role IS NOT NULL
           OR current_user <> 'postgres'
           OR NOT EXISTS (SELECT FROM pg_roles WHERE rolname=current_user AND rolcreatedb)
           OR NOT EXISTS (SELECT FROM pg_class
                   WHERE oid='platform.workspace'::regclass AND relowner=(SELECT oid FROM pg_roles WHERE rolname=current_user)) THEN
            RAISE EXCEPTION '040 owner-only runtime requires verified workspace owner with no separate roles';
        END IF;
    ELSIF api_role IS NULL OR worker_role IS NULL OR api_role = worker_role
       OR api_role IN ('public','postgres') OR worker_role IN ('public','postgres')
       OR NOT EXISTS (SELECT FROM pg_roles WHERE rolname=api_role)
       OR NOT EXISTS (SELECT FROM pg_roles WHERE rolname=worker_role) THEN
        RAISE EXCEPTION '040 requires two distinct existing API/worker roles via cdp.connector_api_role and cdp.connector_worker_role';
    END IF;
    IF to_regprocedure('control.customer_app_scope_status(uuid)') IS NULL
       OR to_regclass('connector.sync_run') IS NULL
       OR to_regclass('platform.workspace') IS NULL
       OR NOT EXISTS (SELECT FROM pg_attribute WHERE attrelid='platform.workspace'::regclass
                      AND attname='app_id' AND atttypid='text'::regtype AND NOT attisdropped)
       OR to_regclass('control.customer_forward_route') IS NOT NULL
       OR to_regclass('raw.customer_current_connector_source_namespace') IS NOT NULL THEN
        RAISE EXCEPTION '040 requires workspace app_id and 026; 037/038 need a separate coordinated migration';
    END IF;
END $preflight$;

-- Old connection registrations are source-only global namespaces. Leave every
-- historical landing/batch/raw row and source intact, but detach the connection
-- so its next sync must register a fresh UUID-derived physical source. Never
-- adopt or rename an old source (its existing customer identities keep that ID).
-- Coordinate a worker drain before this lock; old queued runs need explicit
-- terminalization before a new run for that dataset can be created.
LOCK TABLE connector.connection, connector.dataset, connector.sync_run,
    control.ingestion_batch, control.source_system IN SHARE ROW EXCLUSIVE MODE;
DO $legacy_active$
BEGIN
    IF EXISTS (
        SELECT FROM connector.sync_run r
        JOIN connector.dataset d ON d.dataset_id=r.dataset_id
        JOIN connector.connection c ON c.connection_id=d.connection_id
        JOIN control.source_system ss ON ss.source_system_id=c.source_system_id
        WHERE r.status IN ('QUEUED','EXTRACTING','PUBLISHING','INGESTING','STITCHING')
          AND (ss.source_code IS DISTINCT FROM
                  ('dc1-' || replace(c.connection_id::text,'-',''))
               OR ss.source_type IS DISTINCT FROM 'database_connector')
    ) THEN
        RAISE EXCEPTION '040 old active sync must be fenced and terminalized before source quarantine';
    END IF;
    IF EXISTS (
        SELECT FROM connector.connection c
        JOIN control.source_system ss ON ss.source_system_id=c.source_system_id
        WHERE (ss.source_code IS DISTINCT FROM
                  ('dc1-' || replace(c.connection_id::text,'-',''))
               OR ss.source_type IS DISTINCT FROM 'database_connector')
          AND EXISTS (
              SELECT FROM connector.operation o WHERE o.connection_id=c.connection_id
                AND o.status IN ('QUEUED','DISPATCHED','RUNNING')
          )
    ) THEN
        RAISE EXCEPTION '040 old active connector operation must be fenced before source quarantine';
    END IF;
END $legacy_active$;
-- Persist the old source IDs before detaching. Historical raw IDs retain those
-- sources, but neither their code/type nor their ownership can ever be recycled.
CREATE TABLE control.connector_legacy_source_quarantine (
    source_system_id bigint PRIMARY KEY REFERENCES control.source_system(source_system_id),
    connection_id uuid NOT NULL REFERENCES connector.connection(connection_id),
    source_code text NOT NULL,
    quarantined_at timestamptz NOT NULL DEFAULT now()
);
REVOKE ALL ON control.connector_legacy_source_quarantine FROM PUBLIC;
INSERT INTO control.connector_legacy_source_quarantine(source_system_id,connection_id,source_code)
SELECT ss.source_system_id,c.connection_id,ss.source_code
FROM connector.connection c JOIN control.source_system ss USING(source_system_id)
WHERE ss.source_code IS DISTINCT FROM ('dc1-' || replace(c.connection_id::text,'-',''))
   OR ss.source_type IS DISTINCT FROM 'database_connector';
UPDATE connector.connection c SET source_system_id=NULL
FROM control.source_system ss
WHERE c.source_system_id=ss.source_system_id
  AND (ss.source_code IS DISTINCT FROM ('dc1-' || replace(c.connection_id::text,'-',''))
       OR ss.source_type IS DISTINCT FROM 'database_connector');

-- An app binding is never inferred from a db-* ref or a source code. Verify the
-- full durable run, source, landing and workspace linkage, including job parent.
CREATE FUNCTION control.connector_batch_app_scope(p_batch uuid)
RETURNS TABLE(app_id text,error_code text)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $body$
DECLARE b record; r record;
BEGIN
    SELECT batch_id,landing_batch_id,source_system_id,batch_ref,metadata
      INTO b FROM control.ingestion_batch WHERE batch_id=p_batch;
    IF NOT FOUND OR NOT (coalesce(b.metadata,'{}'::jsonb) ? 'connector_sync_run_id') THEN
        RETURN QUERY SELECT NULL::text,'CONNECTOR_EXCLUDED'::text; RETURN;
    END IF;
    SELECT s.sync_run_id,s.workspace_id,s.landing_batch_id,s.stitching_job_id,
           w.app_id AS workspace_app,c.connection_id,c.source_system_id AS connection_source,
           ss.source_code AS physical_code,ss.source_type AS physical_type,
           l.source_code AS landing_code,l.batch_ref AS landing_ref,
           j.ingestion_batch_id AS job_batch
      INTO r FROM connector.sync_run s
      JOIN platform.workspace w ON w.workspace_id=s.workspace_id
      JOIN connector.dataset d ON d.dataset_id=s.dataset_id AND d.workspace_id=s.workspace_id
      JOIN connector.connection c ON c.connection_id=d.connection_id AND c.workspace_id=s.workspace_id
      JOIN control.stitching_job j ON j.job_id=s.stitching_job_id
      JOIN control.source_landing_batch l ON l.batch_id=s.landing_batch_id
      JOIN control.source_system ss ON ss.source_system_id=b.source_system_id
     WHERE s.ingestion_batch_id=p_batch;
    IF NOT FOUND THEN
        RETURN QUERY SELECT NULL::text,'CONNECTOR_EXCLUDED'::text; RETURN;
    END IF;
    IF b.metadata->>'connector_sync_run_id' IS DISTINCT FROM r.sync_run_id::text
       OR b.landing_batch_id IS DISTINCT FROM r.landing_batch_id
       OR b.batch_ref IS DISTINCT FROM ('db-' || replace(r.sync_run_id::text,'-',''))
       OR b.batch_ref IS DISTINCT FROM r.landing_ref
       OR b.source_system_id IS DISTINCT FROM r.connection_source
       OR r.job_batch IS DISTINCT FROM p_batch
       OR r.landing_code IS DISTINCT FROM r.physical_code
       OR r.physical_code IS DISTINCT FROM ('dc1-' || replace(r.connection_id::text,'-',''))
       OR r.physical_type IS DISTINCT FROM 'database_connector' THEN
        RETURN QUERY SELECT NULL::text,'CONNECTOR_LINEAGE_CONFLICT'::text; RETURN;
    END IF;
    IF r.workspace_app IS NULL THEN
        RETURN QUERY SELECT NULL::text,'CONNECTOR_EXCLUDED'::text; RETURN;
    END IF;
    IF r.workspace_app !~ '^[0-9]+$' THEN
        RETURN QUERY SELECT NULL::text,'CONNECTOR_INVALID_APP_ID'::text; RETURN;
    END IF;
    RETURN QUERY SELECT r.workspace_app::text,NULL::text;
END $body$;
REVOKE ALL ON FUNCTION control.connector_batch_app_scope(uuid) FROM PUBLIC;

-- Existing 026 derives upload provenance at INSERT. Do not alter its file
-- branch, row triggers or typed comparisons; connector binding happens after
-- the sync_run insert and before outbox dispatch in the same transaction.
CREATE OR REPLACE FUNCTION control.set_customer_batch_app_id() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $body$
DECLARE expected text; reason text;
BEGIN
    IF NEW.metadata ? 'connector_sync_run_id' THEN
        IF TG_OP='UPDATE' AND OLD.metadata->>'connector_sync_run_id'
                            IS DISTINCT FROM NEW.metadata->>'connector_sync_run_id' THEN
            RAISE EXCEPTION 'connector sync lineage cannot change';
        END IF;
        IF TG_OP='UPDATE' AND OLD.app_id IS NOT NULL AND
           (NEW.batch_ref,NEW.landing_batch_id,NEW.source_system_id,
            NEW.metadata->>'connector_sync_run_id') IS DISTINCT FROM
           (OLD.batch_ref,OLD.landing_batch_id,OLD.source_system_id,
            OLD.metadata->>'connector_sync_run_id') THEN
            RAISE EXCEPTION 'connector app lineage cannot change';
        END IF;
        IF TG_OP='INSERT' THEN
            NEW.app_id := NULL; -- sync_run does not exist yet
        ELSE
            SELECT scope.app_id,scope.error_code INTO expected,reason
            FROM control.connector_batch_app_scope(NEW.batch_id) scope;
            IF reason IS DISTINCT FROM NULL AND reason <> 'CONNECTOR_EXCLUDED' THEN
                RAISE EXCEPTION 'connector app_id lineage mismatch';
            END IF;
            IF OLD.app_id IS NOT NULL AND OLD.app_id IS DISTINCT FROM expected THEN
                RAISE EXCEPTION 'connector app_id cannot change';
            END IF;
            IF NEW.app_id IS DISTINCT FROM expected THEN
                RAISE EXCEPTION 'connector app_id must match registered workspace';
            END IF;
            NEW.app_id := expected;
        END IF;
    ELSIF TG_OP='UPDATE' AND OLD.metadata ? 'connector_sync_run_id' THEN
        RAISE EXCEPTION 'connector lineage cannot become upload';
    ELSE
        IF EXISTS(SELECT FROM control.source_system s
                  WHERE s.source_system_id=NEW.source_system_id
                    AND (s.source_type='database_connector'
                         OR s.source_code LIKE 'dc1-%')) THEN
            RAISE EXCEPTION 'connector source cannot be used as upload lineage';
        END IF;
        SELECT scope.app_id INTO NEW.app_id FROM control.customer_app_scope_lineage(
            NEW.batch_id,NEW.landing_batch_id,NEW.source_system_id,NEW.batch_ref) scope;
    END IF;
    RETURN NEW;
END $body$;

-- Leave 026's upload-only customer_app_scope_status unchanged. Backfill and
-- upload admission call it and must continue seeing CONNECTOR_EXCLUDED.

-- No client GUC or unverified direct UPDATE: this function checks the workspace
-- binding and locks it before changing the batch. Any mismatch aborts creation.
CREATE FUNCTION control.bind_connector_batch_app_id(p_batch uuid,p_workspace uuid,p_app text)
RETURNS text LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $body$
DECLARE expected text; reason text; stored text;
BEGIN
    PERFORM 1 FROM platform.workspace WHERE workspace_id=p_workspace AND app_id=p_app
      AND app_id ~ '^[0-9]+$' FOR SHARE;
    IF NOT FOUND THEN RAISE EXCEPTION 'workspace app_id mismatch or unbound'; END IF;
    IF NOT EXISTS(SELECT FROM connector.sync_run s WHERE s.ingestion_batch_id=p_batch
                  AND s.workspace_id=p_workspace) THEN
        RAISE EXCEPTION 'connector app_id requires registered sync run';
    END IF;
    SELECT scope.app_id,scope.error_code INTO expected,reason
       FROM control.connector_batch_app_scope(p_batch) scope;
    IF expected IS DISTINCT FROM p_app OR reason IS NOT NULL THEN
        RAISE EXCEPTION 'connector app_id lineage mismatch';
    END IF;
    UPDATE control.ingestion_batch SET app_id=expected WHERE batch_id=p_batch
      AND (app_id IS NULL OR app_id=expected) RETURNING app_id INTO stored;
    IF NOT FOUND OR stored IS DISTINCT FROM expected THEN
        RAISE EXCEPTION 'connector app_id batch conflict';
    END IF;
    RETURN stored;
END $body$;
REVOKE ALL ON FUNCTION control.bind_connector_batch_app_id(uuid,uuid,text) FROM PUBLIC;

CREATE FUNCTION platform.prevent_workspace_app_rebind() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $body$
BEGIN
    IF OLD.app_id IS NOT NULL AND NEW.app_id IS DISTINCT FROM OLD.app_id THEN
        RAISE EXCEPTION 'workspace app_id cannot be rebound';
    END IF;
    RETURN NEW;
END $body$;
CREATE TRIGGER workspace_app_rebind_guard BEFORE UPDATE OF app_id ON platform.workspace
FOR EACH ROW EXECUTE FUNCTION platform.prevent_workspace_app_rebind();

-- A source ID is the globally keyed raw identity namespace. A unique index
-- excludes cross-workspace aliases atomically, including concurrent writers.
CREATE UNIQUE INDEX uq_connection_source_system
ON connector.connection(source_system_id) WHERE source_system_id IS NOT NULL;

-- Once registered, parent linkage is durable provenance. Workers may update
-- status, counters and checkpoints, but may never reassign an owner's keys.
CREATE FUNCTION connector.guard_app_lineage_reparent() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $body$
BEGIN
    IF TG_TABLE_NAME='connection' THEN
        IF NEW.workspace_id IS DISTINCT FROM OLD.workspace_id AND
           (OLD.source_system_id IS NOT NULL OR EXISTS(
               SELECT FROM connector.dataset d WHERE d.connection_id=OLD.connection_id
           )) THEN RAISE EXCEPTION 'connector ownership is immutable'; END IF;
        -- Allow first registration after old terminal runs were quarantined;
        -- the new source must be this connection's never-shared physical code.
        IF NEW.source_system_id IS DISTINCT FROM OLD.source_system_id AND
           (OLD.source_system_id IS NOT NULL OR NEW.source_system_id IS NULL OR
            EXISTS (SELECT FROM control.connector_legacy_source_quarantine q
                     WHERE q.source_system_id=NEW.source_system_id) OR
            NOT EXISTS (SELECT FROM control.source_system ss
                WHERE ss.source_system_id=NEW.source_system_id
                  AND ss.source_code=('dc1-' || replace(OLD.connection_id::text,'-',''))
                  AND ss.source_type='database_connector') OR EXISTS(
               SELECT FROM connector.dataset d JOIN connector.sync_run s
                 ON s.dataset_id=d.dataset_id WHERE d.connection_id=OLD.connection_id
                   AND s.status IN ('QUEUED','EXTRACTING','PUBLISHING','INGESTING','STITCHING')
           )) THEN RAISE EXCEPTION 'connector ownership is immutable'; END IF;
    ELSIF TG_TABLE_NAME='dataset' THEN
        IF (NEW.workspace_id,NEW.connection_id) IS DISTINCT FROM
           (OLD.workspace_id,OLD.connection_id) AND EXISTS(
                SELECT FROM connector.sync_run s WHERE s.dataset_id=OLD.dataset_id
           ) THEN RAISE EXCEPTION 'connector ownership is immutable'; END IF;
    ELSIF TG_TABLE_NAME='sync_run' THEN
        IF NEW.status IN ('QUEUED','EXTRACTING','PUBLISHING','INGESTING','STITCHING')
           AND OLD.status NOT IN ('QUEUED','EXTRACTING','PUBLISHING','INGESTING','STITCHING')
           AND EXISTS (
               SELECT FROM connector.dataset d JOIN connector.connection c
                    ON c.connection_id=d.connection_id
               JOIN control.ingestion_batch b ON b.batch_id=OLD.ingestion_batch_id
               WHERE d.dataset_id=NEW.dataset_id
                 AND (c.source_system_id IS DISTINCT FROM b.source_system_id
                      OR c.source_system_id IS NULL)
           ) THEN RAISE EXCEPTION 'connector legacy run cannot reactivate after source quarantine'; END IF;
        IF (NEW.workspace_id,NEW.dataset_id,NEW.ingestion_batch_id,
            NEW.landing_batch_id,NEW.stitching_job_id) IS DISTINCT FROM
           (OLD.workspace_id,OLD.dataset_id,OLD.ingestion_batch_id,
            OLD.landing_batch_id,OLD.stitching_job_id) THEN
            RAISE EXCEPTION 'connector ownership is immutable';
        END IF;
    END IF;
    RETURN NEW;
END $body$;
CREATE TRIGGER connection_app_lineage_guard BEFORE UPDATE ON connector.connection
FOR EACH ROW EXECUTE FUNCTION connector.guard_app_lineage_reparent();
CREATE TRIGGER dataset_app_lineage_guard BEFORE UPDATE ON connector.dataset
FOR EACH ROW EXECUTE FUNCTION connector.guard_app_lineage_reparent();
CREATE TRIGGER sync_run_app_lineage_guard BEFORE UPDATE ON connector.sync_run
FOR EACH ROW EXECUTE FUNCTION connector.guard_app_lineage_reparent();

-- A registered source's code/type is its raw identity boundary. Refuse later
-- reclassification even if the batch itself has not been dispatched yet.
CREATE FUNCTION control.guard_connector_source_identity() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $body$
BEGIN
    IF (NEW.source_code,NEW.source_type) IS DISTINCT FROM
       (OLD.source_code,OLD.source_type) THEN
        IF EXISTS (SELECT FROM control.connector_legacy_source_quarantine q
                   WHERE q.source_system_id=OLD.source_system_id) THEN
            RAISE EXCEPTION 'quarantined connector source identity is immutable';
        END IF;
        IF EXISTS (SELECT FROM connector.connection c
                   WHERE c.source_system_id=OLD.source_system_id) THEN
            RAISE EXCEPTION 'connector source identity is immutable';
        END IF;
    END IF;
    RETURN NEW;
END $body$;
CREATE TRIGGER connector_source_identity_guard
BEFORE UPDATE OF source_code,source_type ON control.source_system
FOR EACH ROW EXECUTE FUNCTION control.guard_connector_source_identity();

-- Existing raw keys are globally keyed by source_system_id, so require the
-- durable connector batch binding on both direct and bulk customer writers.
CREATE FUNCTION raw.guard_connector_customer_write() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $body$
DECLARE b record; resolved text; reason text; row_batch uuid;
BEGIN
    IF TG_TABLE_NAME='customer_batch_member' THEN
        row_batch := NEW.ingestion_batch_id;
    ELSE
        row_batch := NEW.batch_id;
    END IF;
    SELECT app_id,source_system_id,metadata INTO b FROM control.ingestion_batch
     WHERE batch_id=row_batch;
    IF NOT FOUND OR NEW.source_system_id IS DISTINCT FROM b.source_system_id THEN
        RAISE EXCEPTION 'customer source conflicts with batch';
    END IF;
    IF FOUND AND coalesce(b.metadata,'{}'::jsonb) ? 'connector_sync_run_id' THEN
        SELECT scope.app_id,scope.error_code INTO resolved,reason
          FROM control.connector_batch_app_scope(row_batch) scope;
        IF b.app_id IS NULL OR resolved IS DISTINCT FROM b.app_id OR reason IS NOT NULL THEN
            RAISE EXCEPTION 'connector customer app_id unbound';
        END IF;
    ELSIF EXISTS (SELECT FROM control.source_system ss
                  WHERE ss.source_system_id=NEW.source_system_id
                    AND (ss.source_code LIKE 'dc1-%'
                         OR ss.source_type='database_connector'
                         OR EXISTS(SELECT FROM control.connector_legacy_source_quarantine q
                                   WHERE q.source_system_id=ss.source_system_id))) THEN
        RAISE EXCEPTION 'connector source cannot be used in upload customer rows';
    END IF;
    IF TG_TABLE_NAME='customer_batch_member' AND NOT EXISTS (
        SELECT FROM raw.customer_current r
        WHERE r.raw_customer_id=NEW.raw_customer_id
          AND r.source_system_id=NEW.source_system_id
          AND r.source_customer_id=NEW.source_customer_id
    ) THEN
        RAISE EXCEPTION 'customer batch membership identity conflicts with current source';
    END IF;
    RETURN NEW;
END $body$;
REVOKE ALL ON FUNCTION raw.guard_connector_customer_write() FROM PUBLIC;
CREATE TRIGGER customer_connector_app_guard BEFORE INSERT OR UPDATE ON raw.customer_current
FOR EACH ROW EXECUTE FUNCTION raw.guard_connector_customer_write();
CREATE TRIGGER customer_connector_app_guard BEFORE INSERT ON raw.customer
FOR EACH ROW EXECUTE FUNCTION raw.guard_connector_customer_write();
CREATE TRIGGER customer_connector_member_guard BEFORE INSERT ON raw.customer_batch_member
FOR EACH ROW EXECUTE FUNCTION raw.guard_connector_customer_write();

-- Staging is the app-visible normalized observation. The current profile has
-- no composite history FK, so check the raw identity and exact source key for
-- both INSERT and UPDATE. History's (batch,raw) FK alone does not check its
-- source/customer columns. Keep the global entity graph outside this guard.
CREATE FUNCTION staging.guard_connector_profile_write() RETURNS trigger
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $body$
DECLARE b record; r record; resolved text; reason text;
BEGIN
    SELECT app_id,source_system_id,metadata INTO b FROM control.ingestion_batch
      WHERE batch_id=NEW.batch_id;
    SELECT source_system_id,source_customer_id,app_id INTO r FROM raw.customer_current
      WHERE raw_customer_id=NEW.raw_customer_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'staging customer identity is missing from raw current';
    END IF;
    IF NEW.source_system_id IS DISTINCT FROM r.source_system_id OR
       NEW.source_customer_id IS DISTINCT FROM r.source_customer_id THEN
        RAISE EXCEPTION 'staging customer source identity conflicts with raw current';
    END IF;
    IF b.source_system_id IS DISTINCT FROM NEW.source_system_id THEN
        RAISE EXCEPTION 'staging customer source conflicts with batch';
    END IF;
    IF coalesce(b.metadata,'{}'::jsonb) ? 'connector_sync_run_id' THEN
        SELECT scope.app_id,scope.error_code INTO resolved,reason
          FROM control.connector_batch_app_scope(NEW.batch_id) scope;
        IF b.app_id IS NULL OR b.app_id IS DISTINCT FROM resolved OR
           reason IS NOT NULL OR r.app_id IS DISTINCT FROM b.app_id THEN
            RAISE EXCEPTION 'staging connector app identity conflicts with raw current';
        END IF;
    ELSIF EXISTS (
        SELECT FROM control.source_system ss WHERE ss.source_system_id=NEW.source_system_id
          AND (ss.source_type='database_connector' OR ss.source_code LIKE 'dc1-%'
               OR EXISTS (SELECT FROM control.connector_legacy_source_quarantine q
                          WHERE q.source_system_id=ss.source_system_id))
    ) THEN
        RAISE EXCEPTION 'staging connector source cannot be used as upload';
    END IF;
    RETURN NEW;
END $body$;
REVOKE ALL ON FUNCTION staging.guard_connector_profile_write() FROM PUBLIC;
CREATE TRIGGER customer_connector_app_guard BEFORE INSERT OR UPDATE ON staging.customer_profile
FOR EACH ROW EXECUTE FUNCTION staging.guard_connector_profile_write();
CREATE TRIGGER customer_connector_app_guard BEFORE INSERT ON staging.customer_profile_history
FOR EACH ROW EXECUTE FUNCTION staging.guard_connector_profile_write();

-- The installed 026 backfill refuses unknown UPDATE triggers. Extend only its
-- exact installed body for this named current-row guard, preserving the existing
-- immutable-history, trigger suspension, and upload-only checks verbatim.
DO $backfill$
DECLARE before_sql text; after_sql text;
        needle constant text := $needle$ELSIF NOT ((tr.tgname='customer_app_id_derive' AND tr.tgfoid IN ($needle$;
BEGIN
    SELECT pg_get_functiondef('control.backfill_customer_app_id(regclass,uuid,uuid,integer)'::regprocedure)
      INTO before_sql;
    IF md5(before_sql) <> 'a118bfeefb3ab8cb2e76557f34ca9c4e'
       OR length(before_sql)-length(replace(before_sql,needle,'')) <> length(needle) THEN
        RAISE EXCEPTION '040 unknown 026 backfill definition; review before extending allowlist';
    END IF;
    after_sql := replace(before_sql,needle,
        $insert$ELSIF NOT ((p_table='staging.customer_profile'::regclass
                AND tr.tgname='customer_connector_app_guard'
                AND tr.tgfoid='staging.guard_connector_profile_write()'::regprocedure
                AND tr.tgtype=23)
            OR (p_table='raw.customer_current'::regclass
                AND tr.tgname='customer_connector_app_guard'
                AND tr.tgfoid='raw.guard_connector_customer_write()'::regprocedure
                AND tr.tgtype=23)
            OR (tr.tgname='customer_app_id_derive' AND tr.tgfoid IN ($insert$);
    EXECUTE after_sql;
END $backfill$;

-- Names are GUC-supplied role identifiers, resolved only after catalog checks.
-- No PUBLIC access to owner-rights provenance functions or raw control tables.
DO $grants$
DECLARE api_role text := nullif(current_setting('cdp.connector_api_role',true),'');
        worker_role text := nullif(current_setting('cdp.connector_worker_role',true),'');
BEGIN
    IF current_setting('cdp.connector_owner_runtime',true)='verified-owner-only' THEN
        RETURN; -- Only the verified table owner executes; PUBLIC remains revoked.
    END IF;
    EXECUTE format('GRANT USAGE ON SCHEMA control TO %I',api_role);
    EXECUTE format('GRANT USAGE ON SCHEMA control TO %I',worker_role);
    EXECUTE format('GRANT EXECUTE ON FUNCTION control.bind_connector_batch_app_id(uuid,uuid,text) TO %I',api_role);
    EXECUTE format('GRANT EXECUTE ON FUNCTION control.connector_batch_app_scope(uuid) TO %I',worker_role);
END $grants$;
COMMIT;
