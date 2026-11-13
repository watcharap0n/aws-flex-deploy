"""Intake v2 dataset path for the landing Lambda (mapping v2 projections).

Input:   data_sources/<source_code>/<batch_ref>/datasets/<dataset_id>/<file>.(csv|xlsx|json)
The dataset is an upload dataset (connector.connection.definition_key = 'file_upload') whose
published mapping v2 splits each source row into CUSTOMER / TRANSACTION / EVENT / ASSET /
RELATIONSHIP records (lib: cdp_intake_mapping, vendored).

Routes:
- CUSTOMER     -> canonical customer CSV written to the EXISTING customer landing path
                  data_sources/<source_code>/<handoff_batch_ref>/customers/customer.csv, so the
                  unchanged customer route (landing -> ingestion -> stitcher -> publication)
                  takes over.
- other types  -> raw/<source_code>/intake/<batch_ref>/<dataset_id>/<entity>.ndjson (lineage;
                  not .csv, so ingestion is not triggered) and INSERT into ClickHouse cdp_intake.*
Status per (batch x entity): control.intake_batch_output (migration 058).

Large files: rows are read and projected one at a time (csv module / openpyxl read-only), never
as a whole frame. Facts go out in ~8 MB NDJSON chunks, each sent to ClickHouse and appended to the
lineage object (S3 multipart) on a small I/O thread pool while parsing continues. Customers are
de-duplicated (last occurrence wins, first-seen order) in memory, spilling to SQLite on /tmp past
INTAKE_CUSTOMER_SPILL_KEYS distinct keys. Memory stays flat; /tmp holds the source (already
spooled by the handler) plus the customer spill.

Environment: INTAKE_CLICKHOUSE_SECRET_ID {url, username, password, ca_pem?},
             INTAKE_HMAC_SECRET_ID {key}  (per-app key = HMAC(key, app_id); hashes national IDs)
             INTAKE_IO_WORKERS (default 4), INTAKE_CUSTOMER_SPILL_KEYS (default 200000)
"""

from __future__ import annotations

import csv
import hashlib
import hmac
import io
import json
import logging
import os
import re
import sqlite3
import ssl
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator, Mapping
from concurrent.futures import Future, ThreadPoolExecutor
from typing import Any

from cdp_intake_mapping import Projector, Transforms, normalize_headers
from person_observation import ObservationWriter

LOGGER = logging.getLogger()
DATASET_SEGMENT = "datasets"
FACT_ENTITIES = ("TRANSACTION", "EVENT", "ASSET", "RELATIONSHIP")
BATCH_REF_APP = re.compile(r"^(\d{4}-\d{2}-\d{2}-\d{6})-(\d+)-([0-9a-f]{12})$")
UUID_PATTERN = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
LINEAGE = ("app_id", "source_code", "dataset_id", "batch_ref", "mapping_version_id", "_version")
FACT_COLUMNS = {
    "TRANSACTION": ("source_transaction_id", "source_customer_id", "transaction_type", "status",
                    "occurred_at", "amount", "currency", "discount_amount", "quantity", "channel",
                    "location_id", "items", "attributes", "source_updated_at", "_projection"),
    "EVENT": ("source_event_id", "source_customer_id", "anonymous_id", "event_name",
              "event_category", "occurred_at", "channel", "object_type", "object_id", "value",
              "attributes", "_projection"),
    "ASSET": ("source_asset_id", "asset_type", "name", "identifiers", "status", "attributes",
              "source_updated_at", "_projection"),
    "RELATIONSHIP": ("source_relationship_id", "from_type", "from_id", "relation_type", "to_type",
                     "to_id", "role", "valid_from", "valid_to", "confidence", "attributes",
                     "_projection"),
}

DATASET_SQL = """
SELECT d.dataset_id::text, d.workspace_id::text, d.target_entity, c.source_code,
       c.definition_key, w.app_id, m.mapping_version_id::text, m.status, m.mapping_json
FROM connector.dataset d
JOIN connector.connection c ON c.connection_id = d.connection_id
JOIN platform.workspace w ON w.workspace_id = d.workspace_id
LEFT JOIN connector.mapping_version m ON m.mapping_version_id = d.active_mapping_version_id
WHERE d.dataset_id = %s::uuid
"""

UPSERT_STATUS_SQL = """
INSERT INTO control.intake_batch_output (
    dataset_id, workspace_id, app_id, source_code, batch_ref, object_key, object_etag,
    mapping_version_id, target_entity, route, status, source_rows, output_rows, rejected_rows,
    skipped_rows, output_key, handoff_batch_ref, error)
VALUES (%(dataset_id)s::uuid, %(workspace_id)s::uuid, %(app_id)s, %(source_code)s,
        %(batch_ref)s, %(object_key)s, %(object_etag)s, %(mapping_version_id)s::uuid,
        %(target_entity)s, %(route)s, %(status)s, %(source_rows)s, %(output_rows)s,
        %(rejected_rows)s, %(skipped_rows)s, %(output_key)s, %(handoff_batch_ref)s, %(error)s)
ON CONFLICT (dataset_id, batch_ref, object_key, target_entity) DO UPDATE SET
    object_etag = EXCLUDED.object_etag, mapping_version_id = EXCLUDED.mapping_version_id,
    route = EXCLUDED.route, status = EXCLUDED.status, source_rows = EXCLUDED.source_rows,
    output_rows = EXCLUDED.output_rows, rejected_rows = EXCLUDED.rejected_rows,
    skipped_rows = EXCLUDED.skipped_rows, output_key = EXCLUDED.output_key,
    handoff_batch_ref = EXCLUDED.handoff_batch_ref, error = EXCLUDED.error, updated_at = now()
"""


class DatasetObjectError(ValueError):
    """The upload cannot be processed as sent (wrong dataset, app or mapping)."""


def parse_dataset_parts(parts: list[str]) -> tuple[str, str, str, str] | None:
    """[source_code, batch_ref, 'datasets', dataset_id, file] -> parts, else None."""
    if len(parts) != 5 or parts[2] != DATASET_SEGMENT:
        return None
    source_code, batch_ref, _segment, dataset_id, filename = parts
    if not UUID_PATTERN.fullmatch(dataset_id):
        raise ValueError("dataset_id in the upload path must be a lowercase UUID")
    return source_code, batch_ref, dataset_id, filename


def batch_app_id(batch_ref: str) -> str:
    match = BATCH_REF_APP.fullmatch(batch_ref)
    if not match:
        raise DatasetObjectError(
            "batch_ref must be YYYY-MM-DD-HHMMSS-<numeric_app_id>-<12 lowercase hex>")
    return match.group(2)


def handoff_batch_ref(batch_ref: str, dataset_id: str) -> str:
    """Customer hand-off gets its own batch (one customer file per batch on the old route)
    while keeping the batch_ref format, timestamp and app id."""
    match = BATCH_REF_APP.fullmatch(batch_ref)
    assert match
    digest = hashlib.sha256(f"{batch_ref}|{dataset_id}".encode()).hexdigest()[:12]
    return f"{match.group(1)}-{match.group(2)}-{digest}"


def iter_rows(content: Any, file_format: str) -> tuple[list[str], Iterator[dict[str, Any]]]:
    """Header plus a lazy row iterator; cells are source text (None for a missing trailing cell),
    headers normalised the same way the API stored them. Blank lines are skipped."""
    content.seek(0)
    # Python 3.10's SpooledTemporaryFile has no readable() or seekable(); read its underlying file instead.
    raw = getattr(content, "_file", content) if isinstance(content, tempfile.SpooledTemporaryFile) else content
    if file_format == "csv":
        text = io.TextIOWrapper(raw, encoding="utf-8-sig", newline="")
        reader = csv.reader(text)
        try:
            raw_headers = next(reader)
        except StopIteration:
            raise DatasetObjectError("file is empty") from None
        headers = normalize_headers(raw_headers)
        width = len(headers)

        def csv_rows() -> Iterator[dict[str, Any]]:
            for cells in reader:
                if not cells:
                    continue
                if len(cells) < width:
                    cells = cells + [None] * (width - len(cells))
                yield dict(zip(headers, cells))
        return headers, csv_rows()
    if file_format == "xlsx":
        from openpyxl import load_workbook

        book = load_workbook(raw, read_only=True, data_only=True)
        sheet = book.worksheets[0]
        values = sheet.iter_rows(values_only=True)
        header_row = next(values, None)
        if header_row is None:
            raise DatasetObjectError("file is empty")
        headers = normalize_headers(["" if v is None else str(v) for v in header_row])

        def xlsx_rows() -> Iterator[dict[str, Any]]:
            try:
                for cells in values:
                    if cells is None or all(v is None or v == "" for v in cells):
                        continue
                    yield {h: (None if v is None else str(v)) for h, v in zip(headers, cells)}
            finally:
                book.close()
        return headers, xlsx_rows()
    if file_format == "json":
        records = json.loads(content.read().decode("utf-8-sig"))
        if isinstance(records, dict):
            records = records.get("records") or records.get("data") or []
        if not isinstance(records, list) or not all(isinstance(r, dict) for r in records):
            raise DatasetObjectError("JSON upload must be a list of objects")
        raw: list[str] = []
        for record in records:
            raw.extend(k for k in record if k not in raw)
        headers = normalize_headers(raw)
        return headers, ({h: r.get(k) for h, k in zip(headers, raw)} for r in records)
    raise DatasetObjectError(f"unsupported file format {file_format}")


def read_rows(content: Any, file_format: str) -> tuple[list[str], list[dict[str, Any]]]:
    """Whole-file variant of iter_rows, for small inputs and tests."""
    headers, rows = iter_rows(content, file_format)
    return headers, list(rows)


def tenant_transforms(hmac_secret: bytes, app_id: str) -> Transforms:
    return Transforms(hmac_key=hmac.new(hmac_secret, str(app_id).encode(), hashlib.sha256).digest())


def customer_csv(records: list[dict[str, Any]], columns: list[str]) -> tuple[bytes, int]:
    """Canonical customer CSV for the old route; one row per key (the last occurrence wins)."""
    dedup = CustomerRows(columns)
    for record in records:
        dedup.add(record)
    body = b"".join(dedup.chunks())
    unique = dedup.unique()
    dedup.close()
    return body, unique


class CustomerRows:
    """Last occurrence per source_customer_id wins; output keeps first-seen order (same contract as
    the old in-memory dict). Up to `spill_keys` distinct keys stay in memory, beyond that every
    occurrence is appended to SQLite on local disk and resolved with one GROUP BY at the end."""

    def __init__(self, columns: list[str], spill_keys: int | None = None):
        self.columns = columns
        self.spill_keys = spill_keys or int(os.getenv("INTAKE_CUSTOMER_SPILL_KEYS", "200000"))
        self.memory: dict[str, list[Any]] = {}
        self.db: sqlite3.Connection | None = None
        self.dir: str | None = None
        self.pending: list[tuple[int, str, str]] = []
        self.ordinal = 0
        self._buffer = io.StringIO()
        self._writer = csv.writer(self._buffer, lineterminator="\n")

    def _line(self, record: dict[str, Any]) -> str:
        self._buffer.seek(0)
        self._buffer.truncate()
        self._writer.writerow(["" if record.get(k) is None else record[k] for k in self.columns])
        return self._buffer.getvalue()

    def add(self, record: dict[str, Any]) -> None:
        self.ordinal += 1
        key, line = str(record["source_customer_id"]), self._line(record)
        if self.db is None:
            seen = self.memory.get(key)
            if seen is not None:
                seen[1] = line
                return
            self.memory[key] = [self.ordinal, line]
            if len(self.memory) > self.spill_keys:
                self._spill()
            return
        self.pending.append((self.ordinal, key, line))
        if len(self.pending) >= 20000:
            self._flush()

    def _spill(self) -> None:
        self.dir = tempfile.mkdtemp(prefix="intake-customers-")
        self.db = sqlite3.connect(os.path.join(self.dir, "customers.db"), isolation_level=None)
        self.db.execute("PRAGMA journal_mode=OFF")
        self.db.execute("PRAGMA synchronous=OFF")
        self.db.execute("CREATE TABLE occ (ord INTEGER PRIMARY KEY, key TEXT NOT NULL, line TEXT NOT NULL)")
        # Spilled entries keep their first ordinal with the latest line seen so far; later
        # occurrences get larger ordinals, so MAX(ord) is still the last occurrence.
        self.pending = [(v[0], k, v[1]) for k, v in self.memory.items()]
        self.memory = {}
        self._flush()

    def _flush(self) -> None:
        if self.pending and self.db is not None:
            self.db.execute("BEGIN")
            self.db.executemany("INSERT INTO occ (ord, key, line) VALUES (?, ?, ?)", self.pending)
            self.db.execute("COMMIT")
            self.pending = []

    def unique(self) -> int:
        if self.db is None:
            return len(self.memory)
        self._flush()
        return int(self.db.execute("SELECT COUNT(DISTINCT key) FROM occ").fetchone()[0])

    def chunks(self, size: int = 8 * 1024 * 1024) -> Iterator[bytes]:
        """Header plus one line per customer, in pieces of about `size` bytes."""
        parts: list[str] = [",".join(self.columns) + "\n"]
        length = len(parts[0])
        if self.db is None:
            lines: Any = (v[1] for v in self.memory.values())
        else:
            self._flush()
            self.db.execute("CREATE INDEX occ_key ON occ (key, ord)")
            lines = (row[0] for row in self.db.execute(
                "SELECT o.line FROM (SELECT key, MIN(ord) AS f, MAX(ord) AS l FROM occ GROUP BY key) g "
                "JOIN occ o ON o.ord = g.l ORDER BY g.f"))
        for line in lines:
            parts.append(line)
            length += len(line)
            if length >= size:
                yield "".join(parts).encode("utf-8")
                parts, length = [], 0
        if parts:
            yield "".join(parts).encode("utf-8")

    def close(self) -> None:
        if self.db is not None:
            self.db.close()
            self.db = None
        if self.dir:
            import shutil
            shutil.rmtree(self.dir, ignore_errors=True)
            self.dir = None


class IoPool:
    """Small thread pool for network writes (ClickHouse inserts, S3 parts) so they overlap with
    parsing. At most `inflight` tasks are queued, which bounds the memory held by pending bodies."""

    def __init__(self, workers: int | None = None, inflight: int | None = None):
        workers = workers or int(os.getenv("INTAKE_IO_WORKERS", "4"))
        self.executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="intake-io")
        self.slots = threading.Semaphore(inflight or workers * 2)

    def submit(self, fn: Callable[..., Any], *args: Any) -> Future:
        self.slots.acquire()
        future = self.executor.submit(fn, *args)
        future.add_done_callback(lambda _f: self.slots.release())
        return future

    def close(self) -> None:
        self.executor.shutdown(wait=True)


class S3Writer:
    """One S3 object written in PART_BYTES pieces: a single put_object when it stays small,
    multipart (parts uploaded on the I/O pool) when it grows."""

    PART_BYTES = 8 * 1024 * 1024

    def __init__(self, s3_client: Any, bucket: str, key: str, pool: IoPool, content_type: str | None = None):
        self.s3, self.bucket, self.key, self.pool = s3_client, bucket, key, pool
        self.extra = {"ContentType": content_type} if content_type else {}
        self.buffer = bytearray()
        self.upload_id: str | None = None
        self.parts: list[tuple[int, Future]] = []
        self.size = 0

    def write(self, data: bytes) -> None:
        self.buffer += data
        self.size += len(data)
        while len(self.buffer) >= self.PART_BYTES:
            self._part(bytes(self.buffer[:self.PART_BYTES]))
            del self.buffer[:self.PART_BYTES]

    def _part(self, body: bytes) -> None:
        if self.upload_id is None:
            self.upload_id = self.s3.create_multipart_upload(Bucket=self.bucket, Key=self.key, **self.extra)["UploadId"]
        number = len(self.parts) + 1
        self.parts.append((number, self.pool.submit(
            lambda n=number, b=body: self.s3.upload_part(Bucket=self.bucket, Key=self.key, UploadId=self.upload_id,
                                                         PartNumber=n, Body=b)["ETag"])))

    def close(self) -> None:
        if self.upload_id is None:
            self.s3.put_object(Bucket=self.bucket, Key=self.key, Body=bytes(self.buffer), **self.extra)
            return
        if self.buffer:
            self._part(bytes(self.buffer))
            self.buffer = bytearray()
        try:
            parts = [{"PartNumber": n, "ETag": f.result()} for n, f in self.parts]
            self.s3.complete_multipart_upload(Bucket=self.bucket, Key=self.key, UploadId=self.upload_id,
                                              MultipartUpload={"Parts": parts})
        except Exception:
            self.abort()
            raise

    def abort(self) -> None:
        if self.upload_id is not None:
            try:
                self.s3.abort_multipart_upload(Bucket=self.bucket, Key=self.key, UploadId=self.upload_id)
            except Exception:  # noqa: BLE001
                LOGGER.warning("abort_multipart_failed key=%s", self.key)
            self.upload_id = None


INSERT_CHUNK_BYTES = 8 * 1024 * 1024


class ClickHouseSink:
    def __init__(self, config: Mapping[str, Any]):
        self.url = str(config["url"]).rstrip("/") + "/"
        self.user, self.password = str(config["username"]), str(config["password"])
        # Prod ClickHouse is TLS with a private root CA: pin it (ca_pem), never skip verify.
        self.context = (ssl.create_default_context(cadata=config["ca_pem"])
                        if config.get("ca_pem") else None)

    def _post(self, query: str, body: bytes | None = None) -> str:
        params = {"query": query, "date_time_input_format": "best_effort",
                  "input_format_skip_unknown_fields": "1",
                  "input_format_json_read_numbers_as_strings": "1"}
        req = urllib.request.Request(self.url + "?" + urllib.parse.urlencode(params),
                                     data=body or b"", method="POST",
                                     headers={"X-ClickHouse-User": self.user,
                                              "X-ClickHouse-Key": self.password})
        with urllib.request.urlopen(req, timeout=120, context=self.context) as response:
            return response.read().decode()

    def insert(self, entity: str, rows: list[dict[str, Any]]) -> None:
        # Chunked: prod ClickHouse sits behind a 16 MB request body cap.
        columns = (*LINEAGE, *FACT_COLUMNS[entity])
        query = f"INSERT INTO cdp_intake.{entity.lower()} FORMAT JSONEachRow"
        chunk: list[bytes] = []
        size = 0
        for row in rows:
            line = (json.dumps({c: row.get(c) for c in columns if row.get(c) is not None},
                               ensure_ascii=False) + "\n").encode()
            if chunk and size + len(line) > INSERT_CHUNK_BYTES:
                self._post(query, b"".join(chunk))
                chunk, size = [], 0
            chunk.append(line)
            size += len(line)
        if chunk:
            self._post(query, b"".join(chunk))

    def insert_ndjson(self, entity: str, body: bytes) -> None:
        """Insert pre-encoded JSONEachRow lines (one body <= INSERT_CHUNK_BYTES). Keys outside the
        table are skipped by input_format_skip_unknown_fields, so lineage lines can be sent as-is."""
        if body:
            self._post(f"INSERT INTO cdp_intake.{entity.lower()} FORMAT JSONEachRow", body)

    def count(self, entity: str, dataset_id: str, batch_ref: str, version: int) -> int:
        query = (f"SELECT count() FROM cdp_intake.{entity.lower()} WHERE dataset_id = "
                 f"toUUID('{dataset_id}') AND batch_ref = '{batch_ref}' AND _version = {version}")
        return int(self._post(query + " FORMAT TSVRaw").strip() or 0)


def load_context(cursor: Any, dataset_id: str) -> dict[str, Any]:
    cursor.execute(DATASET_SQL, (dataset_id,))
    row = cursor.fetchone()
    if row is None:
        raise DatasetObjectError("unknown dataset_id")
    keys = ("dataset_id", "workspace_id", "target_entity", "source_code", "definition_key",
            "app_id", "mapping_version_id", "mapping_status", "mapping_json")
    context = dict(zip(keys, row))
    if isinstance(context["mapping_json"], str):
        context["mapping_json"] = json.loads(context["mapping_json"])
    return context


def process_dataset_object(
    *,
    source: Any,
    downloaded: Any,
    connection: Any,
    s3_client: Any,
    output_bucket: str,
    output_root_prefix: str,
    input_prefix: str,
    customer_columns: list[str],
    hmac_secret: bytes,
    sink: ClickHouseSink,
    clock: Callable[[], float] = time.time,
) -> dict[str, Any]:
    with connection.cursor() as cursor:
        ctx = load_context(cursor, source.dataset_id)
    if ctx["definition_key"] != "file_upload":
        raise DatasetObjectError("dataset does not belong to a file_upload connection")
    if ctx["source_code"] != source.source_code:
        raise DatasetObjectError("source_code in the path does not match the dataset connection")
    if not ctx["app_id"] or batch_app_id(source.batch_ref) != str(ctx["app_id"]):
        raise DatasetObjectError("app id in batch_ref does not match the dataset workspace")
    mapping = ctx["mapping_json"] or {}
    if ctx["mapping_status"] != "PUBLISHED" or mapping.get("version") != 2:
        raise DatasetObjectError("dataset has no published mapping v2")

    started = clock()
    headers, rows = iter_rows(downloaded.content, source.file_format)
    projector = Projector(mapping, tenant_transforms(hmac_secret, str(ctx["app_id"])))
    version = int(clock() * 1_000_000)
    entity_of = {p["name"]: p["target_entity"] for p in mapping["projections"]}
    entities = sorted(set(entity_of.values()))
    facts = [e for e in entities if e != "CUSTOMER"]
    raw_prefix = (f"{output_root_prefix}{source.source_code}/intake/{source.batch_ref}/"
                  f"{ctx['dataset_id']}/")
    lineage = {"app_id": str(ctx["app_id"]), "source_code": source.source_code,
               "dataset_id": ctx["dataset_id"], "batch_ref": source.batch_ref,
               "mapping_version_id": ctx["mapping_version_id"], "_version": version}
    observation_writer = ObservationWriter(sink, lineage, source.key,
        os.getenv("RELATED_OBSERVATIONS_ENABLED", "false").lower() == "true")
    chunk_bytes = INSERT_CHUNK_BYTES

    pool = IoPool()
    writers = {e: S3Writer(s3_client, output_bucket, raw_prefix + f"{e.lower()}.ndjson", pool) for e in facts}
    issues: S3Writer | None = None          # rejects + dropped values, created on first use
    buffers = {e: bytearray() for e in facts}
    written = {e: 0 for e in facts}
    inserts: dict[str, list[Future]] = {e: [] for e in facts}
    customers = CustomerRows(customer_columns) if "CUSTOMER" in entities else None
    rejected: dict[str, int] = {}
    reasons: dict[str, dict[str, int]] = {}  # entity -> reason (field: problem) -> rows
    skipped: dict[str, int] = {}
    warned_rows = 0
    source_rows = 0
    errors: dict[str, str] = {}

    def flush(entity: str) -> None:
        body = bytes(buffers[entity])
        buffers[entity] = bytearray()
        if body and entity not in errors:
            inserts[entity].append(pool.submit(sink.insert_ndjson, entity, body))
            writers[entity].write(body)

    def note(item: dict[str, Any]) -> None:
        nonlocal issues
        if issues is None:
            issues = S3Writer(s3_client, output_bucket, raw_prefix + "rejects.ndjson", pool)
        issues.write((json.dumps(item, ensure_ascii=False) + "\n").encode())

    try:
        for row_number, row in enumerate(rows, start=1):
            source_rows = row_number
            result = projector.project(row, row_number=row_number)
            observation_writer.add(projector, row, result, row_number)
            for name, count in result.skipped.items():
                skipped[entity_of[name]] = skipped.get(entity_of[name], 0) + count
            for rej in result.rejects:
                entity = rej["target_entity"]
                rejected[entity] = rejected.get(entity, 0) + 1
                counts = reasons.setdefault(entity, {})
                for reason in rej["reasons"]:
                    counts[reason] = counts.get(reason, 0) + 1
                note(rej)
            if result.warnings:
                warned_rows += 1
                for warning in result.warnings:
                    note(dict(warning, kind="value_dropped"))
            for entity, records in result.records.items():
                if entity == "CUSTOMER":
                    for record in records:
                        customers.add(record)  # type: ignore[union-attr]
                    continue
                buf = buffers[entity]
                for record in records:
                    buf += (json.dumps(dict(record, **lineage), ensure_ascii=False) + "\n").encode()
                    written[entity] += 1
                if len(buf) >= chunk_bytes:
                    flush(entity)
        observation_writer.close()
        parsed = clock()
        for entity in facts:
            flush(entity)

        outputs = []
        with connection.cursor() as cursor:
            for entity in entities:
                status = {"dataset_id": ctx["dataset_id"], "workspace_id": ctx["workspace_id"],
                          "app_id": str(ctx["app_id"]), "source_code": source.source_code,
                          "batch_ref": source.batch_ref, "object_key": source.key,
                          "object_etag": downloaded.etag, "mapping_version_id": ctx["mapping_version_id"],
                          "source_rows": source_rows, "error": None, "handoff_batch_ref": None,
                          "target_entity": entity, "output_rows": 0, "output_key": None,
                          "rejected_rows": rejected.get(entity, 0), "skipped_rows": skipped.get(entity, 0)}
                if status["rejected_rows"]:
                    # Readable without opening rejects.ndjson; overwritten by a real error below.
                    status["error"] = reject_summary(status["rejected_rows"], reasons.get(entity, {}))
                try:
                    if entity == "CUSTOMER":
                        status.update(_hand_off_customers(customers, status, source, ctx, input_prefix,
                                                          s3_client, pool))
                    else:
                        for future in inserts[entity]:
                            future.result()
                        writers[entity].close()
                        if written[entity]:
                            loaded = sink.count(entity, ctx["dataset_id"], source.batch_ref, version)
                            if loaded != written[entity]:
                                raise RuntimeError(f"ClickHouse has {loaded} of {written[entity]} rows")
                        status.update(route="cdp_intake", status="LOADED", output_rows=written[entity],
                                      output_key=writers[entity].key)
                except Exception as exc:
                    if entity != "CUSTOMER":
                        writers[entity].abort()
                    status.update(route="customer_pipeline" if entity == "CUSTOMER" else "cdp_intake",
                                  status="FAILED", error=str(exc)[:500], handoff_batch_ref=None)
                    LOGGER.exception("intake_output_failed entity=%s dataset=%s", entity, ctx["dataset_id"])
                cursor.execute(UPSERT_STATUS_SQL, status)
                outputs.append({k: status[k] for k in ("target_entity", "route", "status",
                                                        "output_rows", "rejected_rows",
                                                        "skipped_rows", "output_key", "error")})
        if issues is not None:
            issues.close()
        connection.commit()
    except BaseException:
        for writer in writers.values():
            writer.abort()
        if issues is not None:
            issues.abort()
        raise
    finally:
        pool.close()
        if customers is not None:
            customers.close()
    failed = [o for o in outputs if o["status"] == "FAILED"]
    elapsed = max(clock() - started, 1e-9)
    LOGGER.info("intake_dataset_processed dataset=%s batch_ref=%s rows=%d warned_rows=%d "
                "parse_s=%.1f total_s=%.1f rows_per_s=%.0f outputs=%s",
                ctx["dataset_id"], source.batch_ref, source_rows, warned_rows, parsed - started,
                elapsed, source_rows / elapsed,
                {o["target_entity"]: (o["status"], o["output_rows"]) for o in outputs})
    return {"status": "intake_failed" if failed else "intake_processed",
            "source_code": source.source_code, "batch_ref": source.batch_ref,
            "dataset_id": ctx["dataset_id"], "source_rows": source_rows, "warned_rows": warned_rows,
            "columns": len(headers), "outputs": outputs}


def reject_summary(rows: int, reasons: dict[str, int], top: int = 3) -> str:
    """'2000 rows rejected: source_created_at: unrecognised date (2000); ...' (max 500 chars)."""
    parts = [f"{reason} ({count})" for reason, count in sorted(reasons.items(), key=lambda x: -x[1])[:top]]
    return f"{rows} rows rejected: " + "; ".join(parts) if parts else f"{rows} rows rejected"


def _hand_off_customers(customers: CustomerRows | None, status: dict[str, Any], source: Any,
                        ctx: dict[str, Any], input_prefix: str, s3_client: Any,
                        pool: IoPool) -> dict[str, Any]:
    """Write the canonical customer CSV for the old route. With no customer row nothing is handed
    off: no hand-off batch is named (so nobody waits for a pipeline run that never starts), and if
    every row was rejected the output is FAILED with the reason."""
    unique = customers.unique() if customers is not None else 0
    if not unique:
        if status["rejected_rows"]:
            raise RuntimeError(f"all customer rows were rejected. {status['error'] or ''}".strip())
        return {"route": "customer_pipeline", "status": "HANDED_OFF", "output_rows": 0,
                "output_key": None, "handoff_batch_ref": None}
    handoff = handoff_batch_ref(source.batch_ref, ctx["dataset_id"])
    key = f"{input_prefix}{source.source_code}/{handoff}/customers/customer.csv"
    writer = S3Writer(s3_client, source.bucket, key, pool, content_type="text/csv")
    try:
        for chunk in customers.chunks():  # type: ignore[union-attr]
            writer.write(chunk)
        writer.close()
    except Exception:
        writer.abort()
        raise
    return {"route": "customer_pipeline", "status": "HANDED_OFF", "output_rows": unique,
            "output_key": key, "handoff_batch_ref": handoff}
