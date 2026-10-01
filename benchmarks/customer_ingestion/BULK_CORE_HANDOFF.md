# Bulk core handoff (local-only; NOT performance acceptance)

## Integration contract

- Import `_lambda/ingestion_data/customer_bulk.py:upsert_customers(cursor, customers, batch_id, source_system_id)` after applying migration 022. Accepts current prepared dictionaries or a one-pass iterable, returns selected row count; **does not commit**. Emits sanitized uncommitted COPY/merge timing, plus SQL NOTICE stage JSON.
- Python input must be canonical unique selected source keys. COPY is bounded (64 KiB reads, one encoded pending record); full typed staging is a PostgreSQL temp table. Do not confuse bounded Python memory with bounded DB temp/WAL/transaction space.
- Function creates/checks current, immutable typed history, immutable membership. Existing `_record_batch_members` may remain as an idempotent compatibility call. Caller must still compare final full batch key set/count against canonical source, seed validation, finalize lineage/status, then commit atomically.
- Source-level advisory transaction lock is acquired inside merge and held through caller commit. If existing-source snapshot and validation must be serialized with other source ingestions, acquire the **same** `pg_advisory_xact_lock(hashtextextended('customer-bulk:' || source_id::text,0))` **before** `_create_existing_source_snapshot`. Chunking SQL does not release this lock or commit.
- No `SECURITY DEFINER`, replication-role changes, trigger-disable command, or client GUC bypass. Function-scoped `enable_nestloop=off` is a query-planner choice, restored automatically; tests check restoration. Immediate FKs remain enabled.

## Coordinated connector rollout is a RELEASE BLOCKER

022 replaces 019 row capture with transition-table INSERT/UPDATE statement triggers. Direct INSERT/forward UPSERT still captures and compares history/membership. Direct late UPDATE now **rejects transaction** instead of silently losing history or rewinding current. Do not resume the unmodified connector with 022.

Connector integration must create `pg_temp.customer_bulk_stage (LIKE raw.customer_current INCLUDING DEFAULTS INCLUDING CONSTRAINTS) ON COMMIT DROP`, uniquely index `source_customer_id`, insert its already selected typed rows with batch/source, then `SELECT raw.apply_customer_bulk(batch_uuid,source_bigint)`. Reuse existing lowercase-email/status/date normalization. Leave `customer_id` unset for new rows; merge preserves legacy current customer_id for existing source identities. Both writers and schema must roll out while paused. 019/020/021 were not edited.

## Verified local correctness

On PostgreSQL **16.15**, localhost **55441**, owned DB **cdp_bulk_regression**: 27 integration cases pass. Includes A/B/replay, late-first A, mismatched old replay, overlap/new identities, typed JSON/time/NULL equivalence, exact snapshot identity on retry, COPY escaping/Unicode, invalid status/age, duplicate keys, membership contradictions, full transaction rollback, same-source concurrency including same-batch conflict, non-superuser invocation, forged bypass GUC rejection and planner-setting restoration.

`tests/integration/fixtures/customer_bulk_live_base.sql` pins all raw current/history/membership columns/indexes/FKs/checks from parent's `live-schema-contract.json`. Control/profile scaffolding is intentionally small, so this is NOT a full validation/stitching benchmark. Parent must exercise actual PG17.7 and whole pipeline.

Run from aws-flex-deploy:

```
.venv/bin/python -m pytest tests/integration/test_customer_bulk.py tests/integration/test_customer_bulk_benchmark.py -q
.venv/bin/python benchmarks/customer_ingestion/benchmark_bulk_core.py --rows 5000 --output /absolute/path/result.json
```

The second command refuses when disk is below a 512 MiB reserve plus conservative fixture budget. The harness resets only its owned DB, uses parent's deterministic `generate_fixture.py`, parses through the real Lambda parser, runs baseline/bulk/bulk/baseline with A then 50%-overlap B then exact B retry, commits and reads exact key sets and typed digests back. Pinned old Lambda function is `tests/integration/fixtures/customer_bulk_baseline.py`.

## Actual performance evidence and limits

Artifacts under `identity-resolution/outputs/ingestion-throughput-release/`:

- `local-bulk-core-5000.json`: initial 5k/batch experiment. Baseline 6.956/5.168 s vs bulk 6.540/6.498 s complete three-phase core runs. Not a speedup; discovered bad comparison plan.
- `local-bulk-plan-exists-before.json`: exact EXISTS predicate took 2.630 s; 12,497,500 rejected join pairs, 1,335,400 temp-block reads. Bare SELECT instead chose a fast hash join, so testing the exact PL/pgSQL predicate mattered.
- `local-bulk-core-1000-safe-plan.json`: after scoped planner fix, A/B/replay with 1k/batch, 1,500 distinct current keys and 2,000 history+membership each. Same typed digest in all four runs. Baseline total 1.004/0.979 s; bulk 0.546/1.720 s. Medians **0.992 s baseline / 1.133 s bulk**: noisy local results do **not** establish overall speedup. Parser/COPY/current/history/membership/commit timings retained. ~1.104/1.107 MB canonical A/B files with Thai/full-width typed/nested-JSON values. Generator SHA and fixture SHA recorded.
- The 1k benchmark precedes the additional statement-membership contradiction check (covered by regression); final code needs a fresh benchmark.

Host free disk dropped from 2.4 GiB at discovery to 477 MiB while work proceeded; further 5k/final benchmark was correctly refused. No unrelated files/volumes pruned and no million fixture generated. Final smoke benchmark is skipped below reserve, not counted as accepted performance.

Unmeasured: S3 canonical availability clock, download, startup/queue, durable-worker kill/restart paths, validation/existing-source snapshot cost, source landing, Aurora ACU/IO, 1M mixed/new and 2M observations. **No claim of 1M <600 s or <300 s.** Parent owns cloud benchmarking, full-pipeline review and coordinated release; no production writes/deploy/commit performed here.
