# Typed customer history: coordinated cutover (NOT deployed)

## Contract

Migration `019_customer_typed_history.sql` renames the existing relation to
`raw.customer_current` (same OID, stable raw_customer_id, source uniqueness and
existing foreign keys). New `raw.customer` stores one typed observation per
(batch_id, raw_customer_id), unique also by batch/source/source_customer_id.
A transaction-local AFTER INSERT / BEFORE UPDATE trigger appends history;
changed same-batch values fail the whole transaction; exact replay ignores ingestion
clock fields and does not mutate current. Current is the maximum immutable
`(control.ingestion_batch.created_at, batch_id)` tuple (UUID breaks timestamp ties),
not last worker arrival. Late first arrivals are captured before their current
update is suppressed. Batch creation timestamps must be non-null and cannot be
changed after migration. The current-row lock serializes conflicting direct,
Lambda and connector upserts. The composite history FK enforces matching raw ID
and source identity. UPDATE/DELETE/TRUNCATE history are rejected. Historical rows are
not a new identity. `snapshot_id` identifies observations.

Stitcher normalizes current identity separately from per-batch typed evidence.
Historical normalization retries compare every persisted typed staging field and
normalization version. Mismatches roll back the entire standardization call before
current profiles change; no per-page partial commits remain.
Historical validation joins ingestion batch AND raw ID, but uses the existing
current standardized_record_id for identity classification. Missing historical
profiles fail closed. Late jobs never replace current attributes with old upload
attributes. Existing membership/quality/identity foreign keys intentionally stay
on the renamed current relation. History/profile history are not privacy-erasure
APIs; an authorized erasure policy still needs explicit design.

## Discovery and impact

Previous artifacts really exist at
`/Users/kmac15/PycharmProjects/identity-resolution/outputs/customer-history-release/`
(`workspace/`, `test_history_contract.py`, `recover_history.py`, `PLAN.md`).
Their migration was outside the real checkout. Local PostgreSQL 16.15 at
127.0.0.1:55441 has the prototype in database `postgres`, under
`/tmp/cdp-customer-history.SHTemL`; this work leaves that catalog/data intact.
New regression tests use separate `cdp_history_regression` database.

Recursive Python/SQL/TS/JS scan of PycharmProjects found live code reads/writes in:
- aws-flex-deploy ingestion: current upsert/source lookup; history membership lookup.
- fargate-user-stitching repository, standardize, validation repository.
- cdp-database-connector-worker loader (additional direct writer, updated too).
- verification SQL (staging orphan join changed to current).
- old migrations, generated build/lib copies, presentation text and release
  prototypes: not runtime source; do not deploy old build/lib copies.
No journey-builder-api raw.customer literal was found in that scan; dynamically
built SQL and external consumers remain an operator inventory requirement.

Catalog audit before deployment (read-only):
```sql
SELECT conrelid::regclass, conname, pg_get_constraintdef(oid)
FROM pg_constraint WHERE confrelid='raw.customer'::regclass;
SELECT view_schema,view_name FROM information_schema.view_table_usage
WHERE table_schema='raw' AND table_name='customer';
SELECT grantee,privilege_type FROM information_schema.role_table_grants
WHERE table_schema IN ('raw','staging') AND table_name IN ('customer','customer_profile');
```
Renamed-relation views keep current semantics by OID: explicitly recreate only
views intended to show observations. Frontend direct `raw.customer WHERE batch_id`
queries now get typed uploads. Joins only on raw_customer_id are NOT unique on
history: identity consumers must select current or supply batch_id.

## Deployment gate and order

1. Do not run AWS/live changes from this preparation task. Parent/operator uses
   profile buzz, region ap-southeast-7, credentials via existing secure tooling.
   Never read .env or print DSNs/secrets/customer payloads.
2. Snapshot/backup and record FK, view, ACL, row-count, identity and deployment
   manifests. Inventory all writers, including connector loader and old tasks.
3. Pause landing->ingestion delivery, connector sync loads AND admission queue
   dispatch. Let active ingestion and stitcher transactions drain. Coordinate
   admission pause with its owning session, not by reverting its commits.
4. Build ingestion, stitcher and connector artifacts from modified source; do not
   deploy stale release prototypes. Save old artifact versions for rollback.
5. Apply SQL with `psql -v ON_ERROR_STOP=1 -f database/migrations/019_customer_typed_history.sql`.
   It is one-shot and transactional, takes ACCESS EXCLUSIVE on current, uses a
   10-second lock timeout, duplicates current rows/index storage, and can require
   substantial WAL/disk/time. Size capacity and maintenance window first.
6. Deploy ALL three updated writers/readers before unpausing. Verify runtime roles
   can SELECT current/history/profile history and INSERT current/history/profile
   history. Migration copies explicit SELECT/INSERT/REFERENCES grants, not owners,
   UPDATE/DELETE privileges or RLS policies. If source uses RLS, custom ownership,
   security-definer APIs or column grants, STOP and design matching policies first.
7. Recover absent historical batches before running queued old validation jobs.
   Do not mark missing evidence successful, or replace an old payload with current.
8. Canary two uploads with same source key and distinct values; delayed first job
   must validate first values while current identity retains second values. Check
   counts, shared raw ID, distinct snapshot IDs, current standardized ID continuity,
   membership and validation totals; manually verify frontend as user requested.
9. Resume connector/ingestion, then admission; monitor errors and missing evidence.

## Backfill: safety specification / remaining implementation gate

The old `recover_history.py` is NOT approved for apply: it trusts equal row counts
with ON CONFLICT DO NOTHING, without comparing existing typed values, and depends
on a recovery ledger absent from this migration. Do not execute it unchanged.

Required recovery implementation:
- Read completed batch lineage, authoritative versioned canonical S3 object,
  recorded checksum and row count. Verify bytes SHA256 and selected canonical key
  count. For connector Parquet manifests use their matching contract, not CSV.
- Run pinned parser/normalizer; record parser version plus artifact digest. Current
  parser may differ from original historical transformations: mark reconstruction,
  never claim byte-for-byte original typed values without proof.
- Resolve stable raw ID by source/key, preferably validate immutable membership;
  missing/ambiguous IDs stop the batch. Do NOT call normal ingestion/upsert current,
  dispatch ECS, change quality results or replay identity.
- In a per-batch transaction insert only missing history rows with origin
  CANONICAL_RECONSTRUCTION; preserve all original evidence. Compare every existing
  typed field excluding observational clocks/provenance against reconstructed
  value BEFORE accepting a conflict. Mismatch must rollback, not update history.
- Verify exact key set, typed-value comparison, totals and membership; write an
  auditable recovery ledger only after all checks. Rerun must be idempotent.
- Missing artifact/checksum/version/identity means unrecoverable, reported plainly.

Reported production examples are unverified here (no live DB/AWS accessed): landing
5af53a20-a21c-4341-824f-b7df12560b05 -> ingestion
54bf574b-7f15-49ff-9fbd-2a299230f409 reportedly 12000 rows, with 892 keys later
updated by ingestion 72fd943f-4747-4910-81df-45513ef2ef3e. Do not advertise recovery
until canonical artifact comparison proves these numbers.

## Tests executed

From aws-flex-deploy:
```sh
CDP_HISTORY_TEST_DSN='host=127.0.0.1 port=55441 user=postgres dbname=cdp_history_regression' \
  ../fargate-user-stitching/.venv/bin/python -m pytest \
  tests/integration/test_customer_history.py tests/integration/test_customer_history_runtime.py -q
.venv/bin/python -m pytest tests/unit -q
```
2 real PostgreSQL tests passed; 124 unit tests passed (one existing dateutil
DeprecationWarning). fargate-user-stitching: `.venv/bin/python -m pytest -q` => 32
passed. Connector worker same command => 96 passed. Runtime test borrows schema
metadata only from local prototype, so portable standalone fixture packaging is
still needed. Full live-scale performance, deployment, backfill and AWS validation
are NOT performed.

## Rollback

Before SQL COMMIT, PostgreSQL rollback restores prior state. After commit, pause
all writers again: never simply point old binaries at historical raw.customer.
Prefer forward fix. A deliberate reverse cutover must archive new history/profile
history, preserve current relation OID/FKs, remove capture trigger, and rename
current back only after dependencies/grants are reviewed. Do not drop evidence
or revert migration 018 / concurrent admission changes.
