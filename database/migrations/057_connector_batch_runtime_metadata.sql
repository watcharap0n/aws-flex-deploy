-- 057: let the connector worker finish its own batch. 038/054 froze ALL metadata
-- of connector batches, but the connector worker's _finalize_lineage adds
-- bucket, connector_manifest_key, duplicate_rows and stitching_job_id, and
-- admission adds the 054 stitching_* keys. Every DATASET_SYNC therefore failed with
-- '038 connector batch lineage is immutable' (found by the 2026-10-19 prod E2E).
-- Connector batches now get the same operational allowance as uploads (054 keys),
-- plus bucket/connector_manifest_key as set-once values. connector_sync_run_id,
-- source_system_id, landing_batch_id, batch_ref and every other key stay immutable.
-- The upload branch is unchanged from 054. No row rewrites.
BEGIN;
SET LOCAL lock_timeout='10s';
DO $preflight$
DECLARE body text;
BEGIN
    SELECT prosrc INTO body FROM pg_proc
      WHERE oid=to_regprocedure('control.guard_customer_forward_batch()');
    IF body IS NULL OR
       encode(digest(body,'sha256'),'hex') <> '9311ce78592628769c2998d911369ce0ebe8364cbfc5788228cd0d85c09f7e66' OR
       NOT EXISTS (SELECT FROM pg_trigger
           WHERE tgrelid='control.ingestion_batch'::regclass
             AND tgname='customer_forward_batch_immutable' AND tgenabled='O'
             AND tgfoid='control.guard_customer_forward_batch()'::regprocedure) THEN
        RAISE EXCEPTION '057 requires the installed 054 guard';
    END IF;
END $preflight$;
CREATE OR REPLACE FUNCTION control.guard_customer_forward_batch() RETURNS trigger
LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $body$
BEGIN
    IF OLD.metadata ? 'connector_sync_run_id' THEN
        IF NOT (NEW.metadata ? 'connector_sync_run_id') OR
           (NEW.source_system_id,NEW.landing_batch_id,NEW.batch_ref)
              IS DISTINCT FROM
           (OLD.source_system_id,OLD.landing_batch_id,OLD.batch_ref) OR
           (NEW.metadata - ARRAY['duplicate_rows','stitching_job_id',
               'validation_run_id','ingestion_error_code','stitching_task_arn',
               'stitching_dispatch_status','stitching_dispatch_error',
               'stitching_exit_code','stitching_stop_code',
               'bucket','connector_manifest_key']) IS DISTINCT FROM
           (OLD.metadata - ARRAY['duplicate_rows','stitching_job_id',
               'validation_run_id','ingestion_error_code','stitching_task_arn',
               'stitching_dispatch_status','stitching_dispatch_error',
               'stitching_exit_code','stitching_stop_code',
               'bucket','connector_manifest_key']) OR
           (OLD.metadata ? 'bucket' AND
               NEW.metadata->'bucket' IS DISTINCT FROM OLD.metadata->'bucket') OR
           (OLD.metadata ? 'connector_manifest_key' AND
               NEW.metadata->'connector_manifest_key' IS DISTINCT FROM
               OLD.metadata->'connector_manifest_key') THEN
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
