-- 051: switch the 041/044/045 connector-aware capture to 050's ACTIVE
-- app/source/user profile and member OIDs. Preserve verified connector provenance,
-- source scope, immutable evidence and snapshot isolation; never replay transfers.
-- The 038 stitch admission gate is owned by 052; this migration does not lift it.
BEGIN;
SET LOCAL lock_timeout='5s';
DO $migration$
DECLARE p record; body text; needle text; replacement text;
BEGIN
    SELECT oid,proowner,proacl,prosecdef,provolatile,proparallel,prokind,
           proconfig,pronargs,prosrc,encode(digest(prosrc,'sha256'),'hex') sha INTO p
    FROM pg_proc WHERE oid=to_regprocedure('control.capture_customer_identity_snapshot(uuid)');
    IF p.oid IS NULL OR p.proowner<>(SELECT oid FROM pg_roles WHERE rolname='postgres')
       OR p.proacl IS DISTINCT FROM '{postgres=X/postgres}'::aclitem[]
       OR p.prosecdef OR p.provolatile<>'v' OR p.proparallel<>'u' OR p.prokind<>'f'
       OR p.pronargs<>1 OR p.proconfig IS DISTINCT FROM ARRAY['search_path=pg_catalog, pg_temp']::text[]
       OR p.sha<>'678feae1d3d8dba0637b9e24b1fc018bbaf0e3b2a3fcb4c16dbf4bccc05ca4bd'
       OR to_regprocedure('control.customer_identity_source_scope(uuid)') IS NULL
       OR to_regprocedure('control.connector_batch_app_scope(uuid)') IS NULL
       OR to_regclass('staging.customer_profile_member_tuple_key') IS NULL
       OR to_regclass('identity.customer_entity_member_app_source_key') IS NULL
       OR to_regclass('staging.customer_profile_history') IS NULL
       OR to_regclass('control.customer_identity_member_export') IS NULL THEN
        RAISE EXCEPTION '051 capture/050 catalog drift; no replacement';
    END IF;
    body:=p.prosrc;
    -- Every replacement is exact and singular; otherwise abort the whole DDL.
    needle:='LEFT JOIN staging.customer_profile p ON p.raw_customer_id=b.raw_customer_id';
    replacement:='LEFT JOIN staging.customer_profile p ON (p.app_id,p.source_system_id,p.source_customer_id)=' ||
                 ' (h.app_id,b.source_system_id,b.source_customer_id)';
    IF length(body)-length(replace(body,needle,''))<>length(needle) THEN RAISE EXCEPTION '051 selected join drift'; END IF;
    body:=replace(body,needle,replacement);
    needle:='LEFT JOIN identity.customer_entity_member m ON m.standardized_record_id=p.standardized_record_id';
    replacement:='LEFT JOIN identity.customer_entity_member m ON m.standardized_record_id=p.standardized_record_id' || E'\n' ||
                 '            AND (m.app_id,m.source_system_id,m.source_customer_id)=' ||
                 '(p.app_id,p.source_system_id,p.source_customer_id)';
    IF length(body)-length(replace(body,needle,''))<>length(needle) THEN RAISE EXCEPTION '051 selected member drift'; END IF;
    body:=replace(body,needle,replacement);
    needle:='OR profile_batch IS DISTINCT FROM j.ingestion_batch_id';
    replacement:='OR profile_batch IS DISTINCT FROM j.ingestion_batch_id' || E'\n' ||
                 '                  OR standardized_record_id IS NULL';
    IF length(body)-length(replace(body,needle,''))<>length(needle) THEN RAISE EXCEPTION '051 selected lineage drift'; END IF;
    body:=replace(body,needle,replacement);
    needle:='SELECT m.raw_customer_id,b.ingestion_batch_id,b.source_system_id,b.source_customer_id,' || E'\n' ||
            '               b.source_system_id IS DISTINCT FROM m.source_system_id' || E'\n' ||
            '                 OR b.source_customer_id IS DISTINCT FROM m.source_customer_id AS identity_conflict' || E'\n' ||
            '        FROM members m LEFT JOIN raw.customer_batch_member b USING(raw_customer_id)';
    replacement:='SELECT m.raw_customer_id,b.ingestion_batch_id,b.source_system_id,b.source_customer_id,' || E'\n' ||
                 '               b.source_system_id IS DISTINCT FROM m.source_system_id' || E'\n' ||
                 '                 OR b.source_customer_id IS DISTINCT FROM m.source_customer_id AS identity_conflict' || E'\n' ||
                 '        FROM members m LEFT JOIN raw.customer_batch_member b' || E'\n' ||
                 '          ON (b.raw_customer_id,b.source_system_id,b.source_customer_id)=' ||
                 '(m.raw_customer_id,m.source_system_id,m.source_customer_id)' || E'\n' ||
                 '         AND EXISTS (SELECT FROM control.ingestion_batch ib WHERE ib.batch_id=b.ingestion_batch_id' ||
                 ' AND ib.app_id IS NOT DISTINCT FROM m.profile_app)';
    IF length(body)-length(replace(body,needle,''))<>length(needle) THEN RAISE EXCEPTION '051 member evidence drift'; END IF;
    body:=replace(body,needle,replacement);
    needle:='OR EXISTS(SELECT 1 FROM selected s' || E'\n' ||
            '              JOIN raw.customer_batch_member prior ON prior.raw_customer_id=s.raw_customer_id' || E'\n' ||
            '              JOIN control.ingestion_batch prior_batch ON prior_batch.batch_id=prior.ingestion_batch_id' || E'\n' ||
            '              CROSS JOIN LATERAL control.customer_identity_source_scope(prior.ingestion_batch_id) prior_scope' || E'\n' ||
            '              WHERE prior.ingestion_batch_id<>j.ingestion_batch_id' || E'\n' ||
            '                 AND (prior_batch.app_id IS DISTINCT FROM owner_scope.app_id' || E'\n' ||
            '                      OR prior_scope.app_id IS DISTINCT FROM owner_scope.app_id' || E'\n' ||
            '                      OR prior_scope.error_code IS NOT NULL))';
    IF length(body)-length(replace(body,needle,''))<>length(needle) THEN RAISE EXCEPTION '051 raw alias guard drift'; END IF;
    body:=replace(body,needle,'');
    needle:='LEFT JOIN raw.customer_current c ON c.raw_customer_id=s.raw_customer_id' || E'\n' ||
            '              WHERE h.raw_customer_id IS NULL OR h.app_id IS DISTINCT FROM owner_scope.app_id' || E'\n' ||
            '                 OR c.source_system_id IS DISTINCT FROM s.source_system_id' || E'\n' ||
            '                 OR c.source_customer_id IS DISTINCT FROM s.source_customer_id' || E'\n' ||
            '                 OR c.app_id IS DISTINCT FROM owner_scope.app_id)';
    replacement:='WHERE h.raw_customer_id IS NULL OR h.app_id IS DISTINCT FROM owner_scope.app_id)';
    IF length(body)-length(replace(body,needle,''))<>length(needle) THEN RAISE EXCEPTION '051 selected observation drift'; END IF;
    body:=replace(body,needle,replacement);
    needle:='OR EXISTS(SELECT 1 FROM evidence e JOIN owners o USING(ingestion_batch_id)' || E'\n' ||
            '              WHERE e.raw_customer_id IS NOT NULL AND o.error_code IS NULL' || E'\n' ||
            '              GROUP BY e.raw_customer_id HAVING count(DISTINCT o.app_id)>1)';
    IF length(body)-length(replace(body,needle,''))<>length(needle) THEN RAISE EXCEPTION '051 alias ownership drift'; END IF;
    body:=replace(body,needle,'');
    needle:='SELECT 1 FROM raw.customer_batch_member b WHERE b.raw_customer_id=m.raw_customer_id' || E'\n' ||
            '                AND b.ingestion_batch_id=m.batch_id';
    replacement:='SELECT 1 FROM raw.customer_batch_member b WHERE' || E'\n' ||
                 '                (b.raw_customer_id,b.ingestion_batch_id,b.source_system_id,b.source_customer_id)=' ||
                 '(m.raw_customer_id,m.batch_id,m.source_system_id,m.source_customer_id)';
    IF length(body)-length(replace(body,needle,''))<>length(needle) THEN RAISE EXCEPTION '051 member lineage drift'; END IF;
    body:=replace(body,needle,replacement);
    needle:='OR profile_user IS DISTINCT FROM source_customer_id OR profile_source IS DISTINCT FROM source_system_id)';
    replacement:='OR profile_user IS DISTINCT FROM source_customer_id OR profile_source IS DISTINCT FROM source_system_id' ||
                 ' OR profile_app IS DISTINCT FROM app_id)';
    IF length(body)-length(replace(body,needle,''))<>length(needle) THEN RAISE EXCEPTION '051 member app drift'; END IF;
    body:=replace(body,needle,replacement);
    -- Selected entity lookup must not use the non-unique raw provenance key.
    needle:='JOIN staging.customer_profile p USING(raw_customer_id)';
    replacement:='JOIN staging.customer_profile p ON (p.app_id,p.source_system_id,p.source_customer_id)=' ||
                 '((SELECT app_id FROM control.ingestion_batch WHERE batch_id=b.ingestion_batch_id),' ||
                 'b.source_system_id,b.source_customer_id)';
    IF (length(body)-length(replace(body,needle,'')))/length(needle)<>2 THEN RAISE EXCEPTION '051 component join drift'; END IF;
    body:=replace(body,needle,replacement);
    needle:='JOIN staging.customer_profile p ON p.standardized_record_id=bridge.standardized_record_id';
    replacement:='JOIN staging.customer_profile p ON p.standardized_record_id=bridge.standardized_record_id' || E'\n' ||
                 '      AND (p.app_id,p.source_system_id,p.source_customer_id)=' ||
                 '(bridge.app_id,bridge.source_system_id,bridge.source_customer_id)';
    IF length(body)-length(replace(body,needle,''))<>length(needle) THEN RAISE EXCEPTION '051 public export join drift'; END IF;
    body:=replace(body,needle,replacement);
    needle:='AND p.raw_customer_id=bridge.raw_customer_id AND p.batch_id=bridge.batch_id';
    IF length(body)-length(replace(body,needle,''))<>length(needle) THEN RAISE EXCEPTION '051 bridge provenance drift'; END IF;
    -- 045's VERIFIED_CONNECTOR/VERIFIED_UPLOAD branches, workspace payload and
    -- immutable 041/044 gates are retained verbatim in the pinned source body.
    EXECUTE 'CREATE OR REPLACE FUNCTION control.capture_customer_identity_snapshot(job_id uuid) ' ||
        'RETURNS void LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $capture_051$' || body || '$capture_051$';
    IF (SELECT prosrc FROM pg_proc WHERE oid=p.oid) IS DISTINCT FROM body THEN
        RAISE EXCEPTION '051 body installation mismatch';
    END IF;
END $migration$;
COMMIT;
