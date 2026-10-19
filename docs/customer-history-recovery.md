# Safe canonical history recovery

## Scope and gates

Use `scripts/recover_customer_history.py`, **not** the old release prototype.
Apply migration `020_customer_history_recovery.sql` after 019, while ingestion,
connector and admission writers are paused. No existing migration/runtime file
is changed by this recovery implementation.

The recovery only inserts absent `raw.customer` observations and an immutable
`control.customer_history_recovery` ledger. It never writes current identity,
membership, normalized profiles, quality, admission or ECS. Each batch owns a
transaction. Failures roll back that batch; earlier committed batches remain.
There is deliberately no destructive undo of committed immutable evidence.

Required authoritative lineage:
- Completed ingestion linked to a PUBLISHED landing batch.
- Matching ingestion/landing SHA256, object key and selected row counts.
- Landing output bucket/key and exact immutable membership for every selected key.
- Ingestion `metadata.customer_history_artifact` containing
  `{"format":"canonical-csv-v1","version_id":"<recorded-S3-VersionId>"}`.

Alternatively, explicitly opt into **CHECKSUM_BOUND_UNVERSIONED_RECONSTRUCTION**
for a canonical `.csv` with no recorded VersionId. The locked ingestion and
landing SHA256 must agree and the supplied bytes must match that checksum.
No metadata repair or fabricated VersionId is required or permitted. A recorded
non-null version conflicts with this mode. The ledger records null `version_id`,
`original_snapshot_proven=false`, the distinct evidence kind, and archive path/hash.
History rows retain `CANONICAL_RECONSTRUCTION`, never original-snapshot provenance.

Use `--checksum-bound-unversioned --archive-dir /private/recovery/archive`
in place of `--artifact-version-id`. Verified bytes are archived under their SHA256
using exclusive creation, mode 0600, fsync and read-back hash verification before
SQL inserts. Existing corrupt archives fail closed. Retain/protect this directory;
it is a local evidence archive, not an S3 Object Lock claim. Archives intentionally
survive SQL rollback (including audit mode); DB history and ledger remain atomic.
Repeat using the same archive path and code revision for ledger idempotency.

Connector Parquet remains explicitly unsupported **by the recovery writer**, not
silently parsed as CSV. A separate read-only offline evidence auditor now checks
local checksum-addressed Parquet bytes without marking any batch recovered.
Verified source contract: connector-worker `storage.py` publishes manifest_version=1,
format=parquet, records_published, bytes_written and per-part bucket/key,
checksum_sha256, row_count, byte_count. `loader.py` selects DISTINCT ON source key
ordered by source_row_number DESC. Safe support therefore requires verifying the
recorded manifest hash plus every part hash/size/schema and reproducing selection,
not simply equating published rows with loaded rows. Archived real manifests
and parts were later available to the **offline auditor**; this is not a recovery
writer, an exact membership check, or a live PostgreSQL typed comparison.

Fetch the specific recorded S3 version separately with approved credentials,
check the returned VersionId, and keep downloaded bytes private. This utility
has no AWS client, default profile, Secrets Manager call or .env loader.

## Offline historical evidence audit (read-only)

`python scripts/audit_customer_history_evidence.py --scope <original-preflight.json>
--latest-snapshot <later-scope.json> --private-parquet-archive <private-sha256-dir>`
reads existing metadata snapshots and local checksum-addressed connector bytes.
It has no AWS/DB clients, no profile access, and emits only batch IDs, counts,
status codes and a SHA256 (never selected rows, source keys or payloads). Keep
private Parquet parts in a protected archive; do not put evidence in the repo.
The original scope and later snapshot must preserve exact recorded lineage and
ordered capture times. Missing/duplicate batch IDs or changed lineage fail closed.
A published manifest and **every** part are verified by SHA256, size, schema,
row count and last-source-row selection using the pinned Parquet parser. Historical
membership counts must match in *both* snapshots. A missing membership remains
blocked even if the same source keys exist in current profiles. It does not infer
app IDs, derive identity from current attributes, check exact member key identity,
compare PostgreSQL typed columns, or write recovery ledgers. Fully populated but
unledgered history is `EXISTING_TYPED_COMPARISON_PENDING`, not VERIFIED. Ledger
statuses are reported as snapshot observations requiring fresh live readback.
CSV bytes are deliberately not fetched or validated by this bounded audit; CSV
candidates remain `BLOCKED_CSV_EVIDENCE_NOT_AUDITED`, not unrecoverable.

On retained September 18 metadata snapshots, the auditor classified the original
120 batches: 14 Parquet artifact-verified but missing typed history (245039 typed
rows at that snapshot), 5 Parquet artifact-verified with existing typed rows but
no exact typed comparison, 63 ledger-reported (not re-verified here), 23 missing
historical membership, 3 lineage mismatches, 4 incomplete lineage/status and 8
CSV candidates needing a separate bytes/typed audit. These are **snapshot-only**
classifications, not current production readiness; a later live read-only DB check
must refresh them before any authorization. `raw.customer` typed history is distinct
from latest normalized `staging.customer_profile`; this audit does not address the
separately reported 169914 latest normalized gap. Do not infer recovery completion
from the snapshot's total typed gap or from the Parquet parser pass.

## Invocation

Use a configured libpq service (credentials outside command arguments):

```sh
python scripts/recover_customer_history.py \
  --service recovery-reviewed \
  --batch-id <ingestion-uuid> \
  --artifact /private/path/canonical.csv \
  --artifact-version-id <recorded-version>
```

Default mode fully exercises inserts/comparisons/ledger and then **rolls back**;
it is not a read-only SQL transaction and requires recovery write privileges.
Add `--apply` only after audit and deployment authorization. Invoke separately
for each batch and stop on nonzero exit. Output contains counts/checksum/status
or fixed error codes, never rows, DSNs or server DETAIL. Secure PostgreSQL server
logs separately: server error logging is outside the CLI's redaction boundary.

The frozen parser records its version and SHA256; the ledger also records recovery
code SHA256, artifact SHA256/version/location, landing lineage, typed digest,
exact membership digest, row counts and PostgreSQL version. Changed provenance
on rerun is rejected rather than rewriting the ledger. Reconstruction is not
proof of the original historical transformation. Duplicate keys, rejected rows,
duplicate headers and schema drift are rejected conservatively.

Comparisons use PostgreSQL typed values and symmetric exact set comparisons,
including keys, NULLs, dates, nested JSON and raw_payload. Only ingestion clocks
and snapshot metadata are excluded. Existing evidence is never overwritten.
Table locks serialize competing history/current/membership/lineage writers;
lock timeout is 5 seconds and statement timeout 120 seconds. This conservative
maintenance utility loads one artifact in memory and is not a live-scale
performance benchmark. Validate resource limits on representative offline data.

## Portable local tests

`tests/integration/fixtures/customer_history_base.sql` builds the synthetic
pre-019 contract, including all parser and standardizer fields. Neither fixture
nor runtime test reads the prototype `postgres` database. Runtime integration
still requires the sibling fargate-user-stitching checkout and its dependencies.
Run serially: the fixtures reset disposable schemas, never use pytest-xdist.

From aws-flex-deploy:

```sh
.venv/bin/python -m pytest tests/unit -q
CDP_HISTORY_TEST_DSN='host=127.0.0.1 port=55441 user=postgres dbname=cdp_history_regression' \
  ../fargate-user-stitching/.venv/bin/python -m pytest \
  tests/unit/test_history_recovery.py \
  tests/integration/test_history_recovery.py \
  tests/integration/test_customer_history_runtime.py \
  tests/integration/test_customer_history.py -q
```

Verified locally for this revision: focused recovery/history/runtime suite 45 passed
on disposable PostgreSQL port 55441. The stronger composite identity FK fixture
now expects rejection at insertion rather than weakening the constraint.
Coverage includes equal-count typed/key conflicts, exact partial replay,
post-insert trigger corruption, ledger-write failure rollback, changed provenance,
wrong version, missing membership, schema drift, audit rollback, ledger immutability,
and delayed normalization without rewinding current identity. No AWS/live DB was
accessed and no customer payloads were used.
