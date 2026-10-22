"""Contract tests for the single-file customer landing and ingestion flow."""

from __future__ import annotations

import csv
import importlib.util
import io
import json
import sys
import types
from pathlib import Path

import pytest
from pglast import parse_sql


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _load_module(name: str, relative_path: str, *, stub_psycopg2: bool = False):
    if stub_psycopg2:
        psycopg2 = types.ModuleType("psycopg2")
        extras = types.ModuleType("psycopg2.extras")
        extras.Json = lambda value: value
        extras.execute_values = lambda *args, **kwargs: None
        psycopg2.extras = extras
        sys.modules.setdefault("psycopg2", psycopg2)
        sys.modules.setdefault("psycopg2.extras", extras)

    spec = importlib.util.spec_from_file_location(
        name, PROJECT_ROOT / relative_path
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def landing():
    return _load_module(
        "customer_landing_lambda",
        "_lambda/landing_data/lambda_function.py",
        stub_psycopg2=True,
    )


@pytest.fixture(scope="module")
def ingestion():
    return _load_module(
        "customer_ingestion_lambda",
        "_lambda/ingestion_data/lambda_function.py",
    )


@pytest.mark.parametrize("raw_status, expected", [
    ("ใช้งาน", "ACTIVE"), ("ปิดใช้งาน", "INACTIVE"),
    (" active ", "ACTIVE"), ("inactive", "INACTIVE"),
    ("BLOCKED", "BLOCKED"), ("DELETED", "DELETED"), ("", "ACTIVE"),
    ("SUSPENDED", "SUSPENDED"), (" suspended ", "SUSPENDED"),
    ("PENDING", "PENDING"),
])
def test_ingestion_normalizes_customer_status(ingestion, raw_status, expected):
    payload = f"source_customer_id,status\nstatus-regression,{raw_status}\n".encode()
    customers, stats = ingestion.prepare_customers(payload)
    assert stats["records_rejected"] == 0
    assert customers[0]["status"] == expected
    assert customers[0]["raw_payload"]["status"] == raw_status


@pytest.mark.parametrize('age,status', [('-1','ACTIVE'), ('18','UNRECOGNIZED'),
                                         ('-999','UNKNOWN')])
def test_ingestion_keeps_rows_with_out_of_contract_values(ingestion, age, status):
    # A value outside the contract never rejects the row (2026-10-22): the typed
    # field is NULL or defaulted and the original text is kept in attributes.
    payload = (f'source_customer_id,age,status\ninvalid,{age},{status}\n'
               'valid,18,SUSPENDED\n').encode()
    customers, stats = ingestion.prepare_customers(payload)
    by_id = {customer['source_customer_id']: customer for customer in customers}
    assert sorted(by_id) == ['invalid', 'valid']
    assert stats['records_received'] == 2 and stats['records_rejected'] == 0
    kept = by_id['invalid']
    assert kept['status'] == 'ACTIVE'
    if int(age) < 0:
        assert kept['age'] is None
        assert kept['attributes']['source_invalid_values'] == {'age': age}
    if status != 'ACTIVE':
        assert kept['attributes']['source_status'] == status
    assert by_id['valid']['status'] == 'SUSPENDED' and 'source_status' not in by_id['valid']['attributes']


@pytest.mark.parametrize('raw,expected', [('resigned', 'INACTIVE'), ('Resigned', 'INACTIVE'),
                                          ('ลาออก', 'INACTIVE'), ('on hold', 'SUSPENDED'),
                                          ('banned', 'BLOCKED'), ('vip', 'ACTIVE')])
def test_ingestion_maps_free_text_statuses_and_keeps_the_source_value(ingestion, raw, expected):
    customers, stats = ingestion.prepare_customers(
        f'source_customer_id,status\nemp-1,{raw}\n'.encode())
    assert stats['records_rejected'] == 0 and stats['records_normalised'] == 1
    assert customers[0]['status'] == expected
    assert customers[0]['attributes']['source_status'] == raw
    kind = 'status_unrecognised' if raw == 'vip' else 'status'
    assert stats['normalised_fields'] == {kind: 1}


def test_ingestion_keeps_unparseable_json_dates_and_timestamps(ingestion):
    customers, stats = ingestion.prepare_customers(
        b'source_customer_id,external_ids,attributes,date_of_birth,source_created_at\n'
        b'c-1,{not json,[1],1990-02-31,yesterday\n')
    assert stats['records_rejected'] == 0
    customer = customers[0]
    assert customer['external_ids'] == {} and customer['date_of_birth'] is None
    assert customer['source_created_at'] is None
    assert customer['attributes'] == {'source_invalid_values': {
        'external_ids': '{not json', 'attributes': '[1]', 'date_of_birth': '1990-02-31',
        'source_created_at': 'yesterday'}}
    assert stats['normalised_fields'] == {'attributes': 1, 'date_of_birth': 1, 'external_ids': 1,
                                          'source_created_at': 1}


def test_ingestion_bounds_an_overlong_source_key_instead_of_rejecting(ingestion):
    key = 'k' * 300
    customers, stats = ingestion.prepare_customers(f'source_customer_id\n{key}\n'.encode())
    assert stats['records_rejected'] == 0
    assert len(customers[0]['source_customer_id']) <= 255
    assert customers[0]['source_customer_id'].startswith('k' * 200 + '#')


def _config(landing):
    return landing.Config(
        db_secret_id="unused",
        input_prefix="data_sources/",
        output_root_prefix="raw/",
        output_bucket=None,
        upsert_page_size=1000,
    )


def test_landing_accepts_one_customer_file(landing):
    source = landing._source_from_record(
        {
            "s3": {
                "bucket": {"name": "example-bucket"},
                "object": {
                    "key": "data_sources/crm/batch-001/customers/customer.csv",
                    "eTag": "abc",
                },
            }
        },
        _config(landing),
    )

    assert source is not None
    assert source.source_code == "crm"
    assert source.batch_ref == "batch-001"
    assert source.source_type == "customers"


def test_landing_rejects_legacy_two_file_path(landing):
    with pytest.raises(ValueError, match="source_type must be customers"):
        landing._source_from_record(
            {
                "s3": {
                    "bucket": {"name": "example-bucket"},
                    "object": {
                        "key": (
                            "data_sources/crm/batch-001/"
                            "customer_profiles/customer.xlsx"
                        )
                    },
                }
            },
            _config(landing),
        )


def test_landing_accepts_crm_alias_and_builds_canonical_row(landing):
    parsed = landing.parse_csv_evidence(
        (
            "StoreCode,FirstName,LastName,ContactNumber,RegisterDate,"
            "LineUuid,BzbsUserId,StoreProvince\n"
            "000123,Ada,Lovelace,0812345678,2026-08-05,line-1,bzbs-1,Bangkok\n"
        ).encode(),
        "source_customer_id",
    )

    assert parsed.file_status == "INGESTED"
    assert parsed.rows[0].source_key == "000123"

    row = landing._canonical_customer_row(
        parsed.rows[0].source_key,
        parsed.rows[0].raw_payload,
    )
    assert row["source_customer_id"] == "000123"
    assert row["first_name"] == "Ada"
    assert row["phone"] == "0812345678"
    assert row["province"] == "Bangkok"
    assert row["source_created_at"].startswith("2026-08-05")
    assert json.loads(row["external_ids"]) == {
        "bzbs_user_id": "bzbs-1",
        "line_user_id": "line-1",
    }


def test_ingestion_accepts_minimal_canonical_file(ingestion):
    customers, stats = ingestion.prepare_customers(
        b"source_customer_id,first_name\ncrm-1,Ada\n"
    )

    assert stats == {
        "records_received": 1,
        "records_rejected": 0,
        "duplicate_rows": 0,
        "records_normalised": 0,
        "normalised_fields": {},
    }
    assert customers[0]["source_customer_id"] == "crm-1"
    assert customers[0]["first_name"] == "Ada"
    assert customers[0]["external_ids"] == {}


def test_full_source_customer_schema_reaches_ingestion(landing, ingestion):
    payload = {
        "source_customer_id": "crm-1",
        "external_id": "ext-1",
        "first_name": "Ada",
        "middle_name": "M",
        "last_name": "Lovelace",
        "full_name": "Ada M Lovelace",
        "nickname": "Enchantress of Numbers",
        "gender": "female",
        "date_of_birth": "1815-12-10",
        "birthday": "1815-12-10",
        "age": "36",
        "email": "ADA@EXAMPLE.COM",
        "phone": "0812345678",
        "line_user_id": "line-1",
        "facebook_id": "facebook-1",
        "google_id": "google-1",
        "apple_id": "apple-1",
        "member_code": "member-1",
        "customer_type": "vip",
        "status": "ACTIVE",
        "language": "th",
        "address_line_1": "1 Main St",
        "address_line_2": "Room 2",
        "district": "Bangkok",
        "province": "Bangkok",
        "postal_code": "10110",
        "country": "TH",
        "source": "frontend-upload",
        "source_system": "crm",
        "external_ids": {"device_ids": ["device-1"]},
        "attributes": {"tier": "gold"},
        "source_created_at": "2026-08-05T01:00:00Z",
        "source_updated_at": "2026-08-05T02:00:00Z",
    }
    canonical = landing._canonical_customer_row("crm-1", payload)
    assert set(canonical) == set(landing.CSV_COLUMNS)
    assert canonical["google_id"] == "google-1"
    assert canonical["apple_id"] == "apple-1"
    assert json.loads(canonical["external_ids"]) == {
        "apple_id": "apple-1",
        "device_ids": ["device-1"],
        "external_id": "ext-1",
        "facebook_id": "facebook-1",
        "google_id": "google-1",
        "line_user_id": "line-1",
        "member_code": "member-1",
    }

    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=landing.CSV_COLUMNS)
    writer.writeheader()
    writer.writerow(canonical)
    customers, stats = ingestion.prepare_customers(output.getvalue().encode())

    assert stats["records_rejected"] == 0
    customer = customers[0]
    assert customer["external_id"] == "ext-1"
    assert customer["nickname"] == "Enchantress of Numbers"
    assert customer["age"] == 36
    assert customer["line_user_id"] == "line-1"
    assert customer["facebook_id"] == "facebook-1"
    assert customer["google_id"] == "google-1"
    assert customer["apple_id"] == "apple-1"
    assert customer["member_code"] == "member-1"
    assert customer["customer_type"] == "vip"
    assert customer["status"] == "ACTIVE"
    assert customer["language"] == "th"
    assert customer["source"] == "frontend-upload"
    assert customer["source_system"] == "crm"


def test_ingestion_keeps_bad_values_rejects_only_keyless_rows_and_last_duplicate_wins(ingestion):
    output = io.StringIO(newline="")
    writer = csv.DictWriter(
        output,
        fieldnames=[
            "source_customer_id",
            "first_name",
            "date_of_birth",
            "external_ids",
        ],
    )
    writer.writeheader()
    writer.writerows(
        [
            {
                "source_customer_id": "same-id",
                "first_name": "Old",
                "date_of_birth": "2000-01-01",
                "external_ids": "{}",
            },
            {
                "source_customer_id": "same-id",
                "first_name": "New",
                "date_of_birth": "2000-01-01",
                "external_ids": "{}",
            },
            {
                "source_customer_id": "bad-date",
                "first_name": "Rejected",
                "date_of_birth": "01/31/2000",
                "external_ids": "{}",
            },
            {
                "source_customer_id": "",
                "first_name": "Rejected",
                "date_of_birth": "",
                "external_ids": "{}",
            },
        ]
    )

    customers, stats = ingestion.prepare_customers(output.getvalue().encode())

    # The unparseable date no longer rejects its row; only the keyless row is dropped.
    assert stats == {
        "records_received": 4,
        "records_rejected": 1,
        "duplicate_rows": 1,
        "records_normalised": 1,
        "normalised_fields": {"date_of_birth": 1},
    }
    by_id = {customer["source_customer_id"]: customer for customer in customers}
    assert sorted(by_id) == ["bad-date", "same-id"]
    assert by_id["same-id"]["first_name"] == "New"
    assert by_id["bad-date"]["date_of_birth"] is None
    assert by_id["bad-date"]["attributes"]["source_invalid_values"] == {"date_of_birth": "01/31/2000"}


def test_ingestion_preserves_shared_external_ids_for_stitching(ingestion):
    customers, stats = ingestion.prepare_customers(
        (
            "source_customer_id,external_id,first_name\n"
            "CUST-000008,STO-000008,Customer\n"
            "GUEST-000008-4,STO-000008,Guest\n"
        ).encode()
    )

    assert stats == {
        "records_received": 2,
        "records_rejected": 0,
        "duplicate_rows": 0,
        "records_normalised": 0,
        "normalised_fields": {},
    }
    assert [customer["source_customer_id"] for customer in customers] == [
        "CUST-000008",
        "GUEST-000008-4",
    ]
    assert {customer["external_id"] for customer in customers} == {
        "STO-000008"
    }


def test_ingestion_keeps_legacy_csv_compatible_during_cutover(ingestion):
    csv_bytes = (
        "customer_id,first_name,last_name,email,phone_number,gender,dob,"
        "signup_date,address,city,state,country,device_id(s),source\n"
        "legacy-1,Ada,Lovelace,,0812345678,,2000-01-01,2026-08-05,"
        "1 Main St,Bangkok,Bangkok,TH,device-1;device-2,crm\n"
    ).encode()

    customers, stats = ingestion.prepare_customers(csv_bytes)

    assert stats["records_rejected"] == 0
    assert customers[0]["source_customer_id"] == "legacy-1"
    assert customers[0]["external_ids"] == {
        "device_ids": ["device-1", "device-2"]
    }
    assert customers[0]["attributes"] == {"acquisition_source": "crm"}


def test_ingestion_upsert_matches_the_source_scoped_schema(
        ingestion, monkeypatch
):
    captured = {}

    def capture_execute_values(cursor, sql, rows, **kwargs):
        captured.update(sql=sql, rows=rows, kwargs=kwargs)

    extras = sys.modules["psycopg2.extras"]
    monkeypatch.setattr(extras, "execute_values", capture_execute_values)
    ingestion._upsert_customers(
        object(),
        [
            {
                "source_customer_id": "crm-1",
                "external_id": None,
                "first_name": "Ada",
                "middle_name": None,
                "last_name": "Lovelace",
                "full_name": "Ada Lovelace",
                "nickname": None,
                "email": None,
                "phone": None,
                "address_line_1": None,
                "address_line_2": None,
                "district": None,
                "province": None,
                "postal_code": None,
                "country": None,
                "date_of_birth": None,
                "birthday": None,
                "age": None,
                "gender": None,
                "line_user_id": None,
                "facebook_id": None,
                "google_id": None,
                "apple_id": None,
                "member_code": None,
                "customer_type": None,
                "status": "ACTIVE",
                "language": None,
                "source": None,
                "source_system": None,
                "external_ids": {},
                "attributes": {},
                "raw_payload": {"source_customer_id": "crm-1"},
                "source_created_at": None,
                "source_updated_at": None,
            }
        ],
        "00000000-0000-0000-0000-000000000001",
        34,
    )

    assert "ON CONFLICT (source_system_id, source_customer_id)" in captured["sql"]
    assert "source_system_id, customer_id," not in captured["sql"]
    assert len(captured["rows"][0]) == 37
    assert captured["kwargs"]["template"].count("%s") == 37


def test_ingestion_builds_upsert_rows_one_page_at_a_time(
        ingestion, monkeypatch
):
    customers, _ = ingestion.prepare_customers(
        (
            "source_customer_id,first_name\n"
            "crm-1,Ada\n"
            "crm-2,Grace\n"
            "crm-3,Katherine\n"
        ).encode()
    )
    captured_page_sizes = []

    def capture_execute_values(cursor, sql, rows, **kwargs):
        captured_page_sizes.append(len(rows))

    extras = sys.modules["psycopg2.extras"]
    monkeypatch.setattr(extras, "execute_values", capture_execute_values)
    monkeypatch.setenv("UPSERT_PAGE_SIZE", "2")

    ingestion._upsert_customers(
        object(),
        customers,
        "00000000-0000-0000-0000-000000000001",
        34,
    )

    assert captured_page_sizes == [2, 1]


def test_ingestion_records_members_by_new_batch_index(ingestion):
    class Cursor:
        def __init__(self):
            self.executions = []

        def execute(self, sql, params=None):
            self.executions.append((sql, params))

        def fetchone(self):
            return (3,)

    cursor = Cursor()
    batch_id = "00000000-0000-0000-0000-000000000001"

    ingestion._record_batch_members(cursor, batch_id, 34, 3)

    insert_sql, insert_params = cursor.executions[0]
    assert "WHERE batch_id = %s" in insert_sql
    assert "ANY(" not in insert_sql
    assert insert_params == (batch_id, batch_id, 34)


def test_ingestion_requires_complete_landing_metadata(ingestion):
    metadata = ingestion._landing_metadata(
        {
            "Metadata": {
                "landing-batch-id": "cb0e70a2-685a-4661-a883-f96f8e9ebf1b",
                "source-code": "crm",
                "content-sha256": "a" * 64,
                "row-count": "60",
            }
        }
    )

    assert metadata["landing-batch-id"] == (
        "cb0e70a2-685a-4661-a883-f96f8e9ebf1b"
    )
    with pytest.raises(ValueError, match="missing required landing metadata"):
        ingestion._landing_metadata({"Metadata": {"source-code": "crm"}})


def test_ingestion_fails_closed_on_landing_lineage_mismatch(ingestion):
    class Cursor:
        def execute(self, sql, params):
            self.params = params

        def fetchone(self):
            return (
                "batch-ref-1",
                "crm",
                "PUBLISHED",
                "raw/crm/landing-1/customer.csv",
                "b" * 64,
                1,
            )

    metadata = {
        "landing-batch-id": "cb0e70a2-685a-4661-a883-f96f8e9ebf1b",
        "source-code": "crm",
        "content-sha256": "a" * 64,
        "row-count": "1",
    }

    with pytest.raises(ValueError, match="Landing batch checksum"):
        ingestion._resolve_landing_lineage(
            Cursor(),
            landing_metadata=metadata,
            source_code="crm",
            object_key="raw/crm/landing-1/customer.csv",
            checksum="a" * 64,
            loaded_rows=1,
        )


def test_ingestion_resolves_single_essential_ecs_container(ingestion):
    class Ecs:
        def describe_task_definition(self, **kwargs):
            assert kwargs == {"taskDefinition": "task:3"}
            return {
                "taskDefinition": {
                    "containerDefinitions": [
                        {"name": "stitcher", "essential": True},
                        {"name": "sidecar", "essential": False},
                    ]
                }
            }

    assert ingestion._resolve_ecs_container_name(Ecs(), "task:3") == "stitcher"


def test_ingestion_starts_ecs_with_batch_and_job_overrides(
        ingestion, monkeypatch
):
    captured = {}

    class Ecs:
        def describe_task_definition(self, **kwargs):
            return {
                "taskDefinition": {
                    "containerDefinitions": [
                        {"name": "stitcher", "essential": True}
                    ]
                }
            }

        def run_task(self, **kwargs):
            captured.update(kwargs)
            return {"tasks": [{"taskArn": "arn:task/1"}], "failures": []}

    monkeypatch.setattr(ingestion.boto3, "client", lambda name: Ecs())
    monkeypatch.setenv("ECS_CLUSTER", "cluster")
    monkeypatch.setenv("ECS_TASK_DEFINITION", "task:3")
    monkeypatch.setenv("ECS_SUBNET_IDS", "subnet-1")
    monkeypatch.setenv("ECS_SECURITY_GROUP_IDS", "sg-1")
    monkeypatch.delenv("ECS_CONTAINER_NAME", raising=False)

    task_arn = ingestion._start_stitching_task("batch-1", "job-1")

    assert task_arn == "arn:task/1"
    assert captured["startedBy"] == "job-1"
    assert captured["overrides"]["containerOverrides"][0]["command"] == [
        "run",
        "--batch-id",
        "batch-1",
        "--job-id",
        "job-1",
    ]


def test_dispatch_recording_preserves_fargate_progress(ingestion):
    class Cursor:
        rowcount = 1

        def __init__(self):
            self.executions = []

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, sql, params=None):
            self.executions.append((sql, params))

    class Connection:
        def __init__(self):
            self.cursor_instance = Cursor()

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def cursor(self):
            return self.cursor_instance

    connection = Connection()
    ingestion._record_stitching_dispatch(
        connection,
        "batch-1",
        "job-1",
        "arn:task/1",
    )

    job_sql, job_params = connection.cursor_instance.executions[0]
    assert "WHEN status = 'QUEUED' THEN 'SUBMITTED'" in job_sql
    assert "'WAITING_FOR_LOCK'" in job_sql
    assert "'RUNNING'" in job_sql
    assert job_params == ("arn:task/1", "job-1", "batch-1", "arn:task/1")


def test_duplicate_ingestion_event_reuses_active_stitching_job(
        ingestion, monkeypatch
):
    monkeypatch.setattr(
        ingestion,
        "_ensure_stitching_job",
        lambda connection, batch_id: {
            "job_id": "job-1",
            "attempt": 1,
            "status": "SUBMITTED",
            "task_arn": "arn:task/1",
        },
    )
    monkeypatch.setattr(
        ingestion,
        "_start_stitching_task",
        lambda *args: pytest.fail("duplicate event must not start another task"),
    )

    result = ingestion._dispatch_stitching(
        {"status": "skipped", "batch_id": "batch-1"},
        object(),
    )

    assert result == {
        "status": "already_submitted",
        "batch_id": "batch-1",
        "job_id": "job-1",
        "task_arn": "arn:task/1",
    }


def test_ecs_stopped_event_marks_nonterminal_job_failed(ingestion):
    class Cursor:
        rowcount = 1

        def __init__(self):
            self.executions = []

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, sql, params):
            self.sql = sql
            self.executions.append((sql, params))

        def fetchone(self):
            return ("job-1", "batch-1", "SUBMITTED")

    class Connection:
        def __init__(self):
            self.cursor_instance = Cursor()

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def cursor(self):
            return self.cursor_instance

    connection = Connection()
    result = ingestion._handle_ecs_task_state_change(
        {
            "detail": {
                "lastStatus": "STOPPED",
                "taskArn": "arn:task/1",
                "startedBy": "job-1",
                "stopCode": "EssentialContainerExited",
                "stoppedReason": "container failed",
                "containers": [{"exitCode": 1}],
            }
        },
        connection,
    )

    assert result == {"status": "failed", "job_id": "job-1", "exit_code": 1}
    assert any(
        "UPDATE identity.resolution_run" in sql
        for sql, _ in connection.cursor_instance.executions
    )


def test_ecs_oom_reason_is_preserved_as_specific_error(ingestion):
    class Cursor:
        rowcount = 1

        def __init__(self):
            self.executions = []

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, sql, params):
            self.executions.append((sql, params))

        def fetchone(self):
            return ("job-1", "batch-1", "RUNNING")

    class Connection:
        def __init__(self):
            self.cursor_instance = Cursor()

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def cursor(self):
            return self.cursor_instance

    connection = Connection()
    ingestion._handle_ecs_task_state_change(
        {
            "detail": {
                "lastStatus": "STOPPED",
                "taskArn": "arn:task/oom",
                "startedBy": "job-1",
                "stopCode": "EssentialContainerExited",
                "stoppedReason": "Essential container in task exited",
                "containers": [
                    {
                        "exitCode": 137,
                        "reason": "OutOfMemoryError: container killed due to memory usage",
                    }
                ],
            }
        },
        connection,
    )

    job_update = next(
        params
        for sql, params in connection.cursor_instance.executions
        if "UPDATE control.stitching_job" in sql
    )
    resolution_update = next(
        params
        for sql, params in connection.cursor_instance.executions
        if "UPDATE identity.resolution_run" in sql
    )
    assert "OutOfMemoryError" in job_update
    assert resolution_update[0].startswith("OutOfMemoryError:")


def test_ecs_stopped_event_does_not_downgrade_success(ingestion):
    class Cursor:
        rowcount = 0

        def __init__(self):
            self.execution_count = 0

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, sql, params):
            self.execution_count += 1

        def fetchone(self):
            return ("job-1", "batch-1", "SUCCEEDED")

    class Connection:
        def __init__(self):
            self.cursor_instance = Cursor()

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def cursor(self):
            return self.cursor_instance

    connection = Connection()
    result = ingestion._handle_ecs_task_state_change(
        {
            "detail": {
                "lastStatus": "STOPPED",
                "taskArn": "arn:task/1",
                "startedBy": "job-1",
                "stopCode": "EssentialContainerExited",
                "containers": [{"exitCode": 1}],
            }
        },
        connection,
    )

    assert result == {
        "status": "unchanged",
        "job_id": "job-1",
        "job_status": "SUCCEEDED",
    }
    assert connection.cursor_instance.execution_count == 1


def test_ingestion_seeds_arrival_facts_before_customer_upsert(
        ingestion, monkeypatch
):
    calls = []

    class Body:
        def read(self):
            return b"source_customer_id,first_name\ncrm-1,Ada\n"

        def close(self):
            pass

    class S3:
        def get_object(self, **kwargs):
            assert kwargs["IfMatch"] == "etag-1"
            return {"Body": Body(), "Metadata": {}}

    class Cursor:
        rowcount = 1

        def __init__(self):
            self.rows = iter([(34,), ("ingestion-1",), ("job-1",)])

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, sql, params=None):
            self.sql = sql

        def fetchone(self):
            return next(self.rows)

    class Connection:
        def __init__(self):
            self.cursor_instance = Cursor()

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def cursor(self):
            return self.cursor_instance

    monkeypatch.setattr(
        ingestion,
        "_landing_metadata",
        lambda response: {"landing-batch-id": "landing-1"},
    )
    monkeypatch.setattr(
        ingestion,
        "_resolve_landing_lineage",
        lambda *args, **kwargs: ("landing-1", "batch-ref-1"),
    )
    monkeypatch.setattr(
        ingestion,
        "_create_validation_run",
        lambda *args, **kwargs: calls.append("create_run") or "validation-1",
    )
    monkeypatch.setattr(
        ingestion,
        "_seed_validation_records",
        lambda *args, **kwargs: calls.append("seed_records"),
    )
    monkeypatch.setattr(
        ingestion,
        "_create_existing_source_snapshot",
        lambda *args, **kwargs: calls.append("snapshot_existing"),
    )
    monkeypatch.setattr(
        ingestion,
        "_upsert_customers",
        lambda *args, **kwargs: calls.append("upsert_customers"),
    )
    monkeypatch.setattr(
        ingestion,
        "_record_batch_members",
        lambda *args, **kwargs: calls.append("record_members"),
    )

    result = ingestion._process_record(
        {
            "s3": {
                "bucket": {"name": "bucket"},
                "object": {
                    "key": "raw/crm/landing-1/customer.csv",
                    "eTag": "etag-1",
                },
            }
        },
        S3(),
        Connection(),
    )

    assert result["validation_run_id"] == "validation-1"
    assert calls == [
        "create_run",
        "snapshot_existing",
        "upsert_customers",
        "seed_records",
        "record_members",
    ]


def test_validation_record_seed_keeps_row_outcomes_without_copying_pii(
        ingestion
):
    class Cursor:
        def execute(self, sql, params):
            self.sql = sql
            self.params = params

    cursor = Cursor()
    ingestion._seed_validation_records(
        cursor,
        validation_run_id="validation-1",
        landing_batch_id="landing-1",
        source_system_id=34,
    )

    insert_columns = cursor.sql.split(
        "INSERT INTO quality.customer_validation_record (", 1
    )[1].split(")\n        SELECT %s", 1)[0]
    assert "source_row_number" in insert_columns
    assert "selected_for_canonical" in insert_columns
    assert "record_fingerprint" in insert_columns
    assert "raw_payload" not in insert_columns
    assert "digest(source.raw_payload::text, 'sha256')" in cursor.sql
    assert "source_key_rank = 1" in cursor.sql
    ranked_sql = cursor.sql.split("WITH ranked AS (", 1)[1].split(
        "), classified AS (", 1
    )[0]
    assert "raw_payload" not in ranked_sql
    assert "validation_errors" not in ranked_sql
    assert "JOIN raw.customer_landing_source AS source" in cursor.sql
    assert "LEFT JOIN raw.customer_current AS customer" in cursor.sql
    assert "existing_source_record" in insert_columns
    assert "ORDER BY ranked.source_row_number" not in cursor.sql


def test_validation_run_stats_scan_landing_rows_once(ingestion):
    class Cursor:
        def execute(self, sql, params):
            self.sql = sql
            self.params = params

        def fetchone(self):
            return ("validation-1",)

    cursor = Cursor()
    result = ingestion._create_validation_run(
        cursor,
        landing_batch_id="landing-1",
        ingestion_batch_id="ingestion-1",
        stitching_job_id="job-1",
        attempt=1,
    )

    assert result == "validation-1"
    assert cursor.sql.count("FROM raw.customer_landing_source") == 1
    assert "GROUP BY NULLIF(btrim(source_customer_id), '')" in cursor.sql


def test_existing_source_snapshot_is_temporary_and_analyzed(ingestion):
    class Cursor:
        def __init__(self):
            self.executions = []

        def execute(self, sql, params=None):
            self.executions.append((sql, params))

    cursor = Cursor()
    ingestion._create_existing_source_snapshot(
        cursor,
        landing_batch_id="landing-1",
        source_system_id=34,
    )

    sql = "\n".join(item[0] for item in cursor.executions)
    assert "CREATE TEMP TABLE ingestion_existing_source_snapshot" in sql
    assert "ON COMMIT DROP" in sql
    assert "ANALYZE ingestion_existing_source_snapshot" in sql


def test_retry_copies_first_attempt_arrival_facts(ingestion, monkeypatch):
    calls = []

    class Cursor:
        rowcount = 1

        def __init__(self):
            self.rows = iter([
                ("job-1", 1, "FAILED", "arn:task/1", "validation-1", "landing-1"),
                ("job-2",),
            ])

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, sql, params=None):
            calls.append(("sql", sql, params))

        def fetchone(self):
            return next(self.rows)

    class Connection:
        def __init__(self):
            self.cursor_instance = Cursor()

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def cursor(self):
            return self.cursor_instance

    monkeypatch.setattr(
        ingestion,
        "_create_validation_run",
        lambda *args, **kwargs: calls.append(("create", kwargs)) or "validation-2",
    )
    monkeypatch.setattr(
        ingestion,
        "_copy_validation_records",
        lambda *args, **kwargs: calls.append(("copy", kwargs)),
    )

    result = ingestion._ensure_stitching_job(Connection(), "ingestion-1")

    assert result == {
        "job_id": "job-2",
        "attempt": 2,
        "status": "QUEUED",
        "task_arn": None,
        "validation_run_id": "validation-2",
    }
    copy_call = next(item for item in calls if item[0] == "copy")
    assert copy_call[1] == {
        "validation_run_id": "validation-2",
        "previous_validation_run_id": "validation-1",
    }


def test_ecs_stopped_event_preserves_existing_validation_failure(ingestion):
    class Cursor:
        rowcount = 0

        def __init__(self):
            self.execution_count = 0

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, sql, params):
            self.execution_count += 1

        def fetchone(self):
            return ("job-1", "batch-1", "FAILED")

    class Connection:
        def __init__(self):
            self.cursor_instance = Cursor()

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def cursor(self):
            return self.cursor_instance

    connection = Connection()
    result = ingestion._handle_ecs_task_state_change(
        {
            "detail": {
                "lastStatus": "STOPPED",
                "taskArn": "arn:task/1",
                "startedBy": "job-1",
                "stopCode": "EssentialContainerExited",
                "stoppedReason": "generic ECS reason",
                "containers": [{"exitCode": 1}],
            }
        },
        connection,
    )

    assert result == {
        "status": "unchanged",
        "job_id": "job-1",
        "job_status": "FAILED",
    }
    assert connection.cursor_instance.execution_count == 1


def test_database_artifacts_parse_as_postgresql_sql():
    migration = PROJECT_ROOT / "database/migrations/013_single_customer_source.sql"
    verification = PROJECT_ROOT / "database/queries/verify_customer_pipeline.sql"
    tracking_migration = (
        PROJECT_ROOT / "database/migrations/014_pipeline_tracking.sql"
    )
    tracking_verification = (
        PROJECT_ROOT / "database/queries/verify_pipeline_tracking.sql"
    )
    quality_migration = (
        PROJECT_ROOT / "database/migrations/015_customer_data_quality.sql"
    )
    shared_external_id_migration = (
        PROJECT_ROOT
        / "database/migrations/016_shared_customer_external_ids.sql"
    )
    quality_verification = (
        PROJECT_ROOT / "database/queries/verify_customer_data_quality.sql"
    )

    assert len(parse_sql(migration.read_text())) == 13
    assert len(parse_sql(verification.read_text())) == 6
    assert len(parse_sql(tracking_migration.read_text())) == 24
    assert len(parse_sql(tracking_verification.read_text())) == 7
    assert len(parse_sql(quality_migration.read_text())) == 30
    assert len(parse_sql(quality_verification.read_text())) == 7
    assert len(parse_sql(shared_external_id_migration.read_text())) == 5


def test_ingestion_stack_allocates_bulk_processing_memory():
    config = (PROJECT_ROOT / "env.yaml").read_text()
    ingestion_config = config.split(
        'stack_name: "CdpIngestionData"', 1
    )[1].split("# Second Lambda stack", 1)[0]

    assert "memory_size: 2048" in ingestion_config
    assert "reserved_concurrent_executions" not in ingestion_config
    assert 'INGESTION_STATEMENT_TIMEOUT: "13min"' in ingestion_config
    assert 'INGESTION_WORK_MEM: "32MB"' in ingestion_config
    assert 'UPSERT_PAGE_SIZE: "1000"' in ingestion_config
    assert "pandas-layer" not in ingestion_config


def test_external_ids_are_lookup_signals_not_raw_customer_keys():
    migration = (
        PROJECT_ROOT
        / "database/migrations/016_shared_customer_external_ids.sql"
    ).read_text()

    assert (
        "DROP INDEX IF EXISTS raw.uq_customer_source_external_id" in migration
    )
    assert (
        "CREATE INDEX IF NOT EXISTS idx_raw_customer_source_external_id"
        in migration
    )
    assert "CREATE UNIQUE INDEX" not in migration


def test_quality_views_are_latest_attempt_and_do_not_expose_raw_pii():
    migration = (
        PROJECT_ROOT / "database/migrations/015_customer_data_quality.sql"
    ).read_text()
    metric_view = migration.split(
        "CREATE OR REPLACE VIEW control.customer_data_quality_metric AS", 1
    )[1].split(
        "CREATE OR REPLACE VIEW control.customer_data_quality_record AS", 1
    )[0]
    record_view = migration.split(
        "CREATE OR REPLACE VIEW control.customer_data_quality_record AS", 1
    )[1].split(
        "CREATE OR REPLACE VIEW control.customer_pipeline_status AS", 1
    )[0]
    pipeline_view = migration.split(
        "CREATE OR REPLACE VIEW control.customer_pipeline_status AS", 1
    )[1]

    for view_sql in (metric_view, record_view):
        assert "raw_payload" not in view_sql
        assert "email_raw" not in view_sql
        assert "phone_raw" not in view_sql
    assert "ORDER BY candidate.attempt DESC" in pipeline_view
    assert "validation_available" in pipeline_view
    assert "vr.error_stage" in pipeline_view


def test_terminal_validation_snapshots_reject_all_child_mutations():
    migration = (
        PROJECT_ROOT / "database/migrations/015_customer_data_quality.sql"
    ).read_text()

    assert migration.count("BEFORE INSERT OR UPDATE OR DELETE") == 2
    assert "OLD.run_status IN ('SUCCEEDED', 'FAILED')" in migration
    assert "ON DELETE SET NULL" not in migration
