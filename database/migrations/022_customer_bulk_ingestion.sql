-- COORDINATED WRITER ROLLOUT REQUIRED. Pause Lambda AND connector, install this
-- migration and move both to raw.apply_customer_bulk(uuid,bigint) before resume.
-- The old connector INSERT/UPSERT still safely captures forward observations,
-- but late direct UPDATE is now rejected (not silently rewound/dropped). It must
-- stage and call the bulk function to preserve late observations. No bypass GUC,
-- replication-role change, SECURITY DEFINER, disabled FK or deferred FK queue.
-- Re-run the generated functions if customer_current typed columns are changed.
BEGIN;
SET LOCAL lock_timeout = '10s';
LOCK TABLE raw.customer_current, raw.customer, raw.customer_batch_member IN ACCESS EXCLUSIVE MODE;
DROP TRIGGER customer_history_capture ON raw.customer_current;
DROP TRIGGER customer_history_capture_update ON raw.customer_current;

-- Generate static typed SQL once at migration, never catalog/JSON per row.
-- Function-scoped enable_nestloop only affects query planning and automatically
-- restores caller settings. Fresh-batch statistics plus EXISTS otherwise chose
-- a measured quadratic materialized scan. No integrity setting is altered.
DO $migration$
DECLARE
    cols text; new_cols text; stage_cols text; history_cols text;
    typed_new text; typed_history text; typed_stage text; assignments text;
BEGIN
    SELECT string_agg(format('%I',attname),',' ORDER BY attnum),
           string_agg(format('n.%I',attname),',' ORDER BY attnum),
           string_agg(CASE WHEN attname='raw_customer_id' THEN 'c.raw_customer_id'
                          WHEN attname='customer_id' THEN 'c.customer_id'
                          ELSE format('s.%I',attname) END,',' ORDER BY attnum),
           string_agg(format('h.%I',attname),',' ORDER BY attnum),
           string_agg(format('n.%I',attname),',' ORDER BY attnum)
             FILTER (WHERE attname NOT IN ('created_at','updated_at','ingested_at')),
           string_agg(format('h.%I',attname),',' ORDER BY attnum)
             FILTER (WHERE attname NOT IN ('created_at','updated_at','ingested_at')),
           string_agg(CASE WHEN attname='raw_customer_id' THEN 'c.raw_customer_id'
                          WHEN attname='customer_id' THEN 'c.customer_id'
                          ELSE format('s.%I',attname) END,',' ORDER BY attnum)
             FILTER (WHERE attname NOT IN ('created_at','updated_at','ingested_at')),
           string_agg(format('%I=s.%I',attname,attname),',' ORDER BY attnum)
             FILTER (WHERE attname NOT IN ('raw_customer_id','customer_id','source_system_id','source_customer_id','created_at','updated_at','ingested_at'))
    INTO cols,new_cols,stage_cols,history_cols,typed_new,typed_history,typed_stage,assignments
    FROM pg_attribute WHERE attrelid='raw.customer_current'::regclass
       AND attnum>0 AND NOT attisdropped;

    EXECUTE format($ddl$
CREATE FUNCTION raw.capture_customer_history_statement() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp SET enable_nestloop = off AS $body$
BEGIN
    IF TG_OP='UPDATE' THEN
        IF EXISTS (
            SELECT FROM old_rows o FULL JOIN new_rows n USING (raw_customer_id)
            LEFT JOIN control.ingestion_batch nb ON nb.batch_id=n.batch_id
            LEFT JOIN control.ingestion_batch ob ON ob.batch_id=o.batch_id
            WHERE o.raw_customer_id IS NULL OR n.raw_customer_id IS NULL
               OR (n.source_system_id,n.source_customer_id) IS DISTINCT FROM
                  (o.source_system_id,o.source_customer_id)
               OR (nb.created_at,nb.batch_id) < (ob.created_at,ob.batch_id)
        ) THEN
            RAISE EXCEPTION 'Current identity cannot change or rewind; use raw.apply_customer_bulk for late observations';
        END IF;
    END IF;
    INSERT INTO raw.customer (%1$s) SELECT %2$s FROM new_rows n
    ON CONFLICT DO NOTHING;
    IF EXISTS (
        SELECT FROM new_rows n LEFT JOIN raw.customer h
          ON h.batch_id=n.batch_id AND h.raw_customer_id=n.raw_customer_id
        WHERE h.raw_customer_id IS NULL OR ROW(%3$s) IS DISTINCT FROM ROW(%4$s)
    ) THEN
        RAISE EXCEPTION 'Customer batch snapshot cannot change for an existing batch/source identity';
    END IF;
    INSERT INTO raw.customer_batch_member(ingestion_batch_id,raw_customer_id,source_system_id,source_customer_id)
      SELECT batch_id,raw_customer_id,source_system_id,source_customer_id FROM new_rows
      ON CONFLICT DO NOTHING;
    IF EXISTS (SELECT FROM new_rows n LEFT JOIN raw.customer_batch_member m
      ON m.ingestion_batch_id=n.batch_id AND m.raw_customer_id=n.raw_customer_id
      WHERE m.raw_customer_id IS NULL OR (m.source_system_id,m.source_customer_id)
        IS DISTINCT FROM (n.source_system_id,n.source_customer_id)) THEN
        RAISE EXCEPTION 'Customer batch membership does not match source identity';
    END IF;
    RETURN NULL;
END $body$;
$ddl$, cols,new_cols,typed_new,typed_history);

    EXECUTE format($ddl$
CREATE FUNCTION raw.apply_customer_bulk(p_batch uuid, p_source bigint) RETURNS bigint
LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp SET enable_nestloop = off
SET default_statistics_target = 10 AS $body$
DECLARE selected_count bigint; t timestamptz := clock_timestamp(); timings jsonb := '{}';
BEGIN
    -- Serializes overlapping batches for one source; held until caller COMMIT.
    -- Other sources run independently. Hash collisions only serialize extra work.
    PERFORM pg_advisory_xact_lock(hashtextextended('customer-bulk:' || p_source::text,0));
    IF NOT EXISTS (SELECT FROM control.ingestion_batch
                   WHERE batch_id=p_batch AND source_system_id=p_source) THEN
        RAISE EXCEPTION 'Customer bulk batch/source lineage does not match';
    END IF;
    IF EXISTS (SELECT FROM pg_temp.customer_bulk_stage
               WHERE batch_id IS DISTINCT FROM p_batch OR source_system_id IS DISTINCT FROM p_source) THEN
        RAISE EXCEPTION 'Customer bulk stage contains a different batch/source';
    END IF;
    SELECT count(*) INTO selected_count FROM pg_temp.customer_bulk_stage;
    -- Wide transient batches need join/cardinality statistics, not large JSON
    -- samples. Aurora 150k benchmarks validated target 10; function scope keeps
    -- all caller/global settings unchanged on success and error.
    ANALYZE pg_temp.customer_bulk_stage;
    timings := jsonb_build_object('lock_validate_analyze',extract(epoch FROM clock_timestamp()-t)); t:=clock_timestamp();
    -- Missing current identities are inserted first. The statement trigger writes
    -- their history/membership before return, retaining immediate FK enforcement.
    INSERT INTO raw.customer_current (%1$s)
      SELECT %2$s FROM pg_temp.customer_bulk_stage n
      WHERE NOT EXISTS (SELECT FROM raw.customer_current c
          WHERE c.source_system_id=p_source AND c.source_customer_id=n.source_customer_id)
      ORDER BY n.source_customer_id
      ON CONFLICT (source_system_id,source_customer_id) DO NOTHING;
    timings := timings || jsonb_build_object('new_current_history_membership',extract(epoch FROM clock_timestamp()-t)); t:=clock_timestamp();
    -- Existing, replay and late-first observations use stable current raw IDs.
    INSERT INTO raw.customer (%1$s)
      SELECT %3$s FROM pg_temp.customer_bulk_stage s JOIN raw.customer_current c
        ON c.source_system_id=p_source AND c.source_customer_id=s.source_customer_id
      WHERE NOT EXISTS (SELECT FROM raw.customer h
        WHERE h.batch_id=p_batch AND h.raw_customer_id=c.raw_customer_id)
      ON CONFLICT DO NOTHING;
    timings := timings || jsonb_build_object('existing_history_insert',extract(epoch FROM clock_timestamp()-t)); t:=clock_timestamp();
    -- NOT NULL key + typed ROW equality; JSONB equality, date/time and NULL
    -- semantics are PostgreSQL's, not Python/string/JSON serialization equality.
    -- A full aggregate deliberately avoids EXISTS's startup-cost row goal.
    -- Fresh batch statistics otherwise choose a quadratic materialized join.
    IF (SELECT count(*)
      FROM pg_temp.customer_bulk_stage s JOIN raw.customer_current c
        ON c.source_system_id=p_source AND c.source_customer_id=s.source_customer_id
      LEFT JOIN raw.customer h ON h.batch_id=p_batch AND h.raw_customer_id=c.raw_customer_id
      WHERE h.raw_customer_id IS NULL OR ROW(%4$s) IS DISTINCT FROM ROW(%5$s)
    ) > 0 THEN
        RAISE EXCEPTION 'Customer batch snapshot cannot change for an existing batch/source identity';
    END IF;
    timings := timings || jsonb_build_object('history_compare',extract(epoch FROM clock_timestamp()-t)); t:=clock_timestamp();
    UPDATE raw.customer_current c SET %6$s,updated_at=now(),ingested_at=now()
      FROM pg_temp.customer_bulk_stage s,control.ingestion_batch ob,control.ingestion_batch nb
      WHERE c.source_system_id=p_source AND c.source_customer_id=s.source_customer_id
        AND ob.batch_id=c.batch_id AND nb.batch_id=p_batch
        AND (nb.created_at,nb.batch_id) > (ob.created_at,ob.batch_id);
    timings := timings || jsonb_build_object('current_update',extract(epoch FROM clock_timestamp()-t)); t:=clock_timestamp();
    INSERT INTO raw.customer_batch_member(ingestion_batch_id,raw_customer_id,source_system_id,source_customer_id)
      SELECT p_batch,c.raw_customer_id,p_source,c.source_customer_id
      FROM pg_temp.customer_bulk_stage s JOIN raw.customer_current c
        ON c.source_system_id=p_source AND c.source_customer_id=s.source_customer_id
      ON CONFLICT DO NOTHING;
    IF EXISTS (SELECT FROM pg_temp.customer_bulk_stage s
      JOIN raw.customer_current c ON c.source_system_id=p_source AND c.source_customer_id=s.source_customer_id
      LEFT JOIN raw.customer_batch_member m ON m.ingestion_batch_id=p_batch AND m.raw_customer_id=c.raw_customer_id
      WHERE m.raw_customer_id IS NULL OR (m.source_system_id,m.source_customer_id)
          IS DISTINCT FROM (c.source_system_id,c.source_customer_id)) THEN
        RAISE EXCEPTION 'Customer batch membership does not match source identity';
    END IF;
    timings := timings || jsonb_build_object('membership',extract(epoch FROM clock_timestamp()-t));
    RAISE NOTICE 'customer_bulk_sql %%',timings;
    RETURN selected_count;
END $body$;
$ddl$, cols,new_cols,stage_cols,typed_stage,typed_history,assignments);
END $migration$;
CREATE TRIGGER customer_history_capture_insert_statement AFTER INSERT ON raw.customer_current
REFERENCING NEW TABLE AS new_rows FOR EACH STATEMENT EXECUTE FUNCTION raw.capture_customer_history_statement();
CREATE TRIGGER customer_history_capture_update_statement AFTER UPDATE ON raw.customer_current
REFERENCING OLD TABLE AS old_rows NEW TABLE AS new_rows FOR EACH STATEMENT EXECUTE FUNCTION raw.capture_customer_history_statement();
COMMENT ON FUNCTION raw.apply_customer_bulk(uuid,bigint) IS
 'Invoker-rights typed temp stage merge, source-serialized until commit; caller owns transaction and selected-key completeness. Requires pg_temp.customer_bulk_stage LIKE raw.customer_current with unique source_customer_id. Legacy late direct UPDATE rejects; coordinated connector rollout required.';
COMMIT;
