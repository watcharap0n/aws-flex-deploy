# Ingestion failure hotfix

Apply `database/migrations/017_customer_suspended_status.sql` before releasing
this ingestion source. This preserves SUSPENDED (quality contract v1), BLOCKED,
and the other existing states; unknown nonempty states are not defaulted.

## Failure/retry contract

After checksum/landing-lineage validation and batch allocation, payload writes
(job, quality snapshot, customer upsert, membership) use a PostgreSQL savepoint.
A catchable failure rolls these writes back and commits `ingestion_batch.status
= 'failed'` with only the exception class in `metadata.ingestion_error_code`.
A sanitized exception (batch UUID and exception class, no rejected-row details)
is raised after commit: the event is **not acknowledged
as successful**, so normal Lambda asynchronous retries remain enabled.

The unique source/object/checksum key atomically reclaims only failed batches
marked by this payload-failure path and with identical landing lineage. It
retains the batch UUID. Completed duplicates remain skipped and use existing
stitching dispatch/idempotency. No stitching job from an unsuccessful payload
survives. Success clears failure metadata. This is not a new retry scheduler.

Limitations: download/parse/lineage/setup failures before the savepoint, process
termination, database disconnection, and commit failures cannot be durably
recorded by this mechanism. They still raise. Exhausted Lambda retries require
operational recovery; no infinite retry or unconditional success is introduced.

## Verification and scoped release

- Run `.venv/bin/python -m pytest tests/unit -q`.
- In a separate environment with real psycopg2, set `CDP_TEST_DSN` (and use
  libpq password sourcing) and run
  `python -m pytest tests/integration/test_ingestion_failure_transaction.py -q`.
  This test redirects SQL exclusively to session-local temporary tables.
- Apply migration 017 with stop-on-error, bounded lock timeout, then inspect
  `pg_get_constraintdef` for `raw.customer.customer_status_check`.
- Normal infrastructure route: `CONFIG_FILE=env.yaml cdk deploy CdpIngestionData`
  **only after reviewing the CDK diff**. Do not deploy unrelated stack changes.
- Scoped emergency source-only route: back up current Lambda ZIP/config, compare
  current runtime source with the reviewed baseline, replace only
  `lambda_function.py` inside the existing ZIP, call `update_function_code` with
  the observed `RevisionId`, and wait for `LastUpdateStatus=Successful`.
  Download the updated ZIP and verify both ZIP CodeSha256 and source bytes.
  Leave environment, layers, event sources, retry configuration and ECS unchanged.
- A source-only hotfix does not reconcile CloudFormation asset metadata. A future
  reviewed CDK deployment from the fixed branch must preserve this source.
- Do not launch recovery in parallel with the existing serial recovery owner.
