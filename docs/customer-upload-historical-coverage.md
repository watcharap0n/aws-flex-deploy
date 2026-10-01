# Historical upload coverage (029)

## Contract

`control.customer_pipeline_status.overall_status` still describes the original upload's end-to-end attempt. Migration029 never changes that status, its timestamps, any error, or any existing column. It does not create transfers, replay ingestion, recapture old jobs from mutable identity state, or repair historical observations.

New nullable `app_snapshot_*` columns describe **a separately published app snapshot whose frozen manifest explicitly includes this exact ingestion batch**. They are not a replacement for the original `transfer_*` fields.

- `app_snapshot_status = PUBLISHED` means matching successful app resolution and matching published transfer ledger evidence exist.
- `app_snapshot_scope = LATEST_APP_USERS_AT_CUTOFF` is mandatory context: the result represents latest required immutable observations at the recorded cutoff, not every upload's observation or original stitching output.
- Run/transfer/workspace/app IDs, publication sequence/time, input checksum and cutoff identify the separate evidence.
- `app_snapshot_app_user_count` is the whole snapshot's app-user count; **never sum it over uploads**.
- `app_snapshot_batch_expected_members`, `app_snapshot_batch_available_observations` and their difference are frozen batch-history counts at snapshot creation. They do not silently change after history recovery.
- NULL means no qualifying published snapshot includes this exact batch. It does not imply a queued worker.
- A later upload of the same app does not inherit an old snapshot. Literal app IDs remain distinct (`000222` is not `222`). Latest valid published matching manifest wins without duplicating pipeline rows.
- A FAILED/BLOCKED ordinary transfer or failed landing/ingestion/stitching stage remains visible even if separate app snapshot evidence exists. Existing failure/outcome columns are untouched.

## User-visible status repair gate

This migration alone is **not a completed UI fix**. No current caller contract has been demonstrated to consume these new columns. A caller must explicitly render two facts, for example:

> Included in published app snapshot (latest app users as of cutoff). Original upload publication: unavailable / blocked. Missing historical observations: N.

Do not map `app_snapshot_status=PUBLISHED` to ordinary COMPLETED, suppress original errors, mark all history recovered, or continue portraying a missing old capture as an actively queued transfer. For failed or active newer attempts, keep the current failure/progress prominent; app evidence is informational only. Update response field selection, polling and UI labels under a tested caller contract before claiming user-facing resolution. New status enum values require explicit client compatibility, not speculative SQL replacement.

## Manual rollout and rollback

1. Review029 independently with current025–028 dependency definitions. Confirm the target schema and exact reader roles.
2. Run the tests below against isolated PostgreSQL, never a forwarded production port.
3. Save installed pipeline view definition, OID, owner, ACL and ordered columns; read-only preview the029 SELECT against current ledgers. Persist exact affected batch list, not just totals.
4. Parent deployment owner may apply029 in its own short transaction during an approved DDL boundary. This task did not apply it.
5. Read back actual querying-role access, preserved old-column results, new evidence, zero row multiplication and selected original failure rows. Verify current ClickHouse markers/version checksums separately; PostgreSQL ledger projection is not a fresh payload audit.
6. First rollback option is caller opt-out; additive columns are harmless. Do not DROP the shared pipeline view to remove columns—PostgreSQL cannot remove appended view columns with CREATE OR REPLACE while preserving dependents. Any physical rollback requires separately reviewed dependency-aware DDL.

## Local verification

`/Users/kmac15/PycharmProjects/fargate-user-stitching/.venv/bin/python -m pytest tests/integration/test_customer_upload_historical_coverage.py -q`

The module creates and drops only `cdp_status_repair_test` on loopback61677, refuses a preexisting database, validates exact database/host/port, installs real014,019,022,024–028, and exercises029 with actual immutable finalization. Existing migrations are not edited. Test scopes cover gaps, original failures and captures, retries, absent evidence, literal-app/connector exclusion, latest publication selection, catalog/ACL preservation and nonowner queries.
