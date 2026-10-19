-- Candidate: overlapping uploads must not make immutable selected evidence disappear.
-- Install manually after 041/042 on the exact deployed function catalog. This does
-- not retroactively recapture or unblock an already BLOCKED transfer. It does not
-- relax global matching, provenance, source-key or analytical integrity checks.
BEGIN;
SET LOCAL lock_timeout='5s';
DO $migration$
DECLARE p record; body text; old text; replacement text;
BEGIN
    SELECT oid,proowner,proacl,prosecdef,provolatile,proparallel,prokind,
           proconfig,pronargs,prosrc,encode(digest(prosrc,'sha256'),'hex') AS sha
      INTO p FROM pg_proc
     WHERE oid=to_regprocedure('control.capture_customer_identity_snapshot(uuid)');
    IF p.oid IS NULL OR p.proowner <> (SELECT oid FROM pg_roles WHERE rolname='postgres')
       OR p.proacl IS DISTINCT FROM '{postgres=X/postgres}'::aclitem[]
       OR p.prosecdef OR p.provolatile<>'v' OR p.proparallel<>'u'
       OR p.prokind<>'f' OR p.pronargs<>1
       OR p.proconfig IS DISTINCT FROM ARRAY['search_path=pg_catalog, pg_temp']::text[]
       OR p.sha NOT IN (
         'ab63ec0cc02a1eea2fde9b70a03e0a026bab66852dcdf7c28fc27cb043673a34',
         '342a9d9de34f06f81252bc1487f47a36a4fef96fb4ee5c7e9136d989bd9ca1bc') THEN
        RAISE EXCEPTION '044 capture catalog drift; no replacement';
    END IF;
    IF p.sha='342a9d9de34f06f81252bc1487f47a36a4fef96fb4ee5c7e9136d989bd9ca1bc' THEN RETURN; END IF;
    IF to_regclass('staging.customer_profile_history') IS NULL
       OR to_regclass('control.customer_identity_member_export') IS NULL
       OR NOT EXISTS (
           SELECT FROM pg_index WHERE indrelid='staging.customer_profile_history'::regclass
             AND indisunique AND pg_get_indexdef(indexrelid) LIKE
              'CREATE UNIQUE INDEX customer_profile_history_pkey ON staging.customer_profile_history USING btree (batch_id, raw_customer_id)%') THEN
        RAISE EXCEPTION '044 immutable profile history and bridge required';
    END IF;
    body:=p.prosrc;
    -- The lock-free function is safe only within the matcher's pinned MVCC
    -- transaction; an old READ COMMITTED worker must fail closed.
    old:='    SELECT * INTO STRICT j FROM control.stitching_job WHERE control.stitching_job.job_id=capture_customer_identity_snapshot.job_id;';
    replacement:='    IF current_setting(''transaction_isolation'') <> ''repeatable read'' THEN' || E'\n' ||
                 '        RAISE EXCEPTION USING ERRCODE=''P2418'',MESSAGE=''Identity snapshot isolation required'';' || E'\n' ||
                 '    END IF;' || E'\n' || old;
    IF length(body)-length(replace(body,old,''))<>length(old) THEN
        RAISE EXCEPTION '044 capture isolation baseline drift';
    END IF;
    body:=replace(body,old,replacement);
    old:='p.standardized_record_id,p.batch_id AS profile_batch,p.app_id AS profile_app,';
    replacement:='p.standardized_record_id,h.batch_id AS profile_batch,p.app_id AS profile_app,' || E'\n' ||
                 '               h.raw_customer_id AS history_raw,h.app_id AS history_app,' || E'\n' ||
                 '               h.source_system_id AS history_source,h.source_customer_id AS history_user,';
    IF length(body)-length(replace(body,old,''))<>length(old) THEN
        RAISE EXCEPTION '044 selected projection drift';
    END IF;
    body:=replace(body,old,replacement);
    old:='LEFT JOIN staging.customer_profile p ON p.raw_customer_id=b.raw_customer_id' || E'\n' ||
         '        LEFT JOIN identity.customer_entity_member m ON m.standardized_record_id=p.standardized_record_id';
    replacement:='LEFT JOIN staging.customer_profile_history h ON h.batch_id=b.ingestion_batch_id' || E'\n' ||
                 '            AND h.raw_customer_id=b.raw_customer_id' || E'\n' ||
                 '        LEFT JOIN staging.customer_profile p ON p.raw_customer_id=b.raw_customer_id' || E'\n' ||
                 '        LEFT JOIN identity.customer_entity_member m ON m.standardized_record_id=p.standardized_record_id';
    IF length(body)-length(replace(body,old,''))<>length(old) THEN
        RAISE EXCEPTION '044 selected history join drift';
    END IF;
    body:=replace(body,old,replacement);
    old:='OR EXISTS(SELECT 1 FROM selected WHERE profile_batch IS DISTINCT FROM j.ingestion_batch_id' || E'\n' ||
         '                 OR profile_app IS DISTINCT FROM owner_scope.app_id)';
    replacement:='OR EXISTS(SELECT 1 FROM selected WHERE history_raw IS NULL' || E'\n' ||
                 '                  OR profile_batch IS DISTINCT FROM j.ingestion_batch_id' || E'\n' ||
                 '                  OR history_app IS DISTINCT FROM owner_scope.app_id' || E'\n' ||
                 '                  OR history_source IS DISTINCT FROM source_system_id' || E'\n' ||
                 '                  OR history_user IS DISTINCT FROM source_customer_id' || E'\n' ||
                 '                  OR profile_app IS DISTINCT FROM owner_scope.app_id)';
    IF length(body)-length(replace(body,old,''))<>length(old) THEN
        RAISE EXCEPTION '044 source evidence guard drift';
    END IF;
    body:=replace(body,old,replacement);
    -- One app can receive the same literal user ID from independent sources.
    -- Preserve every source member in the bridge but emit one public app/user
    -- profile. Two different resolved people for that key remain ambiguous.
    old:='    INSERT INTO control.customer_identity_export(transfer_id,row_number,app_id,user_id,payload)';
    replacement:='    IF EXISTS(SELECT 1 FROM control.customer_identity_member_export' || E'\n' ||
                 '        WHERE transfer_id=run_id AND provenance_status=''VERIFIED_UPLOAD''' || E'\n' ||
                 '        GROUP BY app_id,source_customer_id HAVING count(DISTINCT customer_entity_id)>1) THEN' || E'\n' ||
                 '        RAISE EXCEPTION USING ERRCODE=''P2413'',MESSAGE=''Ambiguous app user identity'';' || E'\n' ||
                 '    END IF;' || E'\n' || old;
    IF length(body)-length(replace(body,old,''))<>length(old) THEN
        RAISE EXCEPTION '044 export insertion baseline drift';
    END IF;
    body:=replace(body,old,replacement);
    old:='    FROM control.customer_identity_member_export bridge' || E'\n' ||
         '    JOIN staging.customer_profile p ON p.standardized_record_id=bridge.standardized_record_id';
    replacement:='    FROM (SELECT DISTINCT ON (app_id,source_customer_id) *' || E'\n' ||
                 '          FROM control.customer_identity_member_export' || E'\n' ||
                 '          WHERE transfer_id=run_id AND provenance_status=''VERIFIED_UPLOAD''' || E'\n' ||
                 '          ORDER BY app_id,source_customer_id,' || E'\n' ||
                 '              (batch_id=j.ingestion_batch_id) DESC,source_system_id,standardized_record_id) bridge' || E'\n' ||
                 '    JOIN staging.customer_profile p ON p.standardized_record_id=bridge.standardized_record_id';
    IF length(body)-length(replace(body,old,''))<>length(old) THEN
        RAISE EXCEPTION '044 export winner baseline drift';
    END IF;
    body:=replace(body,old,replacement);
    old:='    IF selected_count <> (SELECT count(*) FROM control.customer_identity_member_export' || E'\n' ||
         '          WHERE transfer_id=run_id AND provenance_status=''VERIFIED_UPLOAD'')';
    replacement:='    IF selected_count <> (SELECT count(*) FROM (' || E'\n' ||
                 '          SELECT DISTINCT app_id,source_customer_id FROM control.customer_identity_member_export' || E'\n' ||
                 '          WHERE transfer_id=run_id AND provenance_status=''VERIFIED_UPLOAD'') distinct_keys)';
    IF length(body)-length(replace(body,old,''))<>length(old) THEN
        RAISE EXCEPTION '044 export cardinality baseline drift';
    END IF;
    body:=replace(body,old,replacement);
    old:='    LOCK TABLE raw.customer_batch_member, raw.customer, raw.customer_current, staging.customer_profile,' || E'\n' ||
         '        control.customer_identity_upload_scope, control.ingestion_batch,' || E'\n' ||
         '        control.source_landing_batch, control.source_system IN SHARE MODE NOWAIT;' || E'\n' ||
         '    IF to_regclass(''connector.sync_run'') IS NOT NULL THEN' || E'\n' ||
         '        EXECUTE ''LOCK TABLE connector.sync_run IN SHARE MODE NOWAIT'';' || E'\n' ||
         '    END IF;';
    IF length(body)-length(replace(body,old,''))<>length(old) THEN
        RAISE EXCEPTION '044 capture lock baseline drift';
    END IF;
    body:=replace(body,old, '    -- Immutable MVCC source snapshot; no table SHARE lock against ingestion.');
    old:='    -- Recheck after locking provenance against concurrent connector registration.' || E'\n' ||
         '    IF EXISTS(SELECT 1 FROM control.customer_identity_connector_lineage(ARRAY[j.ingestion_batch_id])) THEN RETURN; END IF;';
    IF length(body)-length(replace(body,old,''))<>length(old) THEN
        RAISE EXCEPTION '044 connector lineage baseline drift';
    END IF;
    body:=replace(body,old,'    -- Connector lineage remains checked in this pinned snapshot.');
    -- All other guards remain in the exact pinned baseline, including global
    -- membership run, expanded contributors and current/source tuple checks.
    EXECUTE 'CREATE OR REPLACE FUNCTION control.capture_customer_identity_snapshot(job_id uuid) ' ||
            'RETURNS void LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $capture_044$' ||
            body || '$capture_044$';
    IF (SELECT encode(digest(prosrc,'sha256'),'hex') FROM pg_proc
        WHERE oid=p.oid) <> '342a9d9de34f06f81252bc1487f47a36a4fef96fb4ee5c7e9136d989bd9ca1bc' THEN
        RAISE EXCEPTION '044 installed body does not match candidate: %', (SELECT encode(digest(prosrc,'sha256'),'hex') FROM pg_proc WHERE oid=p.oid);
    END IF;
END $migration$;
-- The base 042 trigger maps all capture failures to an immutable transfer.
-- Retain this on incorrect worker isolation; never allow an export to proceed.
DO $trigger_guard$
DECLARE p record; t record; body text; old text;
BEGIN
    SELECT oid,proowner,proacl,prosecdef,provolatile,proparallel,prokind,
           proconfig,pronargs,prosrc,encode(digest(prosrc,'sha256'),'hex') AS sha
      INTO p FROM pg_proc WHERE oid=to_regprocedure('control.capture_customer_identity_after_stitch()');
    SELECT oid,tgenabled,pg_get_triggerdef(oid) AS def INTO t FROM pg_trigger
      WHERE tgrelid='control.stitching_job'::regclass AND tgname='customer_identity_after_stitch';
    IF p.oid IS NULL OR p.proowner <> (SELECT oid FROM pg_roles WHERE rolname='postgres')
       OR p.proacl IS DISTINCT FROM '{postgres=X/postgres}'::aclitem[]
       OR NOT p.prosecdef OR p.provolatile<>'v' OR p.proparallel<>'u'
       OR p.prokind<>'f' OR p.pronargs<>0
       OR p.proconfig IS DISTINCT FROM ARRAY['search_path=pg_catalog, pg_temp']::text[]
       OR p.sha NOT IN (
         '0631dd91f6c740c89c8be0cef8c94ec5350837d7cfd042ec7ae58246a7ac9a4b',
         'a7d41671ec8a5900897e212e0e1e4f6509af3eef7765496cd6e61f548d4d6790')
       OR t.oid IS NULL OR t.tgenabled<>'O' OR t.def <>
         'CREATE TRIGGER customer_identity_after_stitch AFTER UPDATE OF status ON control.stitching_job FOR EACH ROW WHEN (((new.status)::text = ''SUCCEEDED''::text)) EXECUTE FUNCTION control.capture_customer_identity_after_stitch()' THEN
        RAISE EXCEPTION '044 trigger catalog drift; no replacement';
    END IF;
    IF p.sha='a7d41671ec8a5900897e212e0e1e4f6509af3eef7765496cd6e61f548d4d6790' THEN RETURN; END IF;
    body:=p.prosrc;
    old:='            WHEN ''P2417'' THEN ''MIXED_OR_UNKNOWN_OWNER''';
    IF length(body)-length(replace(body,old,''))<>length(old) THEN
        RAISE EXCEPTION '044 trigger code mapping drift';
    END IF;
    body:=replace(body,old,old || E'\n' ||
                  '            WHEN ''P2418'' THEN ''SNAPSHOT_ISOLATION_REQUIRED''');
    old:='    EXCEPTION WHEN OTHERS THEN' || E'\n' || '        reason := CASE SQLSTATE';
    IF length(body)-length(replace(body,old,''))<>length(old) THEN
        RAISE EXCEPTION '044 inner trigger exception baseline drift';
    END IF;
    body:=replace(body,old, '    EXCEPTION WHEN SQLSTATE ''40001'' THEN RAISE;' || E'\n' ||
                             '              WHEN OTHERS THEN' || E'\n' || '        reason := CASE SQLSTATE');
    old:='EXCEPTION WHEN OTHERS THEN' || E'\n' || '    BEGIN';
    IF length(body)-length(replace(body,old,''))<>length(old) THEN
        RAISE EXCEPTION '044 outer trigger exception baseline drift';
    END IF;
    body:=replace(body,old, 'EXCEPTION WHEN SQLSTATE ''40001'' THEN RAISE;' || E'\n' ||
                             '               WHEN OTHERS THEN' || E'\n' || '    BEGIN');
    EXECUTE 'CREATE OR REPLACE FUNCTION control.capture_customer_identity_after_stitch() '
      || 'RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, pg_temp '
      || 'AS $trigger_044$' || body || '$trigger_044$';
    IF (SELECT encode(digest(prosrc,'sha256'),'hex') FROM pg_proc WHERE oid=p.oid)
       <> 'a7d41671ec8a5900897e212e0e1e4f6509af3eef7765496cd6e61f548d4d6790' THEN
        RAISE EXCEPTION '044 installed trigger body mismatch: %',
          (SELECT encode(digest(prosrc,'sha256'),'hex') FROM pg_proc WHERE oid=p.oid);
    END IF;
END $trigger_guard$;
COMMIT;
