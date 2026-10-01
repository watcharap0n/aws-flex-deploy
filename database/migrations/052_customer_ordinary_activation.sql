-- 052 coordinated ordinary forward activation. Install only with paused writers,
-- matcher and capture after 038/050/051 and a verified 041/044 capture. This is
-- NOT permission to deploy: prove the ordinary worker and public export first.
-- Retain 038's trigger and fail closed on catalog drift or incomplete history.
BEGIN;
SET LOCAL lock_timeout='10s';
LOCK TABLE control.stitching_job,control.ingestion_batch,
    control.source_landing_batch,control.source_system,connector.sync_run,
    control.customer_forward_route,raw.customer,raw.customer_batch_member,
    staging.customer_profile_history,staging.customer_profile,
    identity.customer_entity_member IN SHARE ROW EXCLUSIVE MODE;
DO $preflight$
DECLARE body text;
BEGIN
    IF to_regclass('control.customer_forward_route') IS NULL
       OR to_regclass('staging.customer_profile_member_tuple_key') IS NULL
       OR to_regclass('identity.customer_entity_member_app_source_key') IS NULL
       OR to_regclass('control.customer_identity_member_export') IS NULL
       OR to_regprocedure('raw.apply_customer_forward_bulk(uuid,bigint)') IS NULL
       OR NOT EXISTS (SELECT FROM pg_trigger WHERE tgrelid='control.stitching_job'::regclass
           AND tgname='customer_forward_stitch_gate' AND tgenabled='O') THEN
        RAISE EXCEPTION '052 requires 038/050/041 active catalog and enabled gate';
    END IF;
    SELECT prosrc INTO body FROM pg_proc WHERE oid=to_regprocedure('control.capture_customer_identity_snapshot(uuid)');
    IF body IS NULL OR position('LEFT JOIN staging.customer_profile p ON (p.app_id,p.source_system_id,p.source_customer_id)=' IN body)=0
       OR position('JOIN staging.customer_profile p USING(raw_customer_id)' IN body)>0
       OR position('LEFT JOIN staging.customer_profile_history h ON h.batch_id=b.ingestion_batch_id' IN body)=0
       OR position('Identity snapshot isolation required' IN body)=0 THEN
        RAISE EXCEPTION '052 requires exact 051 logical capture and 044 isolation';
    END IF;
    -- Quarantine ONLY completed, unscoped legacy manual uploads with durable
    -- landing/source agreement and a full, exact raw/member/typed inventory.
    -- An unparseable ref is not connector evidence. Neither NULL app nor the
    -- absence of a sync run on its own is proof of a manual upload. The 038
    -- admission trigger forbids new unscoped uploads; locks freeze this proof.
    -- No row is rewritten, attributed to an app, or omitted from the matcher.
    CREATE TEMP TABLE pg_temp.customer_052_manual_quarantine
        (batch_id uuid PRIMARY KEY) ON COMMIT DROP;
    INSERT INTO pg_temp.customer_052_manual_quarantine(batch_id)
    SELECT b.batch_id FROM control.ingestion_batch b
    JOIN control.source_landing_batch l ON l.batch_id=b.landing_batch_id
    JOIN control.source_system s ON s.source_system_id=b.source_system_id
    WHERE b.app_id IS NULL AND b.status='completed'
      AND b.batch_ref=l.batch_ref AND b.batch_ref IS NOT NULL
      AND l.output_status='PUBLISHED'
      AND l.source_code='manual_upload' AND s.source_code='manual_upload'
      AND b.batch_ref !~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}-[0-9]{6}-([0-9]+)-[0-9a-f]{12}$'
      AND NOT (coalesce(b.metadata,'{}'::jsonb) ? 'connector_sync_run_id')
      AND NOT EXISTS (SELECT FROM connector.sync_run c WHERE c.ingestion_batch_id=b.batch_id
          OR c.landing_batch_id=b.landing_batch_id)
      AND NOT EXISTS (SELECT FROM control.customer_forward_route f WHERE f.source_system_id=b.source_system_id)
      AND EXISTS (SELECT FROM staging.customer_profile_history h WHERE h.batch_id=b.batch_id)
      AND NOT EXISTS (
          SELECT FROM raw.customer_batch_member m
          FULL JOIN raw.customer r ON (r.batch_id,r.raw_customer_id,r.source_system_id,r.source_customer_id)=
              (m.ingestion_batch_id,m.raw_customer_id,m.source_system_id,m.source_customer_id)
              AND r.app_id IS NULL
          FULL JOIN staging.customer_profile_history h ON
              (h.batch_id,h.raw_customer_id,h.source_system_id,h.source_customer_id)=
              (m.ingestion_batch_id,m.raw_customer_id,m.source_system_id,m.source_customer_id)
              AND h.app_id IS NULL
          WHERE coalesce(m.ingestion_batch_id,r.batch_id,h.batch_id)=b.batch_id
            AND (m.raw_customer_id IS NULL OR r.raw_customer_id IS NULL OR h.raw_customer_id IS NULL
              OR m.source_system_id IS DISTINCT FROM b.source_system_id
              OR r.source_system_id IS DISTINCT FROM b.source_system_id
              OR h.source_system_id IS DISTINCT FROM b.source_system_id));
    -- Historical alias UUIDs remain provenance, not logical identities. All
    -- other typed rows still require exact app/source/user representation.
    IF EXISTS (SELECT FROM staging.customer_profile_history h
        JOIN control.ingestion_batch b ON b.batch_id=h.batch_id
        LEFT JOIN raw.customer r ON (r.batch_id,r.raw_customer_id,r.source_system_id,r.source_customer_id)=
            (h.batch_id,h.raw_customer_id,h.source_system_id,h.source_customer_id)
            AND r.app_id IS NOT DISTINCT FROM h.app_id
        LEFT JOIN raw.customer_batch_member bm ON
            (bm.ingestion_batch_id,bm.raw_customer_id,bm.source_system_id,bm.source_customer_id)=
            (h.batch_id,h.raw_customer_id,h.source_system_id,h.source_customer_id)
        LEFT JOIN staging.customer_profile p ON (p.app_id,p.source_system_id,p.source_customer_id)=
            (h.app_id,h.source_system_id,h.source_customer_id)
        LEFT JOIN pg_temp.customer_052_manual_quarantine q ON q.batch_id=h.batch_id
        WHERE q.batch_id IS NULL AND (h.app_id IS NULL OR h.app_id IS DISTINCT FROM b.app_id
          OR r.raw_customer_id IS NULL OR bm.raw_customer_id IS NULL
          OR p.standardized_record_id IS NULL)) THEN
        RAISE EXCEPTION '052 incomplete historical app/source/user representation';
    END IF;
END $preflight$;
-- Replace the 038 guard in-place: same trigger/OID, not a disabled bypass.
CREATE OR REPLACE FUNCTION control.guard_forward_stitch_alias() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $body$
DECLARE scope record;
BEGIN
    IF NEW.status IN ('SUBMITTED','WAITING_FOR_LOCK','VALIDATING','RUNNING','SUCCEEDED')
       AND EXISTS (SELECT FROM control.ingestion_batch b JOIN control.customer_forward_route r
           USING(source_system_id) WHERE b.batch_id=NEW.ingestion_batch_id) THEN
        SELECT * INTO scope FROM control.resolve_customer_identity_batches(ARRAY[NEW.ingestion_batch_id]);
        IF scope.error_code IS NOT NULL OR scope.app_id IS NULL OR NOT EXISTS (
            SELECT FROM control.ingestion_batch b JOIN control.customer_forward_route r
              USING(source_system_id)
            WHERE b.batch_id=NEW.ingestion_batch_id AND b.app_id=r.app_id
              AND b.app_id=scope.app_id) THEN
            RAISE EXCEPTION '052 forward publication scope proof required';
        END IF;
        IF EXISTS (SELECT FROM raw.customer_batch_member bm
            LEFT JOIN raw.customer h ON (h.batch_id,h.raw_customer_id,h.source_system_id,h.source_customer_id)=
                (bm.ingestion_batch_id,bm.raw_customer_id,bm.source_system_id,bm.source_customer_id)
            LEFT JOIN staging.customer_profile_history ph ON
                (ph.batch_id,ph.raw_customer_id,ph.app_id,ph.source_system_id,ph.source_customer_id)=
                (h.batch_id,h.raw_customer_id,h.app_id,h.source_system_id,h.source_customer_id)
            LEFT JOIN staging.customer_profile p ON
                (p.app_id,p.source_system_id,p.source_customer_id)=
                (h.app_id,h.source_system_id,h.source_customer_id)
            WHERE bm.ingestion_batch_id=NEW.ingestion_batch_id
              AND (h.raw_customer_id IS NULL OR h.app_id IS DISTINCT FROM scope.app_id OR
                   (ph.standardized_record_id IS NOT NULL AND p.standardized_record_id IS NULL))) THEN
            RAISE EXCEPTION '052 selected logical identity representation missing';
        END IF;
        IF NEW.status='SUCCEEDED' AND EXISTS (
            SELECT FROM raw.customer_batch_member bm
            LEFT JOIN raw.customer h ON (h.batch_id,h.raw_customer_id,h.source_system_id,h.source_customer_id)=
                (bm.ingestion_batch_id,bm.raw_customer_id,bm.source_system_id,bm.source_customer_id)
            LEFT JOIN staging.customer_profile_history ph ON
                (ph.batch_id,ph.raw_customer_id,ph.app_id,ph.source_system_id,ph.source_customer_id)=
                (h.batch_id,h.raw_customer_id,h.app_id,h.source_system_id,h.source_customer_id)
            LEFT JOIN staging.customer_profile p ON
                (p.app_id,p.source_system_id,p.source_customer_id)=
                (h.app_id,h.source_system_id,h.source_customer_id)
            LEFT JOIN identity.customer_entity_member m ON
                (m.standardized_record_id,m.app_id,m.source_system_id,m.source_customer_id)=
                (p.standardized_record_id,p.app_id,p.source_system_id,p.source_customer_id)
            WHERE bm.ingestion_batch_id=NEW.ingestion_batch_id
              AND (h.raw_customer_id IS NULL OR ph.standardized_record_id IS NULL OR p.standardized_record_id IS NULL
                   OR m.standardized_record_id IS NULL)
        ) THEN
            RAISE EXCEPTION '052 publication requires exact selected normalized member';
        END IF;
        -- A later catalog replacement cannot silently reopen the gate.
        IF NOT EXISTS (SELECT FROM pg_proc WHERE oid=to_regprocedure('control.capture_customer_identity_snapshot(uuid)')
            AND position('LEFT JOIN staging.customer_profile p ON (p.app_id,p.source_system_id,p.source_customer_id)=' IN prosrc)>0
            AND position('JOIN staging.customer_profile p USING(raw_customer_id)' IN prosrc)=0) THEN
            RAISE EXCEPTION '052 capture publication proof drift';
        END IF;
    END IF;
    RETURN NEW;
END $body$;
COMMIT;
