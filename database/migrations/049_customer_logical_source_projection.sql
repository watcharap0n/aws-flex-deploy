-- Candidate only: coordinated ordinary logical-source writer + matcher/publisher
-- rollout required before activation. Do not disable 038 or reinterpret old aliases.
-- New projections deliberately coexist with the OID/FK-bound 019/022 legacy lane.
-- A raw UUID is provenance, never the key of a new logical current/typed row.
BEGIN;
SET LOCAL lock_timeout='5s';
DO $preflight$
BEGIN
    IF to_regclass('raw.customer_current') IS NULL
       OR to_regclass('raw.customer') IS NULL
       OR to_regclass('raw.customer_batch_member') IS NULL
       OR to_regclass('staging.customer_profile_history') IS NULL
       OR to_regclass('raw.customer_logical_current') IS NOT NULL
       OR NOT EXISTS (SELECT FROM pg_constraint WHERE conrelid='raw.customer'::regclass
            AND conname='customer_history_current_fkey') THEN
        RAISE EXCEPTION '049 requires intact 019/022 history/current catalog and no prior logical projection';
    END IF;
END $preflight$;

CREATE TABLE raw.customer_logical_current (
    logical_customer_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    app_id text NOT NULL CHECK (app_id ~ '^[0-9]+$'),
    source_system_id bigint NOT NULL REFERENCES control.source_system(source_system_id),
    source_customer_id varchar(255) NOT NULL CHECK (source_customer_id<>''),
    batch_id uuid NOT NULL REFERENCES control.ingestion_batch(batch_id),
    raw_customer_id uuid NOT NULL,
    first_name text,
    UNIQUE (app_id,source_system_id,source_customer_id),
    UNIQUE (logical_customer_id,app_id,source_system_id,source_customer_id)
);
CREATE TABLE raw.customer_logical_observation (
    snapshot_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    logical_customer_id uuid NOT NULL,
    app_id text NOT NULL,
    source_system_id bigint NOT NULL,
    source_customer_id varchar(255) NOT NULL,
    batch_id uuid NOT NULL REFERENCES control.ingestion_batch(batch_id),
    raw_customer_id uuid NOT NULL,
    first_name text,
    recorded_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (batch_id,logical_customer_id),
    UNIQUE (batch_id,logical_customer_id,raw_customer_id),
    UNIQUE (batch_id,raw_customer_id),
    FOREIGN KEY (logical_customer_id,app_id,source_system_id,source_customer_id)
       REFERENCES raw.customer_logical_current(logical_customer_id,app_id,source_system_id,source_customer_id)
);
CREATE INDEX customer_logical_observation_raw_provenance
    ON raw.customer_logical_observation(raw_customer_id,batch_id);
-- Stable standardized ID is local to a logical source, not to its reused raw UUID.
CREATE TABLE staging.customer_logical_profile_current (
    logical_customer_id uuid PRIMARY KEY REFERENCES raw.customer_logical_current(logical_customer_id),
    standardized_record_id uuid NOT NULL UNIQUE DEFAULT gen_random_uuid(),
    app_id text NOT NULL,
    source_system_id bigint NOT NULL,
    source_customer_id varchar(255) NOT NULL,
    batch_id uuid NOT NULL,
    raw_customer_id uuid NOT NULL,
    first_name_raw text,
    FOREIGN KEY (logical_customer_id,app_id,source_system_id,source_customer_id)
       REFERENCES raw.customer_logical_current(logical_customer_id,app_id,source_system_id,source_customer_id),
    FOREIGN KEY (batch_id,logical_customer_id,raw_customer_id)
       REFERENCES raw.customer_logical_observation(batch_id,logical_customer_id,raw_customer_id)
);
CREATE TABLE staging.customer_logical_profile_history (
    batch_id uuid NOT NULL,
    logical_customer_id uuid NOT NULL,
    standardized_record_id uuid NOT NULL,
    raw_customer_id uuid NOT NULL,
    first_name_raw text,
    PRIMARY KEY(batch_id,logical_customer_id),
    FOREIGN KEY (batch_id,logical_customer_id,raw_customer_id)
       REFERENCES raw.customer_logical_observation(batch_id,logical_customer_id,raw_customer_id)
);
CREATE FUNCTION raw.reject_customer_logical_history_mutation() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $body$
BEGIN RAISE EXCEPTION 'Logical customer observations are immutable'; END $body$;
REVOKE ALL ON FUNCTION raw.reject_customer_logical_history_mutation() FROM PUBLIC;
CREATE FUNCTION raw.guard_customer_logical_current_lineage() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $body$
BEGIN
    IF NOT EXISTS (SELECT FROM control.ingestion_batch b
       WHERE b.batch_id=NEW.batch_id AND b.app_id=NEW.app_id
         AND b.source_system_id=NEW.source_system_id) THEN
        RAISE EXCEPTION 'logical current batch lineage mismatch';
    END IF;
    RETURN NEW;
END $body$;
REVOKE ALL ON FUNCTION raw.guard_customer_logical_current_lineage() FROM PUBLIC;
CREATE TRIGGER customer_logical_current_lineage BEFORE INSERT OR UPDATE
    ON raw.customer_logical_current FOR EACH ROW
    EXECUTE FUNCTION raw.guard_customer_logical_current_lineage();
CREATE FUNCTION raw.guard_customer_logical_observation_lineage() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $body$
BEGIN
    IF NOT EXISTS (SELECT FROM control.ingestion_batch b
       WHERE b.batch_id=NEW.batch_id AND b.app_id=NEW.app_id
         AND b.source_system_id=NEW.source_system_id) THEN
        RAISE EXCEPTION 'logical observation batch lineage mismatch';
    END IF;
    RETURN NEW;
END $body$;
REVOKE ALL ON FUNCTION raw.guard_customer_logical_observation_lineage() FROM PUBLIC;
CREATE TRIGGER customer_logical_observation_lineage BEFORE INSERT
    ON raw.customer_logical_observation FOR EACH ROW
    EXECUTE FUNCTION raw.guard_customer_logical_observation_lineage();
CREATE TRIGGER customer_logical_observation_immutable BEFORE UPDATE OR DELETE OR TRUNCATE
    ON raw.customer_logical_observation FOR EACH STATEMENT EXECUTE FUNCTION raw.reject_customer_logical_history_mutation();
CREATE TRIGGER customer_logical_profile_history_immutable BEFORE UPDATE OR DELETE OR TRUNCATE
    ON staging.customer_logical_profile_history FOR EACH STATEMENT EXECUTE FUNCTION raw.reject_customer_logical_history_mutation();

CREATE FUNCTION raw.apply_customer_logical_source_bulk(p_batch uuid,p_source bigint) RETURNS bigint
LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $body$
DECLARE app text; total bigint; bad bigint;
BEGIN
    -- This lock serializes competing ordinary writers within one source. Another
    -- app may independently own the same source/user and even raw provenance UUID.
    PERFORM pg_advisory_xact_lock(hashtextextended('customer-logical:'||p_source::text,0));
    SELECT b.app_id INTO app FROM control.ingestion_batch b
        WHERE b.batch_id=p_batch AND b.source_system_id=p_source;
    IF app IS NULL OR app !~ '^[0-9]+$' THEN
        RAISE EXCEPTION 'logical batch app/source lineage required';
    END IF;
    SELECT count(*) INTO total FROM pg_temp.customer_logical_source_stage;
    IF EXISTS (SELECT FROM pg_temp.customer_logical_source_stage s
        WHERE s.batch_id IS DISTINCT FROM p_batch OR s.source_system_id IS DISTINCT FROM p_source
            OR s.source_customer_id='' OR s.source_customer_id IS NULL OR s.raw_customer_id IS NULL)
       OR (SELECT count(DISTINCT source_customer_id) FROM pg_temp.customer_logical_source_stage)<>total THEN
        RAISE EXCEPTION 'logical stage lineage mismatch';
    END IF;
    -- The same app/source/user may arrive with a different observation UUID.
    -- Refuse only a same-batch conflict: batch/raw uniqueness and replay
    -- equality below own that boundary; never treat the old raw UUID as key.
    INSERT INTO raw.customer_logical_current
        (app_id,source_system_id,source_customer_id,batch_id,raw_customer_id,first_name)
    SELECT app,p_source,s.source_customer_id,p_batch,s.raw_customer_id,s.first_name
    FROM pg_temp.customer_logical_source_stage s
    ON CONFLICT (app_id,source_system_id,source_customer_id) DO NOTHING;
    IF EXISTS (SELECT FROM pg_temp.customer_logical_source_stage s
        JOIN raw.customer_logical_current c ON (c.app_id,c.source_system_id,c.source_customer_id)=
            (app,p_source,s.source_customer_id)
        JOIN raw.customer_logical_observation h ON (h.batch_id,h.logical_customer_id)=
            (p_batch,c.logical_customer_id)
        WHERE (h.raw_customer_id,h.first_name) IS DISTINCT FROM (s.raw_customer_id,s.first_name)) THEN
        RAISE EXCEPTION 'logical observation replay differs';
    END IF;
    INSERT INTO raw.customer_logical_observation
        (logical_customer_id,app_id,source_system_id,source_customer_id,batch_id,raw_customer_id,first_name)
    SELECT c.logical_customer_id,app,p_source,s.source_customer_id,p_batch,s.raw_customer_id,s.first_name
    FROM pg_temp.customer_logical_source_stage s
    JOIN raw.customer_logical_current c ON (c.app_id,c.source_system_id,c.source_customer_id)=
        (app,p_source,s.source_customer_id)
    ON CONFLICT (batch_id,logical_customer_id) DO NOTHING;
    -- A batch created before the current winner is evidence, not a rewind.
    UPDATE raw.customer_logical_current c SET batch_id=p_batch,raw_customer_id=s.raw_customer_id,
        first_name=s.first_name
    FROM pg_temp.customer_logical_source_stage s,control.ingestion_batch previous,
         control.ingestion_batch incoming
    WHERE (c.app_id,c.source_system_id,c.source_customer_id)=(app,p_source,s.source_customer_id)
      AND previous.batch_id=c.batch_id AND incoming.batch_id=p_batch
      AND (incoming.created_at,incoming.batch_id)>(previous.created_at,previous.batch_id);
    INSERT INTO staging.customer_logical_profile_current
        (logical_customer_id,app_id,source_system_id,source_customer_id,batch_id,raw_customer_id,first_name_raw)
    SELECT c.logical_customer_id,app,p_source,s.source_customer_id,c.batch_id,c.raw_customer_id,c.first_name
    FROM pg_temp.customer_logical_source_stage s JOIN raw.customer_logical_current c
      ON (c.app_id,c.source_system_id,c.source_customer_id)=(app,p_source,s.source_customer_id)
    ON CONFLICT (logical_customer_id) DO UPDATE SET batch_id=EXCLUDED.batch_id,
      raw_customer_id=EXCLUDED.raw_customer_id,first_name_raw=EXCLUDED.first_name_raw;
    IF EXISTS(SELECT FROM pg_temp.customer_logical_source_stage s
        JOIN raw.customer_logical_current c ON (c.app_id,c.source_system_id,c.source_customer_id)=
           (app,p_source,s.source_customer_id)
        JOIN staging.customer_logical_profile_history h ON
           (h.batch_id,h.logical_customer_id)=(p_batch,c.logical_customer_id)
        WHERE (h.raw_customer_id,h.first_name_raw) IS DISTINCT FROM (s.raw_customer_id,s.first_name)) THEN
        RAISE EXCEPTION 'logical typed replay differs';
    END IF;
    INSERT INTO staging.customer_logical_profile_history
       (batch_id,logical_customer_id,standardized_record_id,raw_customer_id,first_name_raw)
    SELECT p_batch,c.logical_customer_id,p.standardized_record_id,s.raw_customer_id,s.first_name
    FROM pg_temp.customer_logical_source_stage s
    JOIN raw.customer_logical_current c ON (c.app_id,c.source_system_id,c.source_customer_id)=
         (app,p_source,s.source_customer_id)
    JOIN staging.customer_logical_profile_current p USING(logical_customer_id)
    ON CONFLICT (batch_id,logical_customer_id) DO NOTHING;
    RETURN total;
END $body$;
REVOKE ALL ON FUNCTION raw.apply_customer_logical_source_bulk(uuid,bigint) FROM PUBLIC;
COMMENT ON FUNCTION raw.apply_customer_logical_source_bulk(uuid,bigint) IS
 '049 coordinated-only owner SQL entrypoint; temp stage has batch_id,raw_customer_id,source_system_id,source_customer_id,first_name. Not a replacement for 022/038 or a matcher activation.';
COMMIT;
