"""Benchmark the streaming intake v2 landing path (no AWS): python tests/benchmarks/intake_stream_benchmark.py [rows]

Generates a retail purchase file (bills over rows // 3 customers) and a customer-only file with
every row a new customer, runs intake_projection.process_dataset_object with S3/ClickHouse stubs
that only count bytes, and prints rows/s and peak RSS.
"""
from __future__ import annotations

import csv
import json
import resource
import sys
import tempfile
import time
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "_lambda" / "landing_data"))
for name in ("psycopg2", "psycopg2.extras"):
    sys.modules.setdefault(name, types.ModuleType(name))
import intake_projection as ip

N = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 1_000_000
CUSTOMER_COLUMNS = ["source_customer_id", "external_id", "first_name", "last_name", "full_name", "email", "phone",
                    "gender", "date_of_birth", "member_code", "province", "attributes", "source_created_at"]


class S3:
    def __init__(self):
        self.bytes = 0
    def put_object(self, Body, **_):
        self.bytes += len(Body)
    def create_multipart_upload(self, **_):
        return {"UploadId": "u"}
    def upload_part(self, Body, PartNumber, **_):
        self.bytes += len(Body)
        return {"ETag": str(PartNumber)}
    def complete_multipart_upload(self, **_):
        pass
    def abort_multipart_upload(self, **_):
        pass


class Sink:
    def __init__(self):
        self.rows = {}
    def insert_ndjson(self, entity, body):
        self.rows[entity] = self.rows.get(entity, 0) + body.count(b"\n")
    def count(self, entity, *_):
        return self.rows.get(entity, 0)


class Cursor:
    def __init__(self, ctx):
        self.ctx = ctx
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False
    def execute(self, *a):
        pass
    def fetchone(self):
        return self.ctx


class Conn:
    def __init__(self, ctx):
        self.ctx = ctx
    def cursor(self):
        return Cursor(self.ctx)
    def commit(self):
        pass


def purchase_file(path: Path, n: int) -> dict:
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["bill_no", "bill_date", "member_code", "mobile", "customer_name", "birthday", "branch", "net_amount", "payment", "items"])
        for i in range(n):
            c = i // 3
            w.writerow([f"INV{i:09d}", f"{1 + i % 28:02d}/{1 + i % 12:02d}/2568 {i % 24:02d}:{i % 60:02d}", f"M{c:08d}",
                        f"08{c % 100000000:08d}", f"ลูกค้า ทดสอบ{c}", "1990-01-15", f"BR{i % 40}", f"{100 + i % 997}.50",
                        "cash" if i % 3 else "card", f"SKU{i % 300}x{1 + i % 3}"])
    keys = {"buyer": {"first_of": [{"id_type": "member", "source_path": "member_code", "transforms": ["trim", "uppercase"]},
                                   {"id_type": "phone", "source_path": "mobile", "transforms": ["phone_key"]}]}}
    return {"version": 2, "derived_keys": keys, "projections": [
        {"name": "customer", "target_entity": "CUSTOMER", "when_present": ["buyer"], "fields": [
            {"target_field": "source_customer_id", "derived_key": "buyer"},
            {"target_field": "member_code", "source_path": "member_code", "transforms": ["trim", "uppercase"]},
            {"target_field": "phone", "source_path": "mobile", "transforms": ["phone"]},
            {"target_field": "full_name", "source_path": "customer_name", "transforms": ["trim"]},
            {"target_field": "date_of_birth", "source_path": "birthday", "transforms": ["parse_date"]}]},
        {"name": "purchase", "target_entity": "TRANSACTION", "when_present": ["bill_no"], "fields": [
            {"target_field": "source_transaction_id", "source_path": "bill_no", "transforms": ["trim"]},
            {"target_field": "source_customer_id", "derived_key": "buyer"},
            {"target_field": "occurred_at", "source_path": "bill_date", "transforms": ["parse_timestamp"]},
            {"target_field": "amount", "source_path": "net_amount", "transforms": ["to_number"]},
            {"target_field": "location_id", "source_path": "branch", "transforms": ["trim"]},
            {"target_field": "items", "pack": [{"path": "items", "as": "lines", "transforms": ["trim"]}]},
            {"target_field": "attributes", "pack": [{"path": "payment", "as": "payment_method", "transforms": ["trim"]}]},
            {"target_field": "transaction_type", "constant": "purchase"}, {"target_field": "currency", "constant": "THB"}]}]}


def customer_file(path: Path, n: int) -> dict:
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["source_customer_id", "first_name", "last_name", "email", "phone", "date_of_birth", "source_created_at", "tier"])
        for i in range(n):
            w.writerow([f"C{i:09d}", f"First{i}", f"Last{i % 5000}", f"user{i}@example.test", f"08{i % 100000000:08d}",
                        "1985-03-02", "Sun Jun 23 2024 22:00:00 GMT-0400 (Eastern Daylight Time)", ["gold", "silver"][i % 2]])
    return {"version": 2, "derived_keys": {"buyer": {"first_of": [{"id_type": "member", "source_path": "source_customer_id", "transforms": ["trim"]}]}},
            "projections": [{"name": "customer", "target_entity": "CUSTOMER", "when_present": ["buyer"], "fields": [
                {"target_field": "source_customer_id", "derived_key": "buyer"},
                {"target_field": "first_name", "source_path": "first_name", "transforms": ["trim"]},
                {"target_field": "last_name", "source_path": "last_name", "transforms": ["trim"]},
                {"target_field": "email", "source_path": "email", "transforms": ["trim", "lowercase"]},
                {"target_field": "phone", "source_path": "phone", "transforms": ["phone"]},
                {"target_field": "date_of_birth", "source_path": "date_of_birth", "transforms": ["parse_date"]},
                {"target_field": "source_created_at", "source_path": "source_created_at", "transforms": ["parse_timestamp"]},
                {"target_field": "attributes", "pack": [{"path": "tier", "as": "tier", "transforms": ["trim"]}]}]}]}


def run(label: str, make) -> None:
    with tempfile.TemporaryDirectory() as d:
        path = Path(d) / "in.csv"
        mapping = make(path, N)
        ctx = ("0b6eee51-1704-4a69-942f-a19853092b80", "578dce72-9575-4fda-851c-ff123826a906", "MULTI", "pos", "file_upload",
               "990008", "a15cc080-6de5-46c7-9033-71f7c0eb5083", "PUBLISHED", mapping)
        source = types.SimpleNamespace(bucket="b", source_code="pos", batch_ref="2026-10-22-101500-990008-0123456789ab",
                                       dataset_id=ctx[0], file_format="csv", key="data_sources/pos/x/datasets/y/in.csv")
        s3, sink = S3(), Sink()
        t0 = time.perf_counter()
        with path.open("rb") as content:
            res = ip.process_dataset_object(source=source, downloaded=types.SimpleNamespace(content=content, etag="e"),
                                            connection=Conn(ctx), s3_client=s3, output_bucket="b", output_root_prefix="raw/",
                                            input_prefix="data_sources/", customer_columns=CUSTOMER_COLUMNS,
                                            hmac_secret=b"k", sink=sink)
        dt = time.perf_counter() - t0
        print(json.dumps({"case": label, "rows": res["source_rows"], "file_mb": round(path.stat().st_size / 1e6, 1),
                          "seconds": round(dt, 1), "rows_per_s": round(res["source_rows"] / dt),
                          "outputs": {o["target_entity"]: [o["status"], o["output_rows"], o["rejected_rows"]] for o in res["outputs"]},
                          "warned_rows": res["warned_rows"], "s3_mb": round(s3.bytes / 1e6, 1),
                          "peak_rss_mb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1024 * 1024 if sys.platform == "darwin" else 1024))}))


if __name__ == "__main__":
    run("purchase (bills + customers)", purchase_file)
    run("customers only, all distinct", customer_file)
