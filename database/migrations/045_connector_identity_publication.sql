-- Add app-bound connector batches to the existing immutable identity publication lane.
-- Pin the installed 044/041 catalog; never reinterpret an app-less legacy sync.
BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='120s';

CREATE FUNCTION control.customer_identity_source_scope(p_batch uuid)
RETURNS TABLE(app_id text,error_code text)
LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $scope$
BEGIN
    IF EXISTS (SELECT 1 FROM control.customer_identity_connector_lineage(ARRAY[p_batch])) THEN
        RETURN QUERY SELECT s.app_id,s.error_code FROM control.connector_batch_app_scope(p_batch) s;
    ELSE
        RETURN QUERY SELECT s.app_id,s.error_code FROM control.customer_app_scope_status(p_batch) s;
    END IF;
END $scope$;
REVOKE ALL ON FUNCTION control.customer_identity_source_scope(uuid) FROM PUBLIC;

DO $migration$
DECLARE p record; body text; old text; replacement text; definition text;
BEGIN
    IF to_regprocedure('control.connector_batch_app_scope(uuid)') IS NULL
       OR to_regclass('control.customer_identity_member_export') IS NULL THEN
        RAISE EXCEPTION 'Connector scope and existing publication bridge required';
    END IF;
    SELECT prosrc,encode(digest(prosrc,'sha256'),'hex') AS sha INTO p
    FROM pg_proc WHERE oid='control.resolve_customer_identity_batches(uuid[])'::regprocedure;
    IF p.sha <> '242b4652e80bc9407257fdfcd31f1e7c044d0e0b0da3e78f58258dbe8672119c' THEN
        RAISE EXCEPTION 'Identity resolver catalog drift';
    END IF;
    body:=p.prosrc;
    old:='        SELECT wanted.batch_id,b.batch_ref,l.batch_ref AS landing_ref,';
    replacement:='        SELECT wanted.batch_id,b.batch_ref,l.batch_ref AS landing_ref,b.app_id AS stored_app,';
    IF length(body)-length(replace(body,old,''))<>length(old) THEN RAISE EXCEPTION 'resolver batch projection drift'; END IF;
    body:=replace(body,old,replacement);
    old:='    SELECT b.batch_id,r.workspace_id,r.app_id,';
    replacement:='    SELECT b.batch_id,CASE WHEN c.ingestion_batch_id IS NOT NULL THEN cs.workspace_id ELSE r.workspace_id END,' || E'\n'
      || '           CASE WHEN c.ingestion_batch_id IS NOT NULL THEN verified.app_id ELSE r.app_id END,';
    IF length(body)-length(replace(body,old,''))<>length(old) THEN RAISE EXCEPTION 'resolver owner projection drift'; END IF;
    body:=replace(body,old,replacement);
    old:='CASE WHEN c.ingestion_batch_id IS NOT NULL THEN ''CONNECTOR_EXCLUDED''';
    replacement:='CASE WHEN c.ingestion_batch_id IS NOT NULL THEN CASE WHEN verified.error_code IS NOT NULL '
      || 'THEN verified.error_code WHEN verified.app_id IS NULL OR cs.workspace_id IS NULL '
      || 'OR b.stored_app IS DISTINCT FROM verified.app_id THEN ''CONNECTOR_LINEAGE_CONFLICT'' END';
    IF length(body)-length(replace(body,old,''))<>length(old) THEN RAISE EXCEPTION 'resolver connector gate drift'; END IF;
    body:=replace(body,old,replacement);
    old:='    FROM batches b LEFT JOIN connectors c ON c.ingestion_batch_id=b.batch_id';
    replacement:=old || E'\n' ||
      '    LEFT JOIN connector.sync_run cs ON c.ingestion_batch_id IS NOT NULL AND cs.ingestion_batch_id=b.batch_id' || E'\n' ||
      '    LEFT JOIN LATERAL control.connector_batch_app_scope(b.batch_id) verified ON c.ingestion_batch_id IS NOT NULL';
    IF length(body)-length(replace(body,old,''))<>length(old) THEN RAISE EXCEPTION 'resolver connector join drift'; END IF;
    body:=replace(body,old,replacement);
    EXECUTE 'CREATE OR REPLACE FUNCTION control.resolve_customer_identity_batches(batch_ids uuid[]) '
      || 'RETURNS TABLE (ingestion_batch_id uuid,workspace_id uuid,app_id text,error_code text) '
      || 'LANGUAGE sql VOLATILE SET search_path=pg_catalog,pg_temp AS $resolver_045$'
      || body || '$resolver_045$';

    SELECT prosrc,encode(digest(prosrc,'sha256'),'hex') AS sha INTO p
    FROM pg_proc WHERE oid='control.capture_customer_identity_snapshot(uuid)'::regprocedure;
    IF p.sha <> '342a9d9de34f06f81252bc1487f47a36a4fef96fb4ee5c7e9136d989bd9ca1bc' THEN
        RAISE EXCEPTION 'Identity capture catalog drift';
    END IF;
    body:=p.prosrc;
    old:='    IF EXISTS(SELECT 1 FROM control.customer_identity_connector_lineage(ARRAY[j.ingestion_batch_id])) THEN RETURN; END IF;';
    IF length(body)-length(replace(body,old,''))<>length(old) THEN RAISE EXCEPTION 'capture connector skip drift'; END IF;
    body:=replace(body,old,'    -- Connector is eligible only via the verified resolver and exact source guards.');
    old:='CROSS JOIN LATERAL control.customer_app_scope_status(prior.ingestion_batch_id) prior_scope';
    IF length(body)-length(replace(body,old,''))<>length(old) THEN RAISE EXCEPTION 'capture prior scope drift'; END IF;
    body:=replace(body,old,'CROSS JOIN LATERAL control.customer_identity_source_scope(prior.ingestion_batch_id) prior_scope');
    old:='EXISTS(SELECT 1 FROM control.customer_app_scope_status(e.ingestion_batch_id) s';
    IF length(body)-length(replace(body,old,''))<>length(old) THEN RAISE EXCEPTION 'capture evidence scope drift'; END IF;
    body:=replace(body,old,'EXISTS(SELECT 1 FROM control.customer_identity_source_scope(e.ingestion_batch_id) s');
    old:='SELECT * FROM control.resolve_customer_identity_batches(' || E'\n' || '           ARRAY(SELECT DISTINCT batch_id FROM affected))';
    replacement:='SELECT r.*,CASE WHEN EXISTS(SELECT 1 FROM control.customer_identity_connector_lineage(ARRAY[r.ingestion_batch_id]))' || E'\n'
      || '            THEN ''VERIFIED_CONNECTOR'' ELSE ''VERIFIED_UPLOAD'' END AS provenance_status' || E'\n'
      || '        FROM control.resolve_customer_identity_batches(' || E'\n' || '           ARRAY(SELECT DISTINCT batch_id FROM affected)) r';
    IF length(body)-length(replace(body,old,''))<>length(old) THEN RAISE EXCEPTION 'capture verified owners drift'; END IF;
    body:=replace(body,old,replacement);
    old:='CASE WHEN o.error_code IS NULL AND o.app_id=a.app_id THEN ''VERIFIED_UPLOAD'' ELSE ''UNATTRIBUTED'' END';
    replacement:='CASE WHEN o.error_code IS NULL AND o.app_id=a.app_id THEN o.provenance_status ELSE ''UNATTRIBUTED'' END';
    IF length(body)-length(replace(body,old,''))<>length(old) THEN RAISE EXCEPTION 'capture provenance drift'; END IF;
    body:=replace(body,old,replacement);
    old:='provenance_status=''VERIFIED_UPLOAD''';
    IF length(body)-length(replace(body,old,''))<>4*length(old) THEN RAISE EXCEPTION 'capture export filters drift'; END IF;
    body:=replace(body,old,'provenance_status IN (''VERIFIED_UPLOAD'',''VERIFIED_CONNECTOR'')');
    old:='JOIN control.customer_identity_upload_scope o ON o.app_id=bridge.app_id';
    IF length(body)-length(replace(body,old,''))<>length(old) THEN RAISE EXCEPTION 'capture upload-only export join drift'; END IF;
    body:=replace(body,old,'-- Source workspace already pinned by verified immutable member_export.');
    -- Only the final export SELECT references this alias in the pinned body.
    old:='''resolution_run_id'',run_id,''workspace_id'',o.workspace_id,';
    IF length(body)-length(replace(body,old,''))<>length(old) THEN RAISE EXCEPTION 'capture workspace payload drift'; END IF;
    body:=replace(body,old,'''resolution_run_id'',run_id,''workspace_id'',bridge.workspace_id,');
    old:='''app_id'',o.app_id,''user_id'',p.source_customer_id,';
    IF length(body)-length(replace(body,old,''))<>length(old) THEN RAISE EXCEPTION 'capture app payload drift'; END IF;
    body:=replace(body,old,'''app_id'',bridge.app_id,''user_id'',p.source_customer_id,');
    old:='SELECT run_id,row_number() OVER (ORDER BY o.app_id COLLATE "C",p.source_customer_id COLLATE "C"),' || E'\n'
         || '        o.app_id,p.source_customer_id,';
    replacement:='SELECT run_id,row_number() OVER (ORDER BY bridge.app_id COLLATE "C",p.source_customer_id COLLATE "C"),' || E'\n'
         || '        bridge.app_id,p.source_customer_id,';
    IF length(body)-length(replace(body,old,''))<>length(old) THEN RAISE EXCEPTION 'capture export key drift'; END IF;
    body:=replace(body,old,replacement);
    EXECUTE 'CREATE OR REPLACE FUNCTION control.capture_customer_identity_snapshot(job_id uuid) '
      || 'RETURNS void LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $capture_045$'
      || body || '$capture_045$';

    SELECT prosrc,encode(digest(prosrc,'sha256'),'hex') AS sha INTO p
    FROM pg_proc WHERE oid='control.capture_customer_identity_after_stitch()'::regprocedure;
    IF p.sha <> 'a7d41671ec8a5900897e212e0e1e4f6509af3eef7765496cd6e61f548d4d6790' THEN
        RAISE EXCEPTION 'Identity trigger catalog drift';
    END IF;
    body:=p.prosrc;
    old:='    IF EXISTS(SELECT 1 FROM control.customer_identity_connector_lineage(ARRAY[NEW.ingestion_batch_id])) THEN RETURN NEW; END IF;';
    IF length(body)-length(replace(body,old,''))<>length(old) THEN RAISE EXCEPTION 'trigger connector skip drift'; END IF;
    body:=replace(body,old,'    -- Both upload and app-bound connector capture under the same resolution fence.');
    EXECUTE 'CREATE OR REPLACE FUNCTION control.capture_customer_identity_after_stitch() '
      || 'RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $trigger_045$'
      || body || '$trigger_045$';

    SELECT pg_get_constraintdef(oid) INTO definition FROM pg_constraint
    WHERE conrelid='control.customer_identity_member_export'::regclass
      AND conname='customer_identity_member_export_provenance_status_check';
    IF definition <> 'CHECK ((provenance_status = ANY (ARRAY[''VERIFIED_UPLOAD''::text, ''UNATTRIBUTED''::text])))' THEN
       RAISE EXCEPTION 'Member provenance check drift';
    END IF;
    ALTER TABLE control.customer_identity_member_export DROP CONSTRAINT customer_identity_member_export_provenance_status_check;
    ALTER TABLE control.customer_identity_member_export ADD CONSTRAINT customer_identity_member_export_provenance_status_check
       CHECK (provenance_status IN ('VERIFIED_UPLOAD','VERIFIED_CONNECTOR','UNATTRIBUTED'));
    ALTER TABLE control.customer_identity_member_export DROP CONSTRAINT customer_identity_member_export_check;
    ALTER TABLE control.customer_identity_member_export ADD CONSTRAINT customer_identity_member_export_check
       CHECK ((provenance_status IN ('VERIFIED_UPLOAD','VERIFIED_CONNECTOR') AND app_id IS NOT NULL AND workspace_id IS NOT NULL)
           OR (provenance_status='UNATTRIBUTED' AND app_id IS NULL AND workspace_id IS NULL));
END $migration$;
COMMIT;
