-- Apply after 019, with all writers paused. Does not grant runtime write access.
BEGIN;
SET LOCAL lock_timeout = '10s';
CREATE TABLE control.customer_history_recovery (
 ingestion_batch_id uuid PRIMARY KEY REFERENCES control.ingestion_batch(batch_id),
 landing_batch_id uuid NOT NULL REFERENCES control.source_landing_batch(batch_id),
 status text NOT NULL CHECK (status = 'VERIFIED'),
 expected_rows bigint NOT NULL CHECK (expected_rows >= 0),
 inserted_rows bigint NOT NULL CHECK (inserted_rows BETWEEN 0 AND expected_rows),
 artifact_sha256 text NOT NULL CHECK (artifact_sha256 ~ '^[0-9a-f]{64}$'),
 typed_sha256 text NOT NULL CHECK (typed_sha256 ~ '^[0-9a-f]{64}$'),
 membership_sha256 text NOT NULL CHECK (membership_sha256 ~ '^[0-9a-f]{64}$'),
 provenance jsonb NOT NULL CHECK (jsonb_typeof(provenance) = 'object'),
 verified_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE TRIGGER customer_history_recovery_immutable
 BEFORE UPDATE OR DELETE OR TRUNCATE ON control.customer_history_recovery
 FOR EACH STATEMENT EXECUTE FUNCTION raw.reject_customer_history_mutation();
COMMENT ON TABLE control.customer_history_recovery IS
 'Verified canonical reconstructions, not proof of original historical transformation. Per-batch atomic append-only audit.';
COMMIT;
