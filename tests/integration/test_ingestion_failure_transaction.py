"""Real PostgreSQL regression; CDP_TEST_DSN must point to a test connection.

Only session-local temporary tables are created. No application rows are read
or written. Run separately from unit tests (which stub psycopg2).
"""
import importlib.util
import io
import os
from pathlib import Path
from types import SimpleNamespace

import pytest


def test_failure_rollback_retry_and_completed_duplicate(monkeypatch):
    dsn = os.getenv('CDP_TEST_DSN')
    if not dsn:
        pytest.skip('CDP_TEST_DSN not configured')
    import psycopg2
    spec = importlib.util.spec_from_file_location('real_ingestion', Path(__file__).resolve().parents[2] / '_lambda/ingestion_data/lambda_function.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    connection = psycopg2.connect(dsn)
    try:
        with connection, connection.cursor() as cursor:
            cursor.execute('''
                CREATE TEMP TABLE source_system (source_system_id serial primary key, source_code text unique, source_name text, source_type text, is_active boolean);
                CREATE TEMP TABLE ingestion_batch (batch_id uuid primary key, source_system_id int, object_name text, file_name text, status text, checksum text, metadata jsonb, batch_ref text, landing_batch_id uuid, completed_at timestamptz, updated_at timestamptz, records_received int, records_loaded int, records_rejected int, UNIQUE(source_system_id, object_name, checksum));
                CREATE TEMP TABLE stitching_job (job_id uuid default gen_random_uuid(), ingestion_batch_id uuid, attempt int, status text);
                CREATE TEMP TABLE customer_validation_run (validation_run_id text, selected_record_count int, existing_source_record_count int, updated_at timestamptz, run_status text);
                CREATE TEMP TABLE customer_validation_record (validation_run_id text, existing_source_record boolean, selected_for_canonical boolean);
                CREATE TEMP TABLE customer (status text CHECK(status = 'ACTIVE'));
                INSERT INTO customer_validation_run VALUES ('validation-1', 0, 0, now(), 'PENDING');
            ''')
        class Cursor:
            def __enter__(self):
                self.cursor = connection.cursor()
                return self
            def __exit__(self, *args):
                self.cursor.close()
            def execute(self, sql, params=None):
                for schema in ['control.', 'quality.']:
                    sql = sql.replace(schema, 'pg_temp.')
                self.cursor.execute(sql, params)
            def fetchone(self):
                return self.cursor.fetchone()
            @property
            def rowcount(self):
                return self.cursor.rowcount
        class Connection:
            def __enter__(self):
                connection.__enter__()
                return self
            def __exit__(self, *args):
                return connection.__exit__(*args)
            def cursor(self):
                return Cursor()
        landing_id = '11111111-1111-1111-1111-111111111111'
        monkeypatch.setattr(module, '_landing_metadata', lambda _: {'landing-batch-id': landing_id})
        monkeypatch.setattr(module, '_resolve_landing_lineage', lambda *a, **k: (landing_id, 'ref'))
        monkeypatch.setattr(module, '_create_validation_run', lambda *a, **k: 'validation-1')
        for name in ['_create_existing_source_snapshot', '_seed_validation_records', '_record_batch_members']:
            monkeypatch.setattr(module, name, lambda *a, **k: None)
        def upsert(cursor, *args):
            cursor.execute("INSERT INTO pg_temp.customer VALUES ('SUSPENDED')")
        monkeypatch.setattr(module, '_upsert_customers', upsert)
        event = {'s3': {'bucket': {'name': 'test'}, 'object': {'key': 'raw/crm/customer.csv', 'eTag': 'etag'}}}
        s3 = SimpleNamespace(get_object=lambda **k: {'Body': io.BytesIO(b'source_customer_id,status\n1,SUSPENDED\n')})
        for _ in range(2):
            with pytest.raises(RuntimeError, match='CheckViolation'):
                module._process_record(event, s3, Connection())
            with connection, connection.cursor() as cursor:
                cursor.execute('SELECT batch_id, status, metadata FROM pg_temp.ingestion_batch')
                rows = cursor.fetchall()
                assert len(rows) == 1 and rows[0][1] == 'failed'
                assert rows[0][2]['ingestion_error_code'] == 'CheckViolation'
                batch_id = str(rows[0][0])
                cursor.execute('SELECT count(*) FROM pg_temp.customer')
                assert cursor.fetchone()[0] == 0
                cursor.execute('SELECT count(*) FROM pg_temp.stitching_job')
                assert cursor.fetchone()[0] == 0
        with connection, connection.cursor() as cursor:
            cursor.execute('ALTER TABLE pg_temp.customer DROP CONSTRAINT customer_status_check')
        result = module._process_record(event, s3, Connection())
        assert result['status'] == 'completed' and result['batch_id'] == batch_id
        result = module._process_record(event, s3, Connection())
        assert result['status'] == 'skipped' and result['batch_id'] == batch_id
        with connection, connection.cursor() as cursor:
            cursor.execute('SELECT count(*) FROM pg_temp.customer')
            assert cursor.fetchone()[0] == 1
            cursor.execute('SELECT count(*) FROM pg_temp.stitching_job')
            assert cursor.fetchone()[0] == 1
            cursor.execute('SELECT metadata FROM pg_temp.ingestion_batch')
            assert 'ingestion_error_code' not in cursor.fetchone()[0]
    finally:
        connection.close()
