"""Forward-only physical source routing at the canonical ingestion transaction."""

import importlib.util
import sys
from pathlib import Path

import pytest


MODULE = Path(__file__).resolve().parents[2] / '_lambda/ingestion_data/lambda_function.py'
spec = importlib.util.spec_from_file_location('customer_forward_ingestion', MODULE)
assert spec is not None and spec.loader is not None
ingestion = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ingestion)

LANDING_ID = '11111111-1111-1111-1111-111111111111'
KEY = f'raw/crm/{LANDING_ID}/customer.csv'
CHECKSUM = 'f' * 64
REF = '2026-09-27-123456-00042-deadbeefcafe'


class Cursor:
    rowcount = 1

    def __init__(self, *, forward_installed=True, old_installed=False, physical_id=101,
                 landing_ref=REF, landing_status='PUBLISHED',
                 batch_inserted=True, existing_status='completed'):
        self.forward_installed = forward_installed
        self.old_installed = old_installed
        self.physical_id = physical_id
        self.landing_ref = landing_ref
        self.landing_status = landing_status
        self.batch_inserted = batch_inserted
        self.existing_status = existing_status
        self.executions = []
        self.next_row = None

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def execute(self, sql, params=None):
        self.executions.append((sql, params))
        self.next_row = None
        if 'INSERT INTO control.source_system' in sql:
            assert "to_regclass('control.customer_forward_source_activation') IS NOT NULL" in sql
            assert params is not None
            self.next_row = (20,) if self.forward_installed == params[2] else None
        elif 'FROM control.source_landing_batch' in sql:
            self.next_row = (self.landing_ref, 'crm', self.landing_status, KEY, CHECKSUM, 1)
        elif "to_regclass('control.customer_forward_source_activation')" in sql:
            self.next_row = (self.forward_installed, self.old_installed)
        elif 'INSERT INTO control.ingestion_batch' in sql:
            if self.batch_inserted:
                self.next_row = ('ingestion-1', self.physical_id if self.forward_installed else 20)
        elif 'FROM control.ingestion_batch' in sql and 'metadata ->>' in sql:
            self.next_row = ('ingestion-1', self.existing_status, None, LANDING_ID,
                             self.physical_id if self.forward_installed else 20)
        elif 'INSERT INTO control.stitching_job' in sql:
            self.next_row = ('job-1',)

    def fetchone(self):
        return self.next_row


class LegacyCursor(Cursor):
    def __init__(self):
        super().__init__(forward_installed=False)


class Connection:
    def __init__(self, cursor):
        self.cursor_instance = cursor
        self.commits = 0
        self.rollbacks = 0

    def __enter__(self):
        return self

    def __exit__(self, exc_type, *_):
        if exc_type:
            self.rollbacks += 1
        else:
            self.commits += 1
        return False

    def cursor(self):
        return self.cursor_instance


def _run(monkeypatch, cursor, *, before_commit=lambda *_: None, forward_enabled=True,
         bulk_enabled=True, old_enabled=False):
    calls = []
    monkeypatch.setenv('CUSTOMER_BULK_ENABLED', str(bulk_enabled).lower())
    monkeypatch.setenv('CUSTOMER_FORWARD_SOURCE_NAMESPACE_ENABLED', str(forward_enabled).lower())
    monkeypatch.setenv('CUSTOMER_SOURCE_NAMESPACE_ENABLED', str(old_enabled).lower())
    monkeypatch.setattr(ingestion, '_create_validation_run', lambda *a, **k: 'validation-1')
    monkeypatch.setattr(ingestion, '_create_existing_source_snapshot',
                        lambda cur, **kw: calls.append(('snapshot', kw)))
    monkeypatch.setattr(ingestion, '_upsert_customers',
                        lambda cur, customers, batch, source: calls.append(('bulk', batch, source)))
    monkeypatch.setattr(ingestion, '_seed_validation_records',
                        lambda cur, **kw: calls.append(('validation', kw)))
    monkeypatch.setattr(ingestion, '_record_batch_members',
                        lambda cur, batch, source, count: calls.append(('members', batch, source, count)))
    record = {'s3': {'bucket': {'name': 'test'},
                     'object': {'key': KEY, 'eTag': 'etag'}}}
    prepared = ([{'source_customer_id': 'shared'}],
                {'records_received': 1, 'records_rejected': 0, 'duplicate_rows': 0},
                CHECKSUM, {'landing-batch-id': LANDING_ID, 'source-code': 'crm',
                           'content-sha256': CHECKSUM, 'row-count': '1'})
    connection = Connection(cursor)
    return ingestion._process_record(record, None, connection, prepared=prepared,
                                     before_commit=before_commit), connection, calls


def test_verified_landing_uses_trigger_returned_physical_id_and_literal_ref(monkeypatch):
    cursor = Cursor()
    result, connection, calls = _run(monkeypatch, cursor)
    sql = [statement for statement, _ in cursor.executions]
    assert sql.index(next(s for s in sql if 'FROM control.source_landing_batch' in s)) < sql.index(
        next(s for s in sql if 'INSERT INTO control.source_system' in s)) < sql.index(
        next(s for s in sql if 'INSERT INTO control.ingestion_batch' in s))
    inserted = next(params for statement, params in cursor.executions
                    if 'INSERT INTO control.ingestion_batch' in statement)
    assert inserted[1:3] == (20, KEY)
    assert inserted[6:] == (REF, LANDING_ID)
    assert not any('customer_forward_file_source' in statement
                   for statement, _ in cursor.executions)
    assert ('bulk', 'ingestion-1', 101) in calls
    assert ('members', 'ingestion-1', 101, 1) in calls
    assert result['source_code'] == 'crm' and result['landing_batch_id'] == LANDING_ID
    assert connection.commits == 1 and connection.rollbacks == 0


def test_trigger_route_registers_legacy_then_reads_physical_from_insert_returning(monkeypatch):
    cursor = Cursor()
    result, connection, calls = _run(monkeypatch, cursor)
    sql = [statement for statement, _ in cursor.executions]
    landing = next(i for i, statement in enumerate(sql) if 'FROM control.source_landing_batch' in statement)
    registration = next(i for i, statement in enumerate(sql) if 'INSERT INTO control.source_system' in statement)
    batch = next(i for i, statement in enumerate(sql) if 'INSERT INTO control.ingestion_batch' in statement)
    assert landing < registration < batch
    assert not any('customer_forward_file_source' in statement for statement in sql)
    assert any("to_regclass('control.customer_forward_source_activation')" in statement
               for statement in sql)
    inserted = cursor.executions[batch][1]
    assert inserted[1] == 20, '038 trigger only accepts legacy source on INSERT'
    assert 'RETURNING batch_id::text, source_system_id' in sql[batch]
    assert ('bulk', 'ingestion-1', 101) in calls
    assert ('members', 'ingestion-1', 101, 1) in calls
    assert result['source_code'] == 'crm' and connection.commits == 1


def test_unactivated_schema_keeps_legacy_source_insert_and_path(monkeypatch):
    cursor = LegacyCursor()
    result, connection, calls = _run(monkeypatch, cursor, forward_enabled=False)
    assert any('INSERT INTO control.source_system' in statement for statement, _ in cursor.executions)
    inserted = next(params for statement, params in cursor.executions
                    if 'INSERT INTO control.ingestion_batch' in statement)
    assert inserted[1:3] == (20, KEY)
    assert ('bulk', 'ingestion-1', 20) in calls
    assert result['source_code'] == 'crm' and connection.commits == 1


def test_duplicate_uses_trigger_mapped_source_and_fences_skip(monkeypatch):
    cursor = Cursor(batch_inserted=False)
    fenced = []
    result, connection, calls = _run(
        monkeypatch, cursor, before_commit=lambda cur, batch: fenced.append(batch),
    )
    lookup = next((sql, params) for sql, params in cursor.executions
                  if 'FROM control.ingestion_batch' in sql and 'metadata ->>' in sql)
    assert 'landing_batch_id = %s' in lookup[0]
    assert lookup[1][0:4] == (LANDING_ID, KEY, CHECKSUM, REF)
    assert lookup[1][5] == 20 and lookup[1][7] == 20
    assert result['status'] == 'skipped' and fenced == ['ingestion-1']
    assert connection.commits == 1 and not calls



@pytest.mark.parametrize('unproven_ref', [
    '2026-09-27-123456-42-deadbeefcafeEXTRA',
    '2026-09-27-123456-+42-deadbeefcafe',
    '2026-09-27-123456-42-DEADBEEFCAFE',
    'batch-001',
])
def test_unproven_landing_ref_never_reaches_source_registration(monkeypatch, unproven_ref):
    cursor = Cursor(landing_ref=unproven_ref)
    with pytest.raises((ValueError, RuntimeError), match='batch.ref|batch_ref|lineage|app'):
        _run(monkeypatch, cursor)
    assert not any('INSERT INTO control.source_system' in statement or
                   'INSERT INTO control.ingestion_batch' in statement
                   for statement, _ in cursor.executions)


@pytest.mark.parametrize('installed,enabled', [(False, True), (True, False)])
def test_activation_disagreement_fails_before_ingestion_batch(monkeypatch, installed, enabled):
    cursor = Cursor(forward_installed=installed)
    with pytest.raises(RuntimeError, match='activation mismatch'):
        _run(monkeypatch, cursor, forward_enabled=enabled)
    assert not any('INSERT INTO control.ingestion_batch' in statement
                   for statement, _ in cursor.executions)


@pytest.mark.parametrize('old_enabled,old_installed', [(True, False), (False, True)])
def test_forward_route_refuses_old_037_activation(monkeypatch, old_enabled, old_installed):
    cursor = Cursor(old_installed=old_installed)
    with pytest.raises(RuntimeError, match='mutually exclusive'):
        _run(monkeypatch, cursor, old_enabled=old_enabled)
    assert not any('INSERT INTO control.source_system' in statement or
                   'INSERT INTO control.ingestion_batch' in statement
                   for statement, _ in cursor.executions)


def test_forward_route_requires_bulk_before_batch(monkeypatch):
    cursor = Cursor()
    with pytest.raises(RuntimeError, match='bulk writer'):
        _run(monkeypatch, cursor, bulk_enabled=False)
    assert not any('INSERT INTO control.source_system' in statement or
                   'INSERT INTO control.ingestion_batch' in statement
                   for statement, _ in cursor.executions)


@pytest.mark.parametrize('physical_id', [None, 0, -1, '101', True])
def test_forward_route_refuses_nonphysical_trigger_result(monkeypatch, physical_id):
    cursor = Cursor(physical_id=physical_id)
    with pytest.raises(RuntimeError, match='no physical source'):
        _run(monkeypatch, cursor)
    assert any('INSERT INTO control.ingestion_batch' in statement
               for statement, _ in cursor.executions)


def test_forward_route_cannot_use_legacy_direct_s3_lambda(monkeypatch):
    monkeypatch.setenv('CUSTOMER_FORWARD_SOURCE_NAMESPACE_ENABLED', 'true')
    monkeypatch.setenv('CUSTOMER_INGESTION_ENABLED', 'false')
    monkeypatch.setattr(ingestion, '_connect', lambda: pytest.fail('old upload bypass opened DB'))
    monkeypatch.setattr(ingestion.boto3, 'client',
                        lambda *_: pytest.fail('old upload bypass opened S3'))
    with pytest.raises(RuntimeError, match='durable|worker|legacy'):
        ingestion.lambda_handler({'Records': [{'s3': {'bucket': {'name': 'test'},
                                                  'object': {'key': KEY, 'eTag': 'etag'}}}]}, None)


def test_forward_flag_refuses_legacy_page_upsert(monkeypatch):
    monkeypatch.setenv('CUSTOMER_FORWARD_SOURCE_NAMESPACE_ENABLED', 'true')
    monkeypatch.setenv('CUSTOMER_BULK_ENABLED', 'false')
    with pytest.raises(RuntimeError, match='bulk writer'):
        ingestion._upsert_customers(object(), [{'source_customer_id': 'shared'}], 'batch', 101)


def test_forward_bulk_uses_only_new_privileged_merge_entrypoint(monkeypatch):
    spec = importlib.util.spec_from_file_location('customer_bulk', MODULE.with_name('customer_bulk.py'))
    assert spec is not None and spec.loader is not None
    bulk = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bulk)
    monkeypatch.setitem(sys.modules, 'customer_bulk', bulk)
    monkeypatch.setenv('CUSTOMER_FORWARD_SOURCE_NAMESPACE_ENABLED', 'true')
    monkeypatch.setenv('CUSTOMER_BULK_ENABLED', 'true')

    class CopyCursor:
        def __init__(self):
            self.executions = []
            self.copied = ''

        def execute(self, sql, params=None):
            self.executions.append((sql, params))

        def copy_expert(self, sql, stream, size):
            assert 'pg_temp.customer_bulk_stage' in sql and size == 65536
            self.copied = stream.read(size)

        def fetchone(self):
            return (1,)

    cursor = CopyCursor()
    ingestion._upsert_customers(cursor, [{'source_customer_id': 'shared'}], 'ingestion-1', 101)
    sql = [statement for statement, _ in cursor.executions]
    assert any('SELECT raw.apply_customer_forward_bulk(' in statement for statement in sql)
    assert not any('SELECT raw.apply_customer_bulk(' in statement for statement in sql)
    assert next(params for statement, params in cursor.executions
                if 'SELECT raw.apply_customer_forward_bulk(' in statement) == ('ingestion-1', 101)
    assert cursor.copied.startswith('ingestion-1\t101\tshared\t')


def test_forward_route_requires_durable_worker_commit_fence(monkeypatch):
    cursor = Cursor()
    with pytest.raises(RuntimeError, match='durable|worker|fence'):
        _run(monkeypatch, cursor, before_commit=None)
    assert not any('INSERT INTO control.ingestion_batch' in statement
                   for statement, _ in cursor.executions)


def test_forward_route_does_not_use_037_snapshot_or_validation_namespace(monkeypatch):
    cursor = Cursor()
    monkeypatch.setenv('CUSTOMER_FORWARD_SOURCE_NAMESPACE_ENABLED', 'true')
    monkeypatch.setenv('CUSTOMER_SOURCE_NAMESPACE_ENABLED', 'false')
    monkeypatch.setattr(ingestion, '_source_namespace_app',
                        lambda *args: pytest.fail('forward source path queried 037 namespace'))
    ingestion._create_existing_source_snapshot(
        cursor, landing_batch_id=LANDING_ID, source_system_id=101,
        ingestion_batch_id='ingestion-1',
    )
    ingestion._seed_validation_records(
        cursor, validation_run_id='validation-1', landing_batch_id=LANDING_ID,
        source_system_id=101, ingestion_batch_id='ingestion-1',
    )
    assert not any('customer_app_scope_status' in sql for sql, _ in cursor.executions)
    assert not any('customer_source_namespace_writer' in sql for sql, _ in cursor.executions)


def test_unpublished_landing_never_reaches_physical_source(monkeypatch):
    cursor = Cursor(landing_status='RECEIVING')
    with pytest.raises(ValueError, match='not published'):
        _run(monkeypatch, cursor)
    assert not any('INSERT INTO control.source_system' in sql or
                   'INSERT INTO control.ingestion_batch' in sql
                   for sql, _ in cursor.executions)


def test_forward_route_rolls_back_when_durable_fence_refuses_commit(monkeypatch):
    cursor = Cursor()
    with pytest.raises(RuntimeError, match='lease revoked'):
        _run(monkeypatch, cursor,
             before_commit=lambda *_: (_ for _ in ()).throw(RuntimeError('lease revoked')))
    assert any('INSERT INTO control.ingestion_batch' in sql for sql, _ in cursor.executions)
    assert not any('UPDATE control.ingestion_batch' in sql and "status = 'failed'" in sql
                   for sql, _ in cursor.executions)
