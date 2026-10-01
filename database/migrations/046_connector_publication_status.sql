-- Expose verified connector transfer state to the existing connector API projection.
-- Preserve the view OID, grants, column order and upload-only continuation branch.
BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='120s';
DO $status$
DECLARE body text; old text; checksum text;
BEGIN
    SELECT pg_get_viewdef('control.customer_publication_status'::regclass,true) INTO body;
    SELECT encode(digest(body,'sha256'),'hex') INTO checksum;
    IF checksum <> '3ba0daec52c6ee2b1ec7ae49bbb96538f91e83c097b1a29ee93349f9227097f5' THEN
        RAISE EXCEPTION 'publication status catalog drift';
    END IF;
    old:='WHERE NOT control.customer_publication_is_connector(ib.batch_id)';
    IF length(body)-length(replace(body,old,''))<>length(old) THEN
        RAISE EXCEPTION 'connector status exclusion drift';
    END IF;
    body:=replace(body,old,'WHERE true');
    EXECUTE 'CREATE OR REPLACE VIEW control.customer_publication_status AS '||body;
END $status$;
COMMENT ON VIEW control.customer_publication_status IS
    'Latest effective identity publication for upload and app-bound connector batches; scoped upload continuation remains separate.';
COMMIT;
