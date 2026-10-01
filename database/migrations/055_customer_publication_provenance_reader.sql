-- The publisher verifies each immutable upload observation before exporting
-- identity membership. Restrict its role to only the relations in that proof.
BEGIN;
SET LOCAL lock_timeout = '5s';
DO $baseline$
BEGIN
    IF current_database() <> 'postgres'
       OR pg_is_in_recovery()
       OR current_user <> 'postgres'
       OR NOT EXISTS (
           SELECT 1 FROM pg_roles
           WHERE rolname = 'identity_publication_worker'
             AND rolcanlogin AND NOT rolsuper AND NOT rolbypassrls)
       OR NOT has_schema_privilege('identity_publication_worker', 'control', 'USAGE')
       OR NOT has_table_privilege('identity_publication_worker',
                                  'control.customer_identity_member_export', 'SELECT')
       OR EXISTS (
           SELECT 1 FROM (VALUES
               ('raw.customer_batch_member'), ('raw.customer'),
               ('staging.customer_profile_history'), ('control.ingestion_batch')
           ) AS v(name)
           WHERE to_regclass(v.name) IS NULL
              OR (SELECT pg_get_userbyid(relowner) <> 'postgres'
                  FROM pg_class WHERE oid = to_regclass(v.name))
              OR has_table_privilege('identity_publication_worker', v.name,
                                     'INSERT,UPDATE,DELETE,TRUNCATE')
       ) THEN
        RAISE EXCEPTION '055 publisher provenance reader ACL baseline mismatch';
    END IF;
END $baseline$;
GRANT USAGE ON SCHEMA raw, staging TO identity_publication_worker;
GRANT SELECT ON TABLE raw.customer_batch_member, raw.customer,
    staging.customer_profile_history, control.ingestion_batch
    TO identity_publication_worker;
COMMIT;
