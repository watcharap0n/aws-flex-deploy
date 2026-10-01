-- 054: permit only ordinary ingestion runtime bookkeeping, not provenance edits.
-- Apply after 038/050-053 with customer writers paused. No row rewrites.
BEGIN;
SET LOCAL lock_timeout='10s';
DO $preflight$
DECLARE body text;
BEGIN
    SELECT prosrc INTO body FROM pg_proc
      WHERE oid=to_regprocedure('control.guard_customer_forward_batch()');
    IF body IS NULL OR
       encode(digest(body,'sha256'),'hex') <> 'bb8edb2bfcd92c2c3de525f9d0b886bad2519ca800087707965e4f4671efe3e6' OR
       NOT EXISTS (SELECT FROM pg_trigger
           WHERE tgrelid='control.ingestion_batch'::regclass
             AND tgname='customer_forward_batch_immutable' AND tgenabled='O'
             AND tgfoid='control.guard_customer_forward_batch()'::regprocedure)
       OR to_regclass('control.customer_forward_route') IS NULL
       OR to_regprocedure('control.guard_forward_stitch_alias()') IS NULL
       OR to_regclass('identity.customer_entity_member_app_source_key') IS NULL
       OR NOT EXISTS (SELECT FROM pg_constraint
           WHERE conrelid='raw.customer'::regclass
             AND conname='customer_history_member_tuple_fkey' AND convalidated) THEN
        RAISE EXCEPTION '054 requires intact 038/050-053 forward catalog';
    END IF;
END $preflight$;
-- Keep the 038 trigger, OID, connector branch and source/app/landing protection.
-- Remove only operational keys when comparing metadata. The complement,
-- including bucket, event_time and landing_batch_id, stays byte-for-byte JSONB
-- immutable. An upload can never acquire a connector marker.
CREATE OR REPLACE FUNCTION control.guard_customer_forward_batch() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $body$
BEGIN
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
       (NEW.metadata - ARRAY['duplicate_rows','stitching_job_id',
           'validation_run_id','ingestion_error_code','stitching_task_arn',
           'stitching_dispatch_status','stitching_dispatch_error',
           'stitching_exit_code','stitching_stop_code']) IS DISTINCT FROM
       (OLD.metadata - ARRAY['duplicate_rows','stitching_job_id',
           'validation_run_id','ingestion_error_code','stitching_task_arn',
           'stitching_dispatch_status','stitching_dispatch_error',
           'stitching_exit_code','stitching_stop_code']) OR
       (NEW.metadata ? 'connector_sync_run_id') OR OLD.metadata IS NULL THEN
        RAISE EXCEPTION '038 batch source/app lineage is immutable';
    END IF;
    RETURN NEW;
END $body$;
COMMIT;
