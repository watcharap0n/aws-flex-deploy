-- Intake v2: status of each projection output of an uploaded dataset file
-- (batch x target_entity). CUSTOMER outputs are handed to the existing customer landing route;
-- TRANSACTION / EVENT / ASSET / RELATIONSHIP are loaded into ClickHouse cdp_intake.*.
-- Additive. Rollback: DROP TABLE control.intake_batch_output.

CREATE TABLE IF NOT EXISTS control.intake_batch_output (
    intake_batch_output_id bigserial PRIMARY KEY,
    dataset_id uuid NOT NULL,
    workspace_id uuid NOT NULL,
    app_id text NOT NULL,
    source_code varchar(64) NOT NULL,
    batch_ref varchar(128) NOT NULL,
    object_key text NOT NULL,
    object_etag text,
    mapping_version_id uuid NOT NULL,
    target_entity varchar(32) NOT NULL
        CHECK (target_entity IN ('CUSTOMER', 'TRANSACTION', 'EVENT', 'ASSET', 'RELATIONSHIP')),
    route varchar(32) NOT NULL CHECK (route IN ('customer_pipeline', 'cdp_intake')),
    status varchar(32) NOT NULL CHECK (status IN ('RUNNING', 'LOADED', 'HANDED_OFF', 'FAILED')),
    source_rows integer NOT NULL DEFAULT 0,
    output_rows integer NOT NULL DEFAULT 0,
    rejected_rows integer NOT NULL DEFAULT 0,
    skipped_rows integer NOT NULL DEFAULT 0,
    output_key text,
    handoff_batch_ref varchar(128),
    error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT uq_intake_batch_output UNIQUE (dataset_id, batch_ref, object_key, target_entity)
);

CREATE INDEX IF NOT EXISTS ix_intake_batch_output_batch
    ON control.intake_batch_output (source_code, batch_ref);

-- Same readers as the existing upload status contract (connector API), SELECT only.
DO $$
DECLARE reader record;
BEGIN
    FOR reader IN
        SELECT DISTINCT x.grantee
        FROM pg_class c, LATERAL aclexplode(c.relacl) x
        WHERE c.oid = 'control.customer_pipeline_status'::regclass
          AND x.privilege_type = 'SELECT'
    LOOP
        IF reader.grantee = 0 THEN
            GRANT SELECT ON control.intake_batch_output TO PUBLIC;
        ELSE
            EXECUTE format('GRANT SELECT ON control.intake_batch_output TO %I',
                           pg_get_userbyid(reader.grantee));
        END IF;
    END LOOP;
END $$;
COMMENT ON TABLE control.intake_batch_output IS
    'Intake v2: one row per uploaded dataset file x projected entity (written by the landing Lambda).';
