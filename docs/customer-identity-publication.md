# Post-stitch customer publication — local handoff

## Upload-only correction (supersedes earlier connector rollout evidence)

024/025 were amended locally before their first production application; no 026,
deployment, platform records, worker code or frontend changes are required by this
scope correction. Baseline bytes/hashes are preserved in
`/Users/kmac15/Downloads/customer-identity-publication-release/upload-only-baseline`.

Every valid upload app is eligible, not just the example app. The literal app
substring in `YYYY-MM-DD-HHMMSS-<numeric app_id>-<12 lower hex>` is authoritative;
leading zeroes remain significant. Existing ingestion batch_ref length limits
still apply. Private `control.customer_identity_upload_scope` assigns each app an
immutable UUID, atomically inside successful capture. The existing `workspace_id`
wire field now carries this INTERNAL upload scope, **not platform.workspace**.
No workspace provisioning, platform FK, default app or allowlist is involved.

Any `connector.sync_run` ingestion, landing or job lineage excludes the batch,
including retries and file-looking refs. Exclusions create no transfer, export,
capture issue or scope. They retain previous pipeline status/clock and have no
transfer metadata; the lightweight publication-status view omits them. Keep
`CONNECTOR_PUBLICATION_STATUS_ENABLED=false` throughout this upload-only release.
Unknown/connector contributors to a selected global entity still BLOCK its upload
with `MIXED_OR_UNKNOWN_OWNER`; source/canonical/history checks are not bypassed.
Malformed upload refs yield `INVALID_UPLOAD_BATCH_REF`.

025 adds a fixed-search-path, read-only SECURITY DEFINER boolean exclusion helper,
granted only to the existing pipeline SELECT readers/owner. This is needed because
PostgreSQL checks function EXECUTE as the querying user even through a view; the
private 024 helpers, registry and snapshots remain ungranted. New status readers
need EXECUTE on `control.customer_publication_is_connector(uuid)` as well as view
SELECT. Install both migrations as the same trusted owner (or explicitly provision
that owner's private helper access). Preserve the registry in backup/restore so
scope UUIDs do not change. BLOCKED captures still require a new stitching run.

Traceback fields are unchanged: transfer links ingestion batch, stitching job,
resolution run and literal app; snapshot retains source_system_id, original
source user ID, raw/standardized record UUIDs and customer_entity_id. No source
customer ID transformation or extra tracking implementation was added.

## State

Implemented and exercised locally. **No production migration, registry population, data rewrite, infrastructure deployment, commit or push was performed.** Existing unrelated working-tree changes were preserved. Snapshot, status/API, compatibility cutover and worker all passed independent final review with no remaining security or logic findings. The interrupted worker review was retried successfully; the reviewer independently passed61 tests plus five adversarial probes, including marker bypass, projection corruption, sorting-key drift and zero-write replay.

## Evidence and chosen scope

The stitcher standardizes a selected batch but resolves all eligible current profiles globally (`fargate-user-stitching/src/identity_stitcher/service.py`, `linkage.py`). Members and canonical entities are mutable current state; a run-only outbox cannot reproduce an old resolution. Existing pipeline status declared `COMPLETED` at stitching success, and connector responses exposed workflow success. The separate commerce publisher reads `standardized.*`, not identity outputs, and swaps tables sequentially.

Read-only live inspection observed PostgreSQL17.7 and ClickHouse25.8.33.6. Existing `cdp_customer.customer_profile` had38 columns, MergeTree order `(app_id,source_system_id,tenant_key,customer_id)`, no resolution lineage, and436,623 rows/distinct `(app_id,customer_id)` keys at inspection. That is a snapshot, not a permanent count. Its nonnull standardized-model Int64 IDs cannot be replaced with the stitcher's UUIDs.

The narrow change is: **successful stitch transaction → immutable selected-user snapshot/outbox → separate bounded worker → verified paired typed projection → one publication marker → final public completion.** Matching/normalization thresholds and ingestion payloads are unchanged.

## Identity, ownership and publication contract

- Mapping `user_id` and profile `customer_id` remain the original `source_customer_id`. `customer_entity_id` is lineage only. No full match-pair history is exported.
- Unique `(app_id,user_id)` / `(app_id,customer_id)` per published scope. Mapping and profile derive from the same physical combined version row and marker, under the same `resolution_run_id`.
- File resolver reads durable landing/ingestion lineage with the anchored documented batch-ref format. Connector lineage is exclusion evidence only. No frontend/upload payload changes.
- Registry is auto-populated per literal upload app with an immutable internal UUID, unrelated to platform workspace. Malformed/conflicting ownership is blocked; no global default or app allowlist.
- Capture checks **every member of selected entities**, including users outside the batch, historical ownership and the contributing staging batch. Mixed/unknown components, stale resolution or source-key conflicts remain unpublished. The matcher itself is still global: rejected components require authoritative repair or separately approved workspace-scoped stitching.
- Complete trustworthy membership history is a prerequisite. Lost pre-014 ownership cannot be inferred. Existing `(source_system_id,source_customer_id)` reuse across apps remains an upstream risk.
- Snapshot and retry scope are immutable. Capture is visible only with successful stitching commit. Subsequent identity runs cannot change the saved payload.
- Ownership locks use NOWAIT to avoid reversing ingestion lock order and deadlocking existing ingestion. Contention gives `BLOCKED / CAPTURE_LOCK_CONTENDED`; successful stitching is retained. **A new stitching run is required after blocked capture**, not stale recapture. Successful capture still holds SHARE locks through outer commit and may delay newly arriving writers.
- Tables/sequences/helpers are private to their owner at migration installation, including revocation of inherited named-role default grants. Give the worker only documented read/column-update privileges.

### Status

Ingestion `completed` and stitching/resolution `SUCCEEDED` remain stage truth. Public status progresses through `TRANSFER_PENDING`, `TRANSFERRING`, `TRANSFER_FAILED` or `TRANSFER_BLOCKED`; `COMPLETED[_WITH_WARNINGS]` appears only after the matching effective latest attempt is `PUBLISHED`.

This applies only to upload batches in the SQL pipeline view. Connector statuses/clock remain unchanged. A journal-only upload capture failure becomes `TRANSFER_BLOCKED` with its sanitized reason, not perpetual pending. Stored execution status/error/`completed_at` are not rewritten.

`CONNECTOR_PUBLICATION_STATUS_ENABLED=false` must remain false for this upload-only release. No API behavior changes or UI verification are claimed.

### Worker safety and compatibility

- Keyset source reads and bounded inserts: default10,000 rows /8MiB, whichever limit is reached first. No row-by-row database writes and no full graph/match-pair transfer.
- Canonical UTF-8 compact sorted-key JSON + LF, ordered by immutable row number, is the SHA-256 authority. PostgreSQL `jsonb::text` is not this checksum format.
- Verify actual stored payloads/counts and independently derived Python expected typed mapping/profile values **before marker publication**. Strict dates, explicit ISO clocks, UTC range, numeric types and UUIDs prevent silent ClickHouse coercion.
- Complete canonical DDL preflight validates table engines/keys/columns, projection expressions and marker dependencies; one view-name substring is insufficient.
- Missing compact-profile observation clocks use `standardized_at` as an observation fallback. Source timestamps and original payload remain unchanged; this does not invent known source/event time. New identity-origin standardized Int64 IDs are NULL. Unavailable eligibility is false, not fabricated true.
- Per-attempt stage tables and atomic partition replacement avoid physical duplicates on retry. Old published versions remain retained. Current keys use publication sequence, so old replay cannot revert newer values.
- A separate PostgreSQL advisory lock serializes publishers. Durable per-request intent plus terminal ClickHouse query-log evidence fences process death/lock loss/ambiguous acknowledgement. Unknown completion **stops the queue**; do not clear intent by hand or blindly retry delayed writes.
- Requires direct/sticky access to one ClickHouse server, synchronous MergeTree, query-log retention and `system.processes`/`system.query_log`/catalog reads, `SYSTEM FLUSH LOGS`, `DESCRIBE TABLE (...)`, and `formatQuerySingleLine`. Production transport must be TLS or an approved encrypted tunnel. Redirects are disabled; response bodies/credentials/PII are not logged.

## Existing customer_profile compatibility cutover

`scripts/cutover_customer_identity_profile.py` is an **explicit offline operation**, not worker startup migration.

1. Stop/fence all old commerce and new identity publishers and verify no in-flight writes. `--writers-stopped` is operator attestation, not an automatic distributed lock.
2. Install the reviewed ClickHouse publication DDL. The template contains `{database}`; use `ClickHouse.install()` with an explicitly selected database. Existing modified objects require explicit reviewed updates: `IF NOT EXISTS` is not schema migration.
3. Review all consumers using `FINAL` or nonnull standardized IDs: final `customer_profile` becomes a View and two Int64 IDs become Nullable. Keep the old exact38-column/exchange publisher retired or update its ownership before resuming it.
4. Cutover rejects invalid legacy/resolved grains, unsupported types, unsafe dependencies and drift. It creates `customer_profile_legacy_snapshot`, streams full-row SHA reconciliation, builds a one-resolved-scan overlay, and uses one atomic `EXCHANGE TABLES`.
5. Original physical rows remain in `customer_profile_pre_identity` for rollback. Active/recovery paths validate object UUID, columns, expected view SQL, snapshot fingerprint and live readability. Lost exchange acknowledgement is reconciled without another copy. Incomplete copy fails closed.
6. Untouched legacy customers remain visible with original standardized IDs and **NULL identity lineage**. They are not falsely advertised as resolved. New published source users override only their matching `(app_id,customer_id)`.

Example only after separate approval:

```bash
python scripts/cutover_customer_identity_profile.py \
  --url https://<private-clickhouse-endpoint> \
  --database cdp_customer --writers-stopped
```

Credentials are injected through `CLICKHOUSE_USER`/`CLICKHOUSE_PASSWORD`, never command-line passwords. Cutover requires TLS for nonloopback endpoints.

Rollback requires stopped publishers and exact catalog reconciliation, then exchanging the view and retained original table. This restores the **pre-cutover** snapshot, not newer published customers; do not automate it during live processing.

## Consumer consistency

Current mapping/profile views share the publication contract, but independent queries can straddle publication. Paired multi-query consumers must pin a resolution run and read `identity_profile_committed WHERE resolution_run_id=...`, or use the combined committed dataset. Filtering a current view by an old run drops keys subsequently superseded and is not historical replay. Legacy users without identity lineage are outside the paired resolved-cohort guarantee.

## Measured performance

Real local PostgreSQL16.15 → ClickHouse25.8.33.6, actual migrations024/025, actual cutover and worker, **synthetic** users. No production PII copied.

| Scenario | 100k users +1 legacy profile | 100k users +436,623 legacy profiles |
|---|---:|---:|
| Capture + commit (excludes seeding) |6.666s|7.683s|
| Transfer wall time, all verification/publication included |43.188s|63.936s|
| Load phase |15.169s|20.731s|
| Verification + publication |27.821s|42.832s|
| Transfer throughput |2,315 users/s|1,564 users/s|
| Full profile read |3.145s /100,001 rows|15.159s /536,623 rows|
| Verified replay, no new inserts |small-scope replay tested separately|45.883s|
| Physical version rows / marker rows |100,000 /1|100,000 /1|

Larger-overlay capture+transfer measured **71.62s**, excluding original ingestion/global matching, queue scheduling and process startup. Source and destination checksum/counts matched; final public status was `COMPLETED`. Larger legacy fixture matches the observed cardinality, **not** actual attribute distributions or production compute/network contention. Retained multi-run growth and million-user batches are not benchmarked. These are measured bounds of this fixture, **not a production SLA or a proven optimization speedup**. Verification dominates; do not remove safety gates to improve a headline number.

Evidence: `benchmarks/poststitch-parent-e2e-100k.json` and `benchmarks/poststitch-parent-e2e-100k-legacy-436623.json`. The earlier `poststitch-parent-100k.json` is a pre-review full-payload baseline with a different fixture and must not be used for A/B speedup claims.

## Verified tests

- Worker + CLI + review regressions + migration routing:61 passed. With selected existing identity regression tests:103 passed. Full stitcher suite with local publication tests enabled: **172 passed,31 skipped** (other opt-in fixtures/benchmark); Ruff and independent final review passed.
- PostgreSQL capture: **65 passed** on16.15; repeated on **17.7:65 passed in63.20s**. Independent review passed, including negative controls for prior ACL/deadlock defects and non-superuser/least-privilege probes.
- SQL public status: **30 passed**; API/connector integration: **114 passed**. Independent adversarial re-review passed.
- Compatibility cutover: **57 passed**, independent final review passed.
- Actual capture→cutover→worker→status E2E: **5 passed**, covering file and connector lineage, mixed-workspace rejection, source-ID/shared-entity correctness, retained legacy profiles, exact physical retry and independent stitching success during transfer failure. Separate larger-legacy100k benchmark: **1 passed**.
- PostgreSQL and ClickHouse read-only reconciliation SQL executed on the isolated fixture; no key/lineage/marker mismatches.

Do not sum overlapping suites into a unique-test total. No production migration or UI tests were run.

## Changed files

`aws-flex-deploy/`:
- `database/migrations/024_customer_identity_publication.sql`
- `database/migrations/025_customer_publication_status.sql`
- `database/queries/verify_customer_identity_publication.sql`
- `database/queries/verify_customer_identity_publication_clickhouse.sql`
- `scripts/cutover_customer_identity_profile.py`
- `tests/integration/test_customer_identity_publication.py`
- `tests/integration/test_customer_publication_status.py`
- `tests/integration/test_customer_identity_profile_cutover.py`
- `tests/integration/test_customer_identity_publication_e2e.py`
- This handoff and the benchmark JSON files above.

`fargate-user-stitching/` (directory has no Git metadata):
- `src/identity_stitcher/publication.py`
- `src/identity_stitcher/sql/clickhouse_identity_publication.sql`
- `src/identity_stitcher/db.py` — numbered PostgreSQL startup migrations only; never run ClickHouse DDL on PostgreSQL.
- `tests/test_clickhouse_publication.py`, `_cli.py`, `_review.py`, `_benchmark.py`
- `tests/test_publication_migration_routing.py`
- `tests/fixtures/publication_db_startup_baseline.py` preserves pre-edit db.py bytes.

`journey-builder-api/`:
- `app/core/config.py`
- `app/modules/connectors/controller.py`, `repository.py`, `schemas.py`
- `tests/test_connector_publication_status.py`, `tests/test_connectors.py`
- `README.md`

No implementation edits to `identity-resolution`, connector worker, ingestion/landing handlers or infrastructure stacks for this task.

## Remaining production work — separately authorized

1. Resolve authoritative release/commit ownership for the non-Git stitcher checkout. Review the packaged manifest and build from a pinned source, not an untracked research directory.
2. Confirm complete upload ownership coverage and assess blocked global components/source-key conflicts; no app/workspace pre-provisioning or default assignment.
3. Apply amended024 then025 as the same trusted owner. No platform dependency/FK. Provision explicit publication-worker and status-reader grants; verify with actual roles. Never grant registry writes to applications/workers.
4. Install/update exact CH DDL, restrict PII/marker/stage/query-log access and retention, then approve the stopped-writer cutover. Retire/update the old publisher and validate consumers.
5. Run the independent worker with bounded limits, TLS/tunnel and direct server routing, under an approved existing runtime/supervisor. No ECS/CDK resources are deployed by this change.
6. Keep `CONNECTOR_PUBLICATION_STATUS_ENABLED=false`. Verify upload status/readback and unchanged connector status on an isolated canary before rollout completion.
7. Monitor blocked capture, transfer failure, in-flight intent and queue age; never replay ingestion to fix only downstream transfer. Retry frozen transfer only when safe; blocked capture needs new stitching. Retain old versions until explicit retention/GC policy review.
8. Measure representative production-like batches and network, plus growing history. Final performance acceptance remains workload-specific; no million-user or end-to-end upload SLA is asserted.

Worker invocation (credentials via environment or existing database config):

```bash
python -m identity_stitcher.publication --once
python -m identity_stitcher.publication --daemon \
  --batch-rows 10000 --batch-bytes 8388608 --retry-seconds 60 --poll-seconds 10
```

Required CH settings: `CLICKHOUSE_URL`, optional `CLICKHOUSE_DATABASE`, `CLICKHOUSE_USER`, `CLICKHOUSE_PASSWORD`. PG: `PUBLICATION_PG_DSN` or existing `load_database_config()`/secret mechanism. `--once` returns nonzero on failed/blocked transfer. Ambiguous query completion stops daemon for reconciliation.

## Source handoff archive

`customer-identity-publication-handoff.zip` contains only the explicitly listed task files, preserving their repository-relative paths, plus a SHA-256 manifest. It is a source-change handoff, **not a standalone deployable image**: sibling fixture dependencies, existing source packages and pinned build ownership still belong to the original repositories. Do not overwrite unrelated working-tree files blindly. No credentials or production records are included.
