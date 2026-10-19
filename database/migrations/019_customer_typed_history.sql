-- Coordinated cutover: pause ingestion AND stitching before applying.
-- Intentionally one-shot: rerunning fails rather than silently accepting drift.
BEGIN;
SET LOCAL lock_timeout = '10s';
LOCK TABLE raw.customer IN ACCESS EXCLUSIVE MODE;
-- Rename preserves the OID: all existing identity/quality/membership FKs and
-- dependent views continue to reference current identity, not observations.
ALTER TABLE raw.customer RENAME TO customer_current;
ALTER TABLE raw.customer_current ADD CONSTRAINT customer_current_source_identity_key
    UNIQUE (raw_customer_id,source_system_id,source_customer_id);
CREATE TABLE raw.customer (LIKE raw.customer_current INCLUDING DEFAULTS INCLUDING CONSTRAINTS INCLUDING STORAGE);
ALTER TABLE raw.customer
    ADD COLUMN snapshot_id uuid NOT NULL DEFAULT gen_random_uuid(),
    ADD COLUMN snapshot_origin text NOT NULL DEFAULT 'INGESTION'
        CHECK (snapshot_origin IN ('INGESTION','CUTOVER_CURRENT','CANONICAL_RECONSTRUCTION')),
    ADD COLUMN snapshot_recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    ADD CONSTRAINT customer_history_pkey PRIMARY KEY (batch_id, raw_customer_id),
    ADD CONSTRAINT customer_history_snapshot_key UNIQUE (snapshot_id),
    ADD CONSTRAINT customer_history_source_key UNIQUE (batch_id,source_system_id,source_customer_id),
    ADD CONSTRAINT customer_history_current_fkey FOREIGN KEY (raw_customer_id,source_system_id,source_customer_id)
        REFERENCES raw.customer_current(raw_customer_id,source_system_id,source_customer_id),
    ADD CONSTRAINT customer_history_batch_fkey FOREIGN KEY (batch_id) REFERENCES control.ingestion_batch(batch_id);
CREATE INDEX customer_history_raw_id ON raw.customer(raw_customer_id,batch_id);
CREATE INDEX customer_history_source ON raw.customer(source_system_id,source_customer_id,batch_id);
DO $seed$
DECLARE columns_sql text;
BEGIN
    SELECT string_agg(quote_ident(attname), ', ' ORDER BY attnum) INTO columns_sql
    FROM pg_attribute WHERE attrelid='raw.customer_current'::regclass AND attnum>0 AND NOT attisdropped;
    EXECUTE format('INSERT INTO raw.customer (%s,snapshot_origin) SELECT %s,''CUTOVER_CURRENT'' FROM raw.customer_current', columns_sql, columns_sql);
END $seed$;
CREATE FUNCTION raw.reject_customer_history_mutation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN RAISE EXCEPTION 'Customer historical observations are immutable'; END $$;
CREATE TRIGGER customer_history_immutable BEFORE UPDATE OR DELETE OR TRUNCATE ON raw.customer
FOR EACH STATEMENT EXECUTE FUNCTION raw.reject_customer_history_mutation();
CREATE FUNCTION raw.capture_customer_history() RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE existing jsonb; incoming jsonb; columns_sql text;
BEGIN
    incoming := to_jsonb(NEW) - ARRAY['created_at','updated_at','ingested_at'];
    SELECT to_jsonb(h) - ARRAY['snapshot_id','snapshot_origin','snapshot_recorded_at','created_at','updated_at','ingested_at']
      INTO existing FROM raw.customer h WHERE h.batch_id=NEW.batch_id AND h.raw_customer_id=NEW.raw_customer_id;
    IF FOUND THEN
        IF existing IS DISTINCT FROM incoming THEN
            RAISE EXCEPTION 'Customer batch snapshot cannot change for an existing batch/source identity';
        END IF;
    ELSE
        SELECT string_agg(quote_ident(attname), ', ' ORDER BY attnum) INTO columns_sql
        FROM pg_attribute WHERE attrelid='raw.customer_current'::regclass AND attnum>0 AND NOT attisdropped;
        EXECUTE format('INSERT INTO raw.customer (%s) SELECT ($1).*', columns_sql) USING NEW;
    END IF;
    -- UPDATE holds the current identity row lock, including ON CONFLICT writers.
    -- Capture even a first late arrival before suppressing its current mutation.
    IF TG_OP = 'UPDATE' THEN
        IF (SELECT (n.created_at,n.batch_id) <= (o.created_at,o.batch_id)
            FROM control.ingestion_batch n CROSS JOIN control.ingestion_batch o
            WHERE n.batch_id=NEW.batch_id AND o.batch_id=OLD.batch_id) THEN
            RETURN NULL;
        END IF;
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER customer_history_capture AFTER INSERT ON raw.customer_current
FOR EACH ROW EXECUTE FUNCTION raw.capture_customer_history();
CREATE TRIGGER customer_history_capture_update BEFORE UPDATE ON raw.customer_current
FOR EACH ROW EXECUTE FUNCTION raw.capture_customer_history();
-- Batch creation order, not worker arrival/commit time, defines current. UUID is
-- the deterministic tie breaker for batches created in the same transaction.
ALTER TABLE control.ingestion_batch ALTER COLUMN created_at SET NOT NULL;
CREATE FUNCTION raw.reject_customer_batch_reordering() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.created_at IS DISTINCT FROM OLD.created_at OR NEW.batch_id IS DISTINCT FROM OLD.batch_id THEN
        RAISE EXCEPTION 'Ingestion batch ordering is immutable';
    END IF;
    RETURN NEW;
END $$;
CREATE TRIGGER customer_batch_order_immutable BEFORE UPDATE ON control.ingestion_batch
FOR EACH ROW EXECUTE FUNCTION raw.reject_customer_batch_reordering();
-- Normalized observations must not overwrite the current identity profile when
-- an older queued batch is validated after a newer upload.
CREATE TABLE staging.customer_profile_history (LIKE staging.customer_profile INCLUDING DEFAULTS INCLUDING CONSTRAINTS INCLUDING STORAGE);
ALTER TABLE staging.customer_profile_history
    ALTER COLUMN batch_id SET NOT NULL,
    ADD PRIMARY KEY(batch_id,raw_customer_id),
    ADD FOREIGN KEY(batch_id,raw_customer_id) REFERENCES raw.customer(batch_id,raw_customer_id);
CREATE TRIGGER customer_profile_history_immutable BEFORE UPDATE OR DELETE OR TRUNCATE ON staging.customer_profile_history
FOR EACH STATEMENT EXECUTE FUNCTION raw.reject_customer_history_mutation();
-- Copy explicit table ACLs, without broadening runtime roles or relying on
-- defaults for new relations. History never needs UPDATE/DELETE/TRUNCATE.
DO $grants$
DECLARE r record; target text;
BEGIN
    FOR r IN SELECT c.relname,a.grantee,a.privilege_type,a.is_grantable
      FROM pg_class c CROSS JOIN LATERAL aclexplode(c.relacl) a
      WHERE c.oid IN ('raw.customer_current'::regclass,'staging.customer_profile'::regclass)
        AND a.privilege_type IN ('SELECT','INSERT','REFERENCES') LOOP
        target := CASE r.relname WHEN 'customer_current' THEN 'raw.customer' ELSE 'staging.customer_profile_history' END;
        EXECUTE format('GRANT %s ON %s TO %s%s',r.privilege_type,target,
            CASE WHEN r.grantee=0 THEN 'PUBLIC' ELSE quote_ident(pg_get_userbyid(r.grantee)) END,
            CASE WHEN r.is_grantable THEN ' WITH GRANT OPTION' ELSE '' END);
    END LOOP;
END $grants$;
COMMENT ON TABLE raw.customer IS 'Immutable typed per-ingestion-batch observations; raw_customer_id is stable source identity, snapshot_id is observation identity.';
COMMENT ON COLUMN raw.customer_current.batch_id IS 'Latest committed ingestion for this source identity. Never use for historical evidence.';
COMMIT;
