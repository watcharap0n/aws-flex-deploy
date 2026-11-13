"""Intake v2 dataset path of the landing Lambda, exercised with the retail purchase sample."""

from __future__ import annotations

import csv
import importlib.util
import io
import json
import sys
import types
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
LANDING_DIR = PROJECT_ROOT / "_lambda" / "landing_data"
LIB_ROOT = PROJECT_ROOT.parent / "buzz-cdp-workspace" / "packages" / "cdp-intake-mapping"
SAMPLE = LIB_ROOT / "samples" / "purchase_bill_sample.csv"
TEMPLATE = LIB_ROOT / "templates" / "retail" / "purchase_bill.json"
DATASET_ID = "0b6eee51-1704-4a69-942f-a19853092b80"
BATCH_REF = "2026-10-22-101500-990008-0123456789ab"


def _load(name: str, path: Path):
    if str(LANDING_DIR) not in sys.path:
        sys.path.insert(0, str(LANDING_DIR))
    psycopg2 = types.ModuleType("psycopg2")
    extras = types.ModuleType("psycopg2.extras")
    extras.Json = lambda value: value
    extras.execute_values = lambda *args, **kwargs: None
    psycopg2.extras = extras
    sys.modules.setdefault("psycopg2", psycopg2)
    sys.modules.setdefault("psycopg2.extras", extras)
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def landing():
    return _load("landing_lambda_for_intake", LANDING_DIR / "lambda_function.py")


@pytest.fixture(scope="module")
def intake():
    return _load("intake_projection", LANDING_DIR / "intake_projection.py")


def _record(key: str) -> dict:
    return {"s3": {"bucket": {"name": "cdp-stg-bucket"}, "object": {"key": key, "eTag": "e1"}}}


def test_dataset_path_is_recognised_and_customer_path_unchanged(landing):
    config = landing._load_config.__globals__["Config"](
        db_secret_id="x", input_prefix="data_sources/", output_root_prefix="raw/",
        output_bucket=None, upsert_page_size=250)
    source = landing._source_from_record(
        _record(f"data_sources/pos/{BATCH_REF}/datasets/{DATASET_ID}/purchase.csv"), config)
    assert (source.source_type, source.dataset_id, source.file_format) == ("datasets", DATASET_ID, "csv")
    classic = landing._source_from_record(
        _record(f"data_sources/pos/{BATCH_REF}/customers/customer.csv"), config)
    assert classic.source_type == "customers" and classic.dataset_id is None
    with pytest.raises(ValueError, match="UUID"):
        landing._source_from_record(_record(f"data_sources/pos/{BATCH_REF}/datasets/nope/x.csv"),
                                    config)


def test_batch_ref_helpers(intake):
    assert intake.batch_app_id(BATCH_REF) == "990008"
    handoff = intake.handoff_batch_ref(BATCH_REF, DATASET_ID)
    assert handoff.startswith("2026-10-22-101500-990008-") and handoff != BATCH_REF
    assert intake.BATCH_REF_APP.fullmatch(handoff)
    with pytest.raises(intake.DatasetObjectError):
        intake.batch_app_id("adhoc-batch")


class _Cursor:
    def __init__(self, conn):
        self.conn = conn

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params):
        self.conn.statements.append((sql, params))

    def fetchone(self):
        return self.conn.context_row


class _Connection:
    def __init__(self, context_row):
        self.context_row = context_row
        self.statements = []
        self.commits = 0

    def cursor(self):
        return _Cursor(self)

    def commit(self):
        self.commits += 1


class _S3:
    def __init__(self):
        self.objects = {}
        self.uploads = {}
        self.multipart_keys = []

    def put_object(self, Bucket, Key, Body, **_kwargs):
        self.objects[Key] = Body

    def create_multipart_upload(self, Bucket, Key, **_kwargs):
        upload_id = f"u{len(self.uploads)}"
        self.uploads[upload_id] = {}
        return {"UploadId": upload_id}

    def upload_part(self, Bucket, Key, UploadId, PartNumber, Body):
        self.uploads[UploadId][PartNumber] = bytes(Body)
        return {"ETag": f"e{PartNumber}"}

    def complete_multipart_upload(self, Bucket, Key, UploadId, MultipartUpload):
        numbers = [p["PartNumber"] for p in MultipartUpload["Parts"]]
        assert numbers == sorted(numbers)
        parts = self.uploads.pop(UploadId)
        self.objects[Key] = b"".join(parts[n] for n in numbers)
        self.multipart_keys.append(Key)

    def abort_multipart_upload(self, Bucket, Key, UploadId):
        self.uploads.pop(UploadId, None)


class _Sink:
    def __init__(self):
        self.rows = {}

    def insert(self, entity, rows):
        self.rows.setdefault(entity, []).extend(rows)

    def insert_ndjson(self, entity, body):
        self.rows.setdefault(entity, []).extend(json.loads(line) for line in body.decode().splitlines())

    def count(self, entity, dataset_id, batch_ref, version):
        return len([r for r in self.rows.get(entity, []) if r["_version"] == version])


def _run(intake, landing, app_id="990008", definition_key="file_upload", body=None, mapping=None):
    mapping = mapping or json.loads(TEMPLATE.read_text())
    conn = _Connection((DATASET_ID, "578dce72-9575-4fda-851c-ff123826a906", "MULTI", "pos",
                        definition_key, app_id, "a15cc080-6de5-46c7-9033-71f7c0eb5083",
                        "PUBLISHED", mapping))
    source = types.SimpleNamespace(
        bucket="cdp-stg-bucket", source_code="pos", batch_ref=BATCH_REF, dataset_id=DATASET_ID,
        file_format="csv", key=f"data_sources/pos/{BATCH_REF}/datasets/{DATASET_ID}/purchase.csv")
    downloaded = types.SimpleNamespace(content=io.BytesIO(body if body is not None else SAMPLE.read_bytes()), etag="e1")
    s3, sink = _S3(), _Sink()
    result = intake.process_dataset_object(
        source=source, downloaded=downloaded, connection=conn, s3_client=s3,
        output_bucket="cdp-stg-bucket", output_root_prefix="raw/", input_prefix="data_sources/",
        customer_columns=landing.CSV_COLUMNS, hmac_secret=b"test", sink=sink,
        clock=lambda: 1_760_000_000.0)
    return result, conn, s3, sink


def test_purchase_upload_splits_into_customers_and_transactions(intake, landing):
    result, conn, s3, sink = _run(intake, landing)
    assert result["status"] == "intake_processed" and result["source_rows"] == 8
    outputs = {o["target_entity"]: o for o in result["outputs"]}

    # Every bill lands in ClickHouse cdp_intake.transaction (the anonymous one too).
    assert outputs["TRANSACTION"]["status"] == "LOADED"
    assert outputs["TRANSACTION"]["output_rows"] == 8
    bills = {r["source_transaction_id"]: r for r in sink.rows["TRANSACTION"]}
    assert "source_customer_id" not in bills["INV-2026100005"]
    assert bills["INV-2026100003"]["source_customer_id"] == "phone:0800000003"
    assert bills["INV-2026100001"]["occurred_at"] == "2026-10-01T02:15:00Z"
    assert bills["INV-2026100007"]["status"] == "void"
    assert {r["app_id"] for r in sink.rows["TRANSACTION"]} == {"990008"}

    # Customers are handed to the existing customer route as one canonical CSV, one row per key.
    handoff = outputs["CUSTOMER"]
    assert handoff["status"] == "HANDED_OFF" and handoff["output_rows"] == 5
    assert handoff["output_key"].endswith("/customers/customer.csv")
    rows = list(csv.DictReader(io.StringIO(s3.objects[handoff["output_key"]].decode())))
    assert [r["source_customer_id"] for r in rows] == [
        "member:M000001", "member:M000002", "phone:0800000003", "member:M000004",
        "member:M000005"]
    assert rows[0]["phone"] == "0800000001" and rows[0]["member_code"] == "M000001"

    # Lineage file is .ndjson (never .csv, so ingestion is not triggered); statuses recorded.
    assert any(k.endswith("/transaction.ndjson") for k in s3.objects)
    assert not any(k.startswith("raw/") and k.endswith(".csv") for k in s3.objects)
    upserts = [p for sql, p in conn.statements if "intake_batch_output" in sql]
    assert {(p["target_entity"], p["status"]) for p in upserts} == {
        ("CUSTOMER", "HANDED_OFF"), ("TRANSACTION", "LOADED")}
    assert conn.commits == 1


def test_wrong_app_or_connection_is_rejected(intake, landing):
    with pytest.raises(intake.DatasetObjectError, match="app id"):
        _run(intake, landing, app_id="43")
    with pytest.raises(intake.DatasetObjectError, match="file_upload"):
        _run(intake, landing, definition_key="postgresql")


def test_clickhouse_insert_is_chunked_under_the_body_cap(intake, monkeypatch):
    sink = intake.ClickHouseSink({"url": "http://ch:8123", "username": "u", "password": "p"})
    bodies = []
    monkeypatch.setattr(sink, "_post", lambda query, body=None: bodies.append(body))
    monkeypatch.setattr(intake, "INSERT_CHUNK_BYTES", 400)
    rows = [{"app_id": "990008", "source_event_id": f"E{i}", "event_name": "app_open",
             "_version": 1} for i in range(20)]
    sink.insert("EVENT", rows)
    assert len(bodies) > 1 and all(len(b) <= 400 for b in bodies)
    sent = [json.loads(line) for b in bodies for line in b.decode().splitlines()]
    assert [r["source_event_id"] for r in sent] == [f"E{i}" for i in range(20)]


def _big_csv(rows: int) -> bytes:
    """Retail purchase layout, `rows` bills over rows // 3 customers, a few bad rows."""
    header = SAMPLE.read_text(encoding="utf-8-sig").splitlines()[0]
    template = SAMPLE.read_text(encoding="utf-8-sig").splitlines()[1].split(",")
    cols = next(csv.reader([header]))
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\n")
    w.writerow(cols)
    first = next(iter(csv.reader([",".join(template)])))
    for i in range(rows):
        cells = list(first)
        for j, name in enumerate(cols):
            low = name.lower()
            if "เลขที่" in name or "bill" in low or "invoice" in low:
                cells[j] = f"INV-{i:08d}"
            elif "สมาชิก" in name or "member" in low:
                cells[j] = f"M{i // 3:07d}"
            elif "เบอร์" in name or "phone" in low or "mobile" in low:
                cells[j] = f"08{(i // 3) % 100000000:08d}"
        w.writerow(cells)
    return out.getvalue().encode()


def test_large_file_streams_with_multipart_and_customer_spill(intake, landing, monkeypatch):
    monkeypatch.setattr(intake.S3Writer, "PART_BYTES", 256 * 1024)
    monkeypatch.setattr(intake, "INSERT_CHUNK_BYTES", 128 * 1024)
    monkeypatch.setenv("INTAKE_CUSTOMER_SPILL_KEYS", "500")
    result, _conn, s3, sink = _run(intake, landing, body=_big_csv(6000))
    outputs = {o["target_entity"]: o for o in result["outputs"]}
    assert result["source_rows"] == 6000
    assert outputs["TRANSACTION"]["status"] == "LOADED"
    assert outputs["TRANSACTION"]["output_rows"] == len(sink.rows["TRANSACTION"]) == 6000
    key = outputs["TRANSACTION"]["output_key"]
    assert key in s3.multipart_keys
    assert len(s3.objects[key].decode().splitlines()) == 6000
    # Customers: one row per member, first-seen order, last occurrence wins (spilled to SQLite).
    handoff = outputs["CUSTOMER"]
    assert handoff["status"] == "HANDED_OFF" and handoff["output_rows"] == 2000
    rows = list(csv.DictReader(io.StringIO(s3.objects[handoff["output_key"]].decode())))
    assert [r["source_customer_id"] for r in rows[:3]] == ["member:M0000000", "member:M0000001", "member:M0000002"]
    assert len({r["source_customer_id"] for r in rows}) == len(rows) == 2000


def test_customers_all_rejected_fail_without_a_handoff(intake, landing):
    mapping = json.loads(TEMPLATE.read_text())
    for proj in mapping["projections"]:
        if proj["target_entity"] == "CUSTOMER":
            # a required customer value that can never be read
            proj["fields"][0] = {"target_field": "source_customer_id", "source_path": next(
                iter(csv.reader([SAMPLE.read_text(encoding="utf-8-sig").splitlines()[0]])))[0],
                "transforms": ["to_number"]}
            proj.pop("when_present", None)
    result, conn, s3, _sink = _run(intake, landing, mapping=mapping)
    customer = {o["target_entity"]: o for o in result["outputs"]}["CUSTOMER"]
    assert customer["status"] == "FAILED" and customer["output_rows"] == 0
    assert customer["error"].startswith("all customer rows were rejected. 8 rows rejected:")
    upsert = next(p for sql, p in conn.statements if "intake_batch_output" in sql and p["target_entity"] == "CUSTOMER")
    assert upsert["handoff_batch_ref"] is None
    assert not any(k.endswith("/customers/customer.csv") for k in s3.objects)


@pytest.mark.parametrize("spool_max", [64, 8 * 1024 * 1024])
def test_reads_the_handlers_spooled_download(intake, spool_max):
    """The handler hands over a SpooledTemporaryFile (in memory or rolled to disk)."""
    import tempfile
    content = tempfile.SpooledTemporaryFile(max_size=spool_max, mode="w+b")
    content.write(SAMPLE.read_bytes())
    headers, rows = intake.iter_rows(content, "csv")
    assert len(list(rows)) == 8 and headers


def test_related_sidecar_keeps_customer_csv_and_statuses_byte_identical(intake, landing, monkeypatch):
    monkeypatch.delenv('RELATED_OBSERVATIONS_ENABLED', raising=False)
    before = _run(intake, landing)
    monkeypatch.setenv('RELATED_OBSERVATIONS_ENABLED', 'true')
    after = _run(intake, landing)
    assert before[0] == after[0]
    assert before[1].statements == after[1].statements
    assert before[2].objects == after[2].objects
    assert before[3].rows['TRANSACTION'] == after[3].rows['TRANSACTION']
    assert after[3].rows['person_observation_batch'][0]['rows'] > 0


def test_related_sidecar_failure_does_not_fail_customer_route(intake, landing, monkeypatch):
    original = _Sink.insert_ndjson
    def fail_observation(self, entity, body):
        if entity.startswith('person_observation'):
            raise RuntimeError('observation store unavailable')
        return original(self, entity, body)
    monkeypatch.setattr(_Sink, 'insert_ndjson', fail_observation)
    monkeypatch.setenv('RELATED_OBSERVATIONS_ENABLED', 'true')
    result, _, _, sink = _run(intake, landing)
    assert result['status'] == 'intake_processed'
    assert all(o['status'] != 'FAILED' for o in result['outputs'])
    assert 'person_observation_batch' not in sink.rows


def test_related_observations_include_keyless_names_and_hash_identity_tokens(intake):
    from person_observation import observations
    from cdp_intake_mapping import Projector, Transforms
    mapping = {'version': 2, 'derived_keys': {}, 'projections': [{
        'name': 'customer', 'target_entity': 'CUSTOMER', 'fields': [
            {'target_field': 'source_customer_id', 'source_path': 'id'},
            {'target_field': 'full_name', 'source_path': 'name'},
            {'target_field': 'external_ids', 'pack': [{'as': 'national_id', 'path': 'nid'}]}]}]}
    projector = Projector(mapping, Transforms(hmac_key=b'test-only'))
    row = {'name': 'Keyless Person', 'nid': 'sensitive-identity'}
    result = projector.project(row)
    assert not result.records.get('CUSTOMER')
    record = list(observations(projector, row, result, {}, 1))[0]
    assert 'sensitive-identity' not in record['payload']
    assert json.loads(record['payload'])['customer']['full_name'] == 'Keyless Person'
