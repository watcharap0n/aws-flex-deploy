-- Staging publisher read access to the immutable global membership bridge.
-- New 041 table is owner-only, while the existing publisher needs exact bridge
-- membership/provenance to verify each immutable export and prevent leakage.
-- Do not grant PUBLIC, INSERT/UPDATE/DELETE, or blanket schema access.
BEGIN;
SET LOCAL lock_timeout='5s';
DO $baseline$
BEGIN
    IF to_regclass('control.customer_identity_member_export') IS NULL
       OR NOT EXISTS (SELECT 1 FROM pg_class
            WHERE oid='control.customer_identity_member_export'::regclass
              AND relkind='r' AND pg_get_userbyid(relowner)='postgres')
       OR NOT EXISTS (SELECT 1 FROM pg_roles
            WHERE rolname='identity_publication_worker' AND rolcanlogin AND NOT rolsuper)
       OR NOT has_schema_privilege('identity_publication_worker','control','USAGE')
       OR NOT has_table_privilege('identity_publication_worker','control.customer_identity_export','SELECT')
       OR has_table_privilege('identity_publication_worker','control.customer_identity_member_export','INSERT,UPDATE,DELETE,TRUNCATE') THEN
       RAISE EXCEPTION '043 member bridge ACL baseline mismatch';
    END IF;
END $baseline$;
GRANT SELECT ON TABLE control.customer_identity_member_export TO identity_publication_worker;
COMMIT;
