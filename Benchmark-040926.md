# CDP ingestion throughput benchmark

Evidence updated: 2026-09-04. All values below are observed from runtime control
tables, ClickHouse ETL audit rows, PostgreSQL activity/statistics, or worker
checkpoints. Estimates are labelled explicitly.

## Executive summary

| Pipeline stage | Latest useful observation | Throughput assessment | Confidence |
|---|---:|---:|---|
| Database connector | 3 rows in 16.9-17.3 min end to end | Fixed orchestration latency only; not a volume benchmark | Low |
| Manual import file, end to end | 85,314-150,000 rows in 2.7-4.6 min | **528-548 rows/sec**, **0.40-0.42 MiB/sec** | High |
| Manual import canonical-output phase | 85,314-150,000 rows in 2.2-3.7 min | **657-669 rows/sec** | High |
| Elasticsearch raw to `standardized` before tuning | Four partitions combined | approximately **15 rows/sec** | High |
| Elasticsearch raw to `standardized` after indexes, page 1,000 | Short measured interval | approximately **80-100 rows/sec** | Medium |
| Elasticsearch raw to `standardized` after indexes, page 5,000 | Sustained checkpoint windows | approximately **150-190 rows/sec** | High |
| PostgreSQL `standardized` to ClickHouse | Latest successful run: 7,222,494 loader rows in 683 sec | **10,573 rows/sec** | High |
| PostgreSQL `standardized` to ClickHouse, observed successful range | Six successful runs | **9,921-16,911 rows/sec** | High |

The current bottleneck is Elasticsearch-to-standardized PostgreSQL merge work,
not ClickHouse publication. Database-connector volume throughput remains
unmeasured because the available successful connector runs contain only three
records.

## Database connector

### Available evidence

| Sync run | Records | Bytes | End-to-end duration | Result |
|---|---:|---:|---:|---|
| `a5d3ae97-ea1f-4e01-abbd-092e4dce6e92` | 3 | 9,822 | 1,036 sec | Succeeded |
| `7a22e697-5609-4675-a02a-8834f4365193` | 3 | 9,822 | 1,015 sec | Succeeded |
| `ba896a9c-a028-41f0-8332-17c50ff5a5b8` | 3 | 9,822 | 444 sec | Succeeded |

The 15-17 minute recent durations are dominated by task dispatch, downstream
ingestion/stitching, polling and finalization. Dividing three rows by that time
does not measure connector extraction capacity.

The connector worker is configured for 5,000-row fetch/COPY batches and up to
100,000 rows or 128 MiB per Parquet part, but a controlled 100k-1M row database
sync is still required before publishing a connector rows/sec or GiB/hour SLA.

## Manual import file

End-to-end here means source landing batch creation through canonical output
publication. It includes file validation and canonical output generation, but
does not include a later identity-stitching run.

| Input rows | Input bytes | End-to-end time | Rows/sec | MiB/sec |
|---:|---:|---:|---:|---:|
| 150,000 | 115,431,387 | 273.6 sec | **548** | **0.40** |
| 85,314 | 70,972,191 | 161.6 sec | **528** | **0.42** |
| 65,000 | 49,969,693 | 117.1 sec | **555** | **0.41** |
| 65,000 | 49,969,693 | 130.6 sec | **498** | **0.37** |
| 65,000 | 49,969,693 | 218.5 sec | **297** | **0.22** |

The two latest representative runs sustain approximately 528-548 rows/sec.
At the observed 0.40-0.42 MiB/sec, a 1 GiB file of similar shape would take
approximately 41-43 minutes through canonical output publication.

The canonical-output sub-phase itself processed the latest two files at 657-669
rows/sec. `control.ingestion_batch.started_at` and `completed_at` are identical
for these runs, so the database-load sub-phase cannot be timed independently
from the current audit columns.

## Elasticsearch raw to PostgreSQL standardized

### Current runtime

| Setting | Value |
|---|---:|
| Fargate CPU | 2 vCPU |
| Fargate memory | 4,096 MiB |
| Page size | 5,000 rows |
| Load statement timeout | 1,800 sec |
| Merge lock timeout | 1,800 sec |
| Active partitions/tasks | 4 |

The source-system advisory lock still permits only one merge transaction at a
time. Other partition workers extract concurrently but queue for the same
`source_system_id` lock.

### Measured improvement

| Configuration | Aggregate throughput | Relative to original |
|---|---:|---:|
| Page 1,000, missing lookup indexes | approximately 15 rows/sec | 1.0x |
| Page 1,000, valid `source_record_id` indexes | approximately 80-100 rows/sec | 5-7x |
| Page 5,000, valid indexes | approximately 150-190 rows/sec | 10-13x |

The two lookup indexes are valid, live and used by PostgreSQL:

| Index | Size | Observed scans at verification |
|---|---:|---:|
| `ix_order_version_default_source_record` | 76 MB | 241,838 |
| `ix_order_item_version_default_source_record` | 113 MB | 222,981 |

Worker logs at page size 5,000 show approximately 30-35 seconds of merge work
after lock acquisition and 60-88 seconds waiting for the source-system lock.
Checkpoint observation over a sustained window produced roughly 190 rows/sec
across the four partitions.

### Current volume and ETA

At the 2026-09-04 snapshot:

| Measure | Rows |
|---|---:|
| Elasticsearch expected documents | 105,121,571 |
| Elasticsearch documents restored to PostgreSQL raw | 82,245,508 |
| PostgreSQL standardized source records | 4,089,308 at the earlier full snapshot; actively increasing |
| Raw restored but not yet standardized | approximately 78.2M at that snapshot |
| Elasticsearch documents not yet restored | 22,876,063 |

At 150-190 rows/sec, the already-restored backlog is approximately 4.8-6.0
days. Processing the full expected Elasticsearch volume is approximately
6.1-7.7 days if restoration remains ahead of standardization. Throughput can
decline as current tables and indexes grow, so these are operational estimates,
not an SLA.

### Remaining bottleneck

Four PostgreSQL worker sessions use one advisory lock keyed by
`source_system_id`. Before the index fix, transactions held the lock for 4-6
minutes. After the fix and page-size increase, lock waits fell to roughly 60-88
seconds, but three workers still wait while one merges.

The next major optimization requires worker-code changes:

1. load immutable lineage/version rows in parallel;
2. acquire the source-system lock only for short current-winner updates; or
3. use a dedicated backfill mode that bulk-rebuilds current tables periodically
   instead of after every page.

Do not remove the lock without deterministic key ordering and deadlock tests.

## PostgreSQL standardized to ClickHouse

### Successful run history

| Run ID | Loader rows | Duration | Rows/sec |
|---|---:|---:|---:|
| `643fdbaa-c39c-4186-8938-04116222936e` | 7,222,494 | 683 sec | **10,573** |
| `1a743e22-6762-4d0f-b563-e4a033c7ccb6` | 4,522,656 | 267 sec | **16,911** |
| `77979b39-e33f-4ffb-98eb-c9cc9bad9535` | 2,645,450 | 267 sec | **9,921** |
| `1fa9c458-5cc3-4062-beab-1640004a9bd7` | 2,629,155 | 192 sec | **13,662** |
| `4dfdca15-e509-489d-953c-1f063722f752` | 1,920,251 | 140 sec | **13,756** |
| `abfe5bf6-0cd2-468a-a880-da971da05612` | 2,316,746 | 189 sec | **12,248** |

`rows_loaded` includes incremental history plus full mutable key/product
snapshots, so it measures loader work rather than only net-new final rows.

The latest published final state at that benchmark point was:

| Final table | Rows |
|---|---:|
| `purchase_transaction` | 1,608,264 |
| `purchase_transaction_items` | 1,499,325 |
| `product_master` | 1,977 |
| `store_master` | 424 |
| `customer_profile` | 436,623 |

ClickHouse throughput is over 50 times the current Elasticsearch
standardization production rate. It is not the end-to-end bottleneck and can
catch up incrementally while upstream continues.

## 1 GiB planning view

Rows/sec is more reliable than MiB/sec across these pipelines because the
record shapes differ substantially.

| Pipeline scope | Current planning estimate | Confidence |
|---|---:|---|
| Database connector end to end | Unknown until a production-sized run | Low |
| Manual import file to canonical output | **41-78 min/GiB** for observed file shapes | Medium-High |
| Elasticsearch raw to standardized | Use **150-190 rows/sec**; byte estimate not reliable | High for current row shape |
| Standardized to ClickHouse | **9.9k-16.9k loader rows/sec** | High |

Do not use the previous blanket 2-4 hours/GiB SLA. Connector DB, manual file,
Elasticsearch and ClickHouse have different fixed costs and row shapes and must
be budgeted separately.

## References

- Database connector runtime data: `connector.sync_run` and
  `connector.sync_partition`.
- Manual import data: `control.source_landing_batch`,
  `control.source_landing_file` and `control.ingestion_batch`.
- Elasticsearch progress: `control.standardization_run`,
  `control.standardization_checkpoint` and PostgreSQL activity statistics.
- Standardization runtime configuration:
  `cdk_stacks/standardization_runtime_stack.py` and `env.yaml`.
- ClickHouse audit data: `standardized_raw.etl_run`, `etl_checkpoint` and
  `etl_reconciliation`.
- ClickHouse loader:
  `/Users/kmac15/PycharmProjects/identity-resolution/design/standardized-clickhouse-transfer/transfer.py`.

These measurements are operational evidence, not contractual SLAs. Repeat the
database connector benchmark with at least 100k rows before assigning a
connector throughput target.
