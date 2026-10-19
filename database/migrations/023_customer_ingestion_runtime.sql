-- Durable ingestion outbox and attempt fencing. Additive; does not require 021.
-- Install after 019 (+022 for bulk activation). All runtime switches start OFF.
BEGIN;
CREATE TABLE IF NOT EXISTS control.customer_ingestion_runtime (
 singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
 schema_version integer NOT NULL DEFAULT 1 CHECK (schema_version = 1),
 enabled boolean NOT NULL DEFAULT false,
 mode text NOT NULL DEFAULT 'one_shot' CHECK (mode IN ('one_shot','warm')),
 max_active integer NOT NULL DEFAULT 1 CHECK (max_active BETWEEN 1 AND 8),
 max_attempts integer NOT NULL DEFAULT 3 CHECK (max_attempts BETWEEN 1 AND 20),
 lease_seconds integer NOT NULL DEFAULT 180 CHECK (lease_seconds BETWEEN 30 AND 3600),
 retry_seconds integer NOT NULL DEFAULT 30 CHECK (retry_seconds BETWEEN 0 AND 3600)
);
INSERT INTO control.customer_ingestion_runtime(singleton) VALUES (true) ON CONFLICT DO NOTHING;
CREATE TABLE IF NOT EXISTS control.customer_ingestion_job (
 job_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 event_identity text NOT NULL UNIQUE,
 stage text NOT NULL DEFAULT 'canonical' CHECK (stage IN ('canonical','landing')),
 bucket text NOT NULL, object_key text NOT NULL, etag text NOT NULL, version_id text,
 event jsonb NOT NULL,
 status text NOT NULL DEFAULT 'QUEUED' CHECK (status IN ('QUEUED','ACTIVE','SUCCEEDED','FAILED')),
 ingestion_batch_id uuid REFERENCES control.ingestion_batch(batch_id),
 landing_batch_id uuid,
 attempt_count integer NOT NULL DEFAULT 0,
 available_at timestamptz NOT NULL DEFAULT now(),
 created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
 completed_at timestamptz, error_code text
);
CREATE TABLE IF NOT EXISTS control.customer_ingestion_attempt (
 attempt_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
 job_id uuid NOT NULL REFERENCES control.customer_ingestion_job,
 attempt integer NOT NULL,
 status text NOT NULL CHECK (status IN ('RESERVED','SUBMITTED','RUNNING','STOPPING','UNKNOWN','SUCCEEDED','FAILED')),
 slot integer NOT NULL CHECK (slot > 0),
 owner uuid, task_arn text, request jsonb,
 created_at timestamptz NOT NULL DEFAULT now(), submitted_at timestamptz,
 started_at timestamptz, heartbeat_at timestamptz, lease_until timestamptz,
 completed_at timestamptz, error_code text, stop_detail jsonb,
 UNIQUE(job_id,attempt)
);
CREATE UNIQUE INDEX IF NOT EXISTS customer_ingestion_active_slot
 ON control.customer_ingestion_attempt(slot)
 WHERE status NOT IN ('SUCCEEDED','FAILED');
CREATE UNIQUE INDEX IF NOT EXISTS customer_ingestion_active_job
 ON control.customer_ingestion_attempt(job_id)
 WHERE status NOT IN ('SUCCEEDED','FAILED');
CREATE INDEX IF NOT EXISTS customer_ingestion_queue
 ON control.customer_ingestion_job(available_at,created_at) WHERE status='QUEUED';
COMMENT ON TABLE control.customer_ingestion_attempt IS
 'Persist before RunTask/download. UNKNOWN retains slot until operator reconciliation; never blind redispatch after token safety horizon.';
COMMIT;
