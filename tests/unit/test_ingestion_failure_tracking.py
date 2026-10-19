"""Failure bookkeeping must commit without acknowledging the S3 event."""
import importlib.util
import io
import sys
import types
from pathlib import Path

import pytest


@pytest.fixture
def ingestion(monkeypatch):
    extras = types.ModuleType('psycopg2.extras')
    extras.Json = lambda value: value
    monkeypatch.setitem(sys.modules, 'psycopg2.extras', extras)
    path = Path(__file__).resolve().parents[2] / '_lambda/ingestion_data/lambda_function.py'
    spec = importlib.util.spec_from_file_location('failure_ingestion', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Connection:
    """Transaction double: statements retained only on successful context exit."""
    def __init__(self):
        self.sql = []
        self.committed = []
        self.rows = iter([(1,), ('batch-1',), ('job-1',)])
        self.rowcount = 1

    def __enter__(self):
        return self

    def __exit__(self, typ, exc, tb):
        if typ is None:
            self.committed = list(self.sql)
        return False

    def cursor(self):
        return Cursor(self)


class Cursor:
    def __init__(self, connection):
        self.connection = connection
        self.rowcount = 1

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params=None):
        self.connection.sql.append((sql, params))

    def fetchone(self):
        return next(self.connection.rows)


def setup_ingestion(ingestion, monkeypatch):
    monkeypatch.setattr(ingestion, '_landing_metadata', lambda _: {'landing-batch-id': 'landing-1'})
    monkeypatch.setattr(ingestion, '_resolve_landing_lineage', lambda *a, **kw: ('landing-1', 'ref-1'))
    monkeypatch.setattr(ingestion, '_create_validation_run', lambda *a, **kw: 'validation-1')
    monkeypatch.setattr(ingestion, '_create_existing_source_snapshot', lambda *a, **kw: None)
    monkeypatch.setattr(ingestion, '_seed_validation_records', lambda *a, **kw: None)
    monkeypatch.setattr(ingestion, '_record_batch_members', lambda *a, **kw: None)
    event = {'s3': {'bucket': {'name': 'bucket'}, 'object': {'key': 'raw/crm/customer.csv', 'eTag': 'etag'}}}
    s3 = types.SimpleNamespace(get_object=lambda **kw: {'Body': io.BytesIO(b'source_customer_id,status\n1,SUSPENDED\n')})
    return event, s3


def test_upsert_failure_commits_failure_and_reraises(ingestion, monkeypatch):
    event, s3 = setup_ingestion(ingestion, monkeypatch)
    error = RuntimeError('sensitive source row must not be persisted')
    def fail(*args):
        raise error
    monkeypatch.setattr(ingestion, '_upsert_customers', fail)
    connection = Connection()
    with pytest.raises(RuntimeError) as caught:
        ingestion._process_record(event, s3, connection)
    assert 'sensitive source row' not in str(caught.value)
    assert caught.value.__suppress_context__
    assert any("status = 'failed'" in sql for sql, _ in connection.committed)
    assert any('ROLLBACK TO SAVEPOINT ingestion_payload' in sql for sql, _ in connection.committed)
    assert 'sensitive source row' not in str(connection.committed)


def test_failed_payload_can_be_reclaimed_without_replaying_completed(ingestion, monkeypatch):
    event, s3 = setup_ingestion(ingestion, monkeypatch)
    monkeypatch.setattr(ingestion, '_upsert_customers', lambda *args: None)
    connection = Connection()
    ingestion._process_record(event, s3, connection)
    sql = next(sql for sql, _ in connection.committed if 'INSERT INTO control.ingestion_batch' in sql)
    assert 'DO UPDATE SET' in sql
    assert "ingestion_batch.status = 'failed'" in sql
    assert "ingestion_batch.metadata ? 'ingestion_error_code'" in sql
    assert 'ingestion_batch.landing_batch_id = EXCLUDED.landing_batch_id' in sql
