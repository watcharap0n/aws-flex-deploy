"""Any customer file lands (2026-10-22): no file or row dies on its values or key column.

Prod failures this encodes: employees.csv (97 rows with status "resigned" rejected,
then the whole file failed its row-count check), customer_profile_copy.csv and
BA_TEST_QA_20261014_no_identifiers.csv (no source_customer_id column: INVALID file).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_customer_pipeline import _load_module  # noqa: E402


@pytest.fixture(scope="module")
def landing():
    return _load_module("tolerance_landing_lambda", "_lambda/landing_data/lambda_function.py",
                        stub_psycopg2=True)


@pytest.fixture(scope="module")
def ingestion():
    return _load_module("tolerance_ingestion_lambda", "_lambda/ingestion_data/lambda_function.py",
                        stub_psycopg2=True)


def _parse(landing, text, *, spool=False):
    parsed = landing.parse_csv_evidence(text.encode(), "source_customer_id", spool_rows=spool)
    return parsed, list(parsed.rows)


@pytest.mark.parametrize("spool", [False, True])
def test_file_without_key_column_derives_keys_instead_of_failing(landing, spool):
    parsed, rows = _parse(landing, "phone,email,first_name,member_code\n"
                                   "081-234-5678,,Ann,\n"
                                   ",B@Example.Test,Bob,\n"
                                   "0812345679,,Cat,m-9\n", spool=spool)
    assert parsed.file_status == "INGESTED_WITH_WARNINGS" and parsed.file_error is None
    assert [row.source_key for row in rows] == ["phone:0812345678", "email:b@example.test", "member:M-9"]
    assert [row.validation_errors for row in rows] == [["derived_key:phone"], ["derived_key:email"],
                                                       ["derived_key:member"]]
    assert parsed.blank_key_rows == 3


@pytest.mark.parametrize("spool", [False, True])
def test_blank_keys_are_derived_and_explicit_keys_kept(landing, spool):
    parsed, rows = _parse(landing, "source_customer_id,phone\nC-1,0811111111\n,0822222222\n",
                          spool=spool)
    assert [row.source_key for row in rows] == ["C-1", "phone:0822222222"]
    assert rows[0].validation_errors is None
    assert rows[1].validation_errors == ["derived_key:phone"]


@pytest.mark.parametrize("spool", [False, True])
def test_row_without_any_identifier_gets_a_stable_fingerprint(landing, spool):
    text = "_ignored_1,_ignored_2\nBA_TEST_NoIdentifier,12\n"
    first = _parse(landing, text, spool=spool)[1][0]
    again = _parse(landing, text, spool=spool)[1][0]
    assert first.source_key.startswith("row:") and first.source_key == again.source_key
    assert first.validation_errors == ["derived_key:row"]


@pytest.mark.parametrize("spool", [False, True])
def test_derived_key_collisions_follow_the_duplicate_rule(landing, spool):
    parsed, rows = _parse(landing, "phone,first_name\n0811111111,Old\n0811111111,New\n", spool=spool)
    assert parsed.duplicate_key_rows == 2
    assert rows[1].validation_errors == ["derived_key:phone", "duplicate_key:source_customer_id"]


def test_ambiguous_key_columns_use_the_first_instead_of_failing(landing):
    parsed, rows = _parse(landing, "source_customer_id,customer_id\nA,B\n")
    assert parsed.file_status == "INGESTED" and rows[0].source_key == "A"


def test_empty_file_still_fails_with_a_reason(landing):
    parsed, _ = _parse(landing, "source_customer_id,first_name\n")
    assert parsed.file_status == "INVALID" and parsed.file_error == "no_data_rows"


def test_canonical_row_keeps_invalid_json_and_dates_instead_of_failing(landing):
    row = landing._canonical_customer_row("C-1", {
        "external_ids": "{oops", "attributes": '{"tier":"gold"}',
        "date_of_birth": "31/02/1990", "source_created_at": "not a time", "status": "resigned"})
    attributes = json.loads(row["attributes"])
    assert json.loads(row["external_ids"]) == {}
    assert row["date_of_birth"] == "" and row["source_created_at"] == ""
    assert row["status"] == "resigned"  # ingestion maps it; landing keeps the text
    assert attributes == {"tier": "gold", "source_invalid_values": {
        "external_ids": "{oops", "date_of_birth": "31/02/1990", "source_created_at": "not a time"}}


@pytest.mark.parametrize("raw", ["None", "null", "N/A", "-", ""])
def test_null_placeholders_are_missing_not_invalid(landing, raw):
    row = landing._canonical_customer_row("C-1", {"date_of_birth": raw})
    assert row["date_of_birth"] == "" and json.loads(row["attributes"]) == {}


@pytest.mark.parametrize("raw,expected", [("2533-05-12", "1990-05-12"), ("12/05/2533", "1990-05-12"),
                                          ("1990-05-12", "1990-05-12")])
def test_thai_buddhist_era_birthdates_are_converted(landing, raw, expected):
    assert landing._canonical_customer_row("C-1", {"date_of_birth": raw})["date_of_birth"] == expected


def test_landing_output_always_passes_ingestion_without_rejections(landing, ingestion):
    # End to end in memory: Kane's employees.csv shape plus every bad value seen in prod.
    source = ("employee_code,first_name,phone,status,date_of_birth,attributes\n"
              "E1,Ann,0811111111,resigned,2533-01-02,{}\n"
              "E2,Bob,0822222222,active,None,{bad\n"
              "E3,Cat,0833333333,ลาออก,not-a-date,{}\n")
    _, rows = _parse(landing, source)
    import csv
    import io
    out = io.StringIO()
    writer = csv.DictWriter(out, fieldnames=landing.CSV_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow(landing._canonical_customer_row(row.source_key, row.raw_payload))
    customers, stats = ingestion.prepare_customers(out.getvalue().encode())
    assert stats["records_received"] == 3 and stats["records_rejected"] == 0
    assert [c["status"] for c in customers] == ["INACTIVE", "ACTIVE", "INACTIVE"]
    assert customers[0]["date_of_birth"].isoformat() == "1990-01-02"
    assert customers[0]["attributes"]["source_status"] == "resigned"
