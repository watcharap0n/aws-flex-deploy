-- Bounded lock-capture retry for fresh successful staging stitch transactions.
-- Never recapture already BLOCKED transfers. Retry lock acquisition only: every
-- failed NOWAIT attempt runs inside its own PL/pgSQL exception subtransaction,
-- releasing acquired SHARE lock prefixes before waiting. Retain the outer global
-- advisory transaction lock and the existing source/membership validation.
-- Manual migration after 041; strict deployed trigger body pin and private ACL.
BEGIN;
SET LOCAL lock_timeout='5s';
DO $guard$
DECLARE p record; t record;
BEGIN
    SELECT oid,proowner,proacl,prosecdef,provolatile,proparallel,prokind,proconfig,pronargs,
           encode(digest(pg_get_functiondef(oid),'sha256'),'hex') AS definition_hash,
           encode(digest(prosrc,'sha256'),'hex') AS body_hash
      INTO p FROM pg_proc WHERE oid=to_regprocedure('control.capture_customer_identity_after_stitch()');
    IF p.oid IS NULL OR p.proowner <> (SELECT oid FROM pg_roles WHERE rolname='postgres')
       OR p.proacl IS DISTINCT FROM '{postgres=X/postgres}'::aclitem[]
       OR NOT p.prosecdef OR p.provolatile<>'v' OR p.proparallel<>'u'
       OR p.prokind<>'f' OR p.pronargs<>0
       OR p.proconfig IS DISTINCT FROM ARRAY['search_path=pg_catalog, pg_temp']::text[]
       OR p.definition_hash<>'0d54df28c7a16f17817dfce072b02103e8da0c87e8bd8d20d24b044a566de9ea'
       OR p.body_hash<>'65bc78cf1810d7fabccd50c8c68abdcc55b0029a6cafe88db2619a257c8a38a3' THEN
        RAISE EXCEPTION '042 capture trigger catalog drift; no replacement';
    END IF;
    SELECT oid,tgenabled,pg_get_triggerdef(oid) AS def INTO t FROM pg_trigger
       WHERE tgrelid='control.stitching_job'::regclass AND tgname='customer_identity_after_stitch';
    IF t.oid IS NULL OR t.tgenabled<>'O' OR t.def<>
       'CREATE TRIGGER customer_identity_after_stitch AFTER UPDATE OF status ON control.stitching_job FOR EACH ROW WHEN (((new.status)::text = ''SUCCEEDED''::text)) EXECUTE FUNCTION control.capture_customer_identity_after_stitch()' THEN
        RAISE EXCEPTION '042 capture trigger binding drift; no replacement';
    END IF;
    IF to_regclass('control.customer_identity_member_export') IS NULL
       OR to_regprocedure('control.capture_customer_identity_snapshot(uuid)') IS NULL THEN
        RAISE EXCEPTION '042 requires complete 041 bridge';
    END IF;
END $guard$;
CREATE OR REPLACE FUNCTION control.capture_customer_identity_after_stitch()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp AS $trigger$
DECLARE run_id uuid; reason text; lock_retry_deadline timestamptz;
BEGIN
    -- Connector exclusion, original success and durable outbox semantics unchanged.
    IF EXISTS(SELECT 1 FROM control.customer_identity_connector_lineage(ARRAY[NEW.ingestion_batch_id])) THEN RETURN NEW; END IF;
    SELECT r.run_id INTO run_id FROM identity.resolution_run r
        WHERE r.stitching_job_id=NEW.job_id AND r.status='SUCCEEDED';
    IF run_id IS NULL THEN
        INSERT INTO control.customer_identity_capture_issue(stitching_job_id,error_code)
            VALUES(NEW.job_id,'NO_SUCCESSFUL_RESOLUTION') ON CONFLICT DO NOTHING;
        RETURN NEW;
    END IF;
    PERFORM pg_advisory_xact_lock(7413220179::bigint);
    lock_retry_deadline := clock_timestamp() + interval '10 seconds';
    BEGIN
        LOOP
            BEGIN
                PERFORM control.capture_customer_identity_snapshot(NEW.job_id);
                EXIT;
            EXCEPTION WHEN lock_not_available THEN
                -- A failed NOWAIT (55P03) unwinds ALL relation locks acquired in
                -- this attempt, so ingestion can finish its reverse lock order.
                IF clock_timestamp() >= lock_retry_deadline THEN RAISE; END IF;
            END;
            PERFORM pg_sleep(0.1);
        END LOOP;
    EXCEPTION WHEN OTHERS THEN
        reason := CASE SQLSTATE
            WHEN '55P03' THEN 'CAPTURE_LOCK_CONTENDED'
            WHEN 'P2410' THEN 'INVALID_UPLOAD_BATCH_REF'
            WHEN 'P2411' THEN 'BATCH_LINEAGE_CONFLICT'
            WHEN 'P2412' THEN 'BLANK_SOURCE_USER'
            WHEN 'P2413' THEN 'DUPLICATE_APP_USER'
            WHEN 'P2414' THEN 'INCOMPLETE_MAPPING'
            WHEN 'P2415' THEN 'STALE_RESOLUTION'
            WHEN 'P2416' THEN 'SOURCE_IDENTITY_CONFLICT'
            WHEN 'P2417' THEN 'MIXED_OR_UNKNOWN_OWNER'
            ELSE 'CAPTURE_SQL_ERROR' END;
        INSERT INTO control.customer_identity_transfer
            (transfer_id,resolution_run_id,stitching_job_id,ingestion_batch_id,status,error_code,row_count)
            VALUES(run_id,run_id,NEW.job_id,NEW.ingestion_batch_id,'BLOCKED',reason,0)
            ON CONFLICT (stitching_job_id) DO NOTHING;
    END;
    RETURN NEW;
EXCEPTION WHEN OTHERS THEN
    BEGIN
        INSERT INTO control.customer_identity_capture_issue(stitching_job_id,error_code)
            VALUES(NEW.job_id,'CAPTURE_SQL_ERROR') ON CONFLICT DO NOTHING;
    EXCEPTION WHEN OTHERS THEN NULL;
    END;
    RETURN NEW;
END $trigger$;
COMMIT;
