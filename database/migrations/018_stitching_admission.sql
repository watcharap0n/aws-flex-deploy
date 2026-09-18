-- Additive admission journal. Existing QUEUED jobs are the durable outbox.
-- No customer data, status constraints, or active jobs are rewritten.
BEGIN;
SET LOCAL lock_timeout = '5s';
CREATE TABLE IF NOT EXISTS control.stitching_admission (
    job_id uuid PRIMARY KEY REFERENCES control.stitching_job(job_id),
    request jsonb NOT NULL,
    state text NOT NULL DEFAULT 'PREPARED'
        CHECK (state IN ('PREPARED', 'LAUNCHED', 'BLOCKED')),
    dispatch_attempts integer NOT NULL DEFAULT 0,
    prepared_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    next_attempt_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    task_arn text,
    retry_job_id uuid,
    last_error text,
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
COMMENT ON TABLE control.stitching_admission IS
    'Durable ECS admission intents; immutable request/clientToken survives Lambda crash. BLOCKED requires operator reconciliation, never blind relaunch.';
COMMIT;
