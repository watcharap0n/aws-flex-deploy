"""Local-only landing COPY/streaming regression and small wide benchmark.

Run: LANDING_TEST_DSN='host=127.0.0.1 port=55441 user=postgres dbname=cdp_landing_bulk_regression' pytest this_file
Never points at application or production databases.
"""
import csv
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import uuid

import psycopg2
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / '_lambda/landing_data'))
spec = importlib.util.spec_from_file_location('landing_bulk_test_lambda', ROOT / '_lambda/landing_data/lambda_function.py')
assert spec is not None and spec.loader is not None
landing = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = landing
spec.loader.exec_module(landing)


@pytest.fixture
def db():
    dsn = os.environ.get('LANDING_TEST_DSN')
    if not dsn:
        pytest.skip('Set LANDING_TEST_DSN to the disposable local regression database')
    options = psycopg2.extensions.parse_dsn(dsn)
    assert options.get('host') == '127.0.0.1'
    assert options.get('dbname') == 'cdp_landing_bulk_regression'
    connection = psycopg2.connect(dsn)
    with connection, connection.cursor() as q:
        q.execute('DROP SCHEMA IF EXISTS raw CASCADE; DROP SCHEMA IF EXISTS control CASCADE; CREATE SCHEMA raw; CREATE SCHEMA control;')
        q.execute('''
        CREATE TABLE control.source_landing_batch (
          batch_id uuid PRIMARY KEY DEFAULT gen_random_uuid(), source_code varchar(64) NOT NULL,
          batch_ref varchar(128) NOT NULL, status varchar(50) NOT NULL DEFAULT 'RECEIVING',
          last_error text, output_status varchar(20) NOT NULL DEFAULT 'PENDING',
          output_bucket text, output_key text, output_row_count bigint, output_sha256 char(64),
          output_etag text, output_error text, output_started_at timestamptz, output_published_at timestamptz,
          created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
          UNIQUE(source_code,batch_ref));
        CREATE TABLE control.source_landing_file (
          file_id uuid PRIMARY KEY DEFAULT gen_random_uuid(), batch_id uuid NOT NULL REFERENCES control.source_landing_batch,
          source_type text NOT NULL, s3_bucket text NOT NULL, s3_object_key text NOT NULL,
          s3_version_id text, s3_etag text, content_sha256 char(64), file_size_bytes bigint,
          sheet_name text, column_names jsonb, row_count bigint, blank_key_rows bigint, duplicate_key_rows bigint,
          status text NOT NULL, error_message text, created_at timestamptz NOT NULL DEFAULT now(),
          updated_at timestamptz NOT NULL DEFAULT now(), completed_at timestamptz,
          UNIQUE(batch_id,source_type), UNIQUE(batch_id,file_id));
        CREATE TABLE raw.customer_landing_source (
          source_record_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
          batch_id uuid NOT NULL REFERENCES control.source_landing_batch,
          file_id uuid NOT NULL, source_row_number integer NOT NULL, source_customer_id text,
          raw_payload jsonb NOT NULL, validation_errors jsonb, ingested_at timestamptz NOT NULL DEFAULT now(),
          FOREIGN KEY(batch_id,file_id) REFERENCES control.source_landing_file(batch_id,file_id),
          UNIQUE(file_id,source_row_number));
        CREATE INDEX ix_customer_landing_source_batch_customer ON raw.customer_landing_source(batch_id,source_customer_id);
        ''')
    yield connection
    connection.close()


def register(db, content=b'fixture', suffix='csv'):
    source = landing.SourceObject('fixture-bucket', f'data_sources/crm/test/customers/source.{suffix}', 'crm', 'test', 'customers', suffix, 'version-1')
    downloaded = landing.DownloadedObject(content, 'version-1', 'etag-1', len(content), hashlib.sha256(content).hexdigest())
    with db, db.cursor() as q:
        batch = landing._resolve_batch(q, source.source_code, source.batch_ref)
        file_id, action = landing._register_file(q, batch, source, downloaded)
    return source, downloaded, batch, file_id, action


def evidence(db):
    with db, db.cursor() as q:
        q.execute('SELECT source_row_number,source_customer_id,raw_payload,validation_errors FROM raw.customer_landing_source ORDER BY source_row_number')
        return q.fetchall()


def test_copy_accepts_one_shot_rows_and_same_file_replacement(db):
    _, _, batch, file_id, _ = register(db)
    rows = [
        landing.EvidenceRow(2, '0001', {'name': 'ไทย\t"line\n\\N', 'attributes': {'n': None}, 'id': '9007199254740993123'}, ['duplicate_key:source_customer_id']),
        landing.EvidenceRow(3, None, {'source_customer_id': None}, ['missing_or_blank_key:source_customer_id']),
        landing.EvidenceRow(4, '0001', {'name': 'last'}, ['duplicate_key:source_customer_id']),
        landing.EvidenceRow(5, '\\N', {'name': 'literal null marker'}, None),
    ]
    for _ in range(2):
        with db, db.cursor() as q:
            assert landing._replace_evidence_rows(q, 'customers', batch, file_id, iter(rows), 2) == 4
        assert evidence(db) == [(r.source_row_number, r.source_key, r.raw_payload, r.validation_errors) for r in rows]
    # COPY is the acceptance boundary: no SQL literal megastatement per page.
    assert 'copy_expert' in landing._replace_evidence_rows.__code__.co_names


def test_streamed_export_preserves_exact_bytes_last_row_winner(db):
    _, _, batch, file_id, _ = register(db)
    rows = [landing.EvidenceRow(2, '0001', {'first_name': 'old'}, None),
            landing.EvidenceRow(3, None, {'first_name': 'ignored'}, ['missing_or_blank_key:source_customer_id']),
            landing.EvidenceRow(4, '0001', {'first_name': 'ไทย', 'date_of_birth': '2020-01-02', 'source_created_at': '2026-09-18T10:00:00Z', 'attributes': {'n': None}, 'phone': '0012345678'}, ['duplicate_key:source_customer_id'])]
    with db, db.cursor() as q:
        landing._replace_evidence_rows(q, 'customers', batch, file_id, rows, 2)
    expected, count = landing._build_customer_csv(db, batch, 'crm')
    with landing._build_customer_csv_spooled(db, batch, 'crm', spool_limit=1) as exported:
        assert exported.row_count == count == 1
        assert exported.body.read() == expected
        assert exported.sha256 == hashlib.sha256(expected).hexdigest()
        assert exported.size == len(expected)
        assert exported.body._rolled
    record = next(csv.DictReader(io.StringIO(expected.decode('utf-8-sig'))))
    assert record['first_name'] == 'ไทย'
    assert record['phone'] == '0012345678'
    assert json.loads(record['attributes']) == {'n': None}
    assert record['date_of_birth'] == '2020-01-02'


def test_copy_failure_restores_previous_evidence(db):
    _, _, batch, file_id, _ = register(db)
    before = [landing.EvidenceRow(2, 'original', {'n': None}, None)]
    with db, db.cursor() as q:
        landing._replace_evidence_rows(q, 'customers', batch, file_id, before, 100)
    with pytest.raises(psycopg2.Error):
        with db, db.cursor() as q:
            landing._replace_evidence_rows(q, 'customers', batch, file_id, [before[0], before[0]], 100)
    assert evidence(db) == [(2, 'original', {'n': None}, None)]


def test_json_nullable_large_integer_key_never_passes_through_float():
    parsed = landing.parse_json_evidence(b'[{"source_customer_id":9007199254740993123},{"source_customer_id":null}]', 'source_customer_id')
    assert parsed.rows[0].source_key == '9007199254740993123'
    assert parsed.rows[0].raw_payload['source_customer_id'] == 9007199254740993123


def test_csv_phone_alias_is_an_exact_identifier():
    parsed = landing.parse_csv_evidence(b'source_customer_id,phone_number\n0001,0012345678\n', 'source_customer_id')
    row = parsed.rows[0]
    assert landing._canonical_customer_row(row.source_key, row.raw_payload)['phone'] == '0012345678'


def test_disk_backed_evidence_preserves_global_duplicates_and_invalid_keys(tmp_path):
    content = ('StoreCode,FirstName,attributes\n0001,ไทย,""\n,blank,""\n0001,last,""\n0002,unique,""\n').encode()
    expected = landing.parse_csv_evidence(content, 'source_customer_id')
    with landing.parse_source_evidence_spooled(content, 'csv', 'source_customer_id') as parsed:
        assert not isinstance(parsed.rows, list)
        assert [row.raw_payload for row in parsed.rows] == [row.raw_payload for row in expected.rows]
        assert [row.source_key for row in parsed.rows] == [row.source_key for row in expected.rows]
        assert [row.validation_errors for row in parsed.rows] == [row.validation_errors for row in expected.rows]
        assert list(parsed.rows) == list(parsed.rows)  # reiterable; no consumed generator
        assert parsed.duplicate_key_rows == 2
        assert parsed.blank_key_rows == 1
        assert parsed.file_status == 'INGESTED_WITH_WARNINGS'
        assert len(parsed.rows) == 4


def test_bounded_csv_text_contract_preserves_lexemes_across_late_mixed_rows(monkeypatch):
    # Whole-file pandas inference changes the meaning of early rows when a
    # later chunk contains text; bounded CSV instead keeps every field lexical.
    content = (b'source_customer_id,amount,comment\n'
               b'0001,1,"line, one"\n'
               b'0002,2.5,"line\ntwo"\n'
               b'0001,not-numeric,""\n'
               b',,NA\n')
    monkeypatch.setenv('LANDING_MAX_PARSE_BYTES', '8')
    monkeypatch.setenv('LANDING_MAX_CSV_BYTES', '1024')
    with landing.parse_source_evidence_spooled(io.BytesIO(content), 'csv', 'source_customer_id') as parsed:
        rows = list(parsed.rows)
        assert [row.source_key for row in rows] == ['0001', '0002', '0001', None]
        assert [row.raw_payload['amount'] for row in rows] == ['1', '2.5', 'not-numeric', '']
        assert [row.raw_payload['comment'] for row in rows] == ['line, one', 'line\ntwo', '', 'NA']
        assert [row.source_row_number for row in rows] == [2, 3, 4, 5]
        assert [row.validation_errors for row in rows] == [
            ['duplicate_key:source_customer_id'], None,
            ['duplicate_key:source_customer_id'], ['missing_or_blank_key:source_customer_id']]
        assert parsed.duplicate_key_rows == 2
        assert parsed.blank_key_rows == 1


def test_bounded_csv_late_text_over_chunk_boundary_keeps_early_lexemes(monkeypatch):
    monkeypatch.setenv('LANDING_MAX_PARSE_BYTES', '8')
    monkeypatch.setenv('LANDING_MAX_CSV_BYTES', str(1024 * 1024))
    content = (b'StoreCode,amount,amount\n' +
               b'0001,001,1\n' * 1024 + b'0002,2.5,2\n' +
               b'0003,not-numeric,3\n')
    with landing.parse_source_evidence_spooled(io.BytesIO(content), 'csv', 'source_customer_id') as parsed:
        rows = list(parsed.rows)
        assert parsed.column_names == ['StoreCode', 'amount', 'amount.1']
        assert rows[0].raw_payload == {'StoreCode': '0001', 'amount': '001', 'amount.1': '1'}
        assert rows[1024].raw_payload['amount'] == '2.5'
        assert rows[1025].raw_payload['amount'] == 'not-numeric'
        assert rows[0].validation_errors == ['duplicate_key:source_customer_id']
        assert parsed.duplicate_key_rows == 1024


def test_csv_download_default_accepts_large_declared_size_but_enforces_csv_limit(monkeypatch):
    class Body:
        def __init__(self):
            self.closed = False
        def read(self, size):
            assert size == 1024 * 1024
            raise RuntimeError('read reached')
        def close(self):
            self.closed = True
    bodies = []
    class S3:
        def get_object(self, **request):
            body = Body()
            bodies.append(body)
            return {'Body': body, 'ContentLength': 268435457}
    source = landing.SourceObject('bucket', 'key', 'crm', 'batch', 'customers', 'csv', None)
    monkeypatch.setenv('LANDING_MAX_PARSE_BYTES', '268435457')
    monkeypatch.delenv('LANDING_MAX_CSV_BYTES', raising=False)
    with pytest.raises(RuntimeError, match='read reached'):
        landing._download_object(S3(), source)
    monkeypatch.setenv('LANDING_MAX_CSV_BYTES', '268435456')
    with pytest.raises(landing.SourceCapacityError):
        landing._download_object(S3(), source)
    assert all(body.closed for body in bodies)


def test_default_csv_cap_admits_562mib_without_lifting_whole_frame_cap(monkeypatch):
    class ReadReached(RuntimeError):
        pass
    class Body:
        closed = False
        def read(self, size):
            assert 0 < size <= 1024 * 1024
            raise ReadReached('bounded read reached')
        def close(self):
            self.closed = True
    class S3:
        def get_object(self, **kwargs):
            return {'Body': body, 'ContentLength': 562 * 1024 ** 2}
    monkeypatch.delenv('LANDING_MAX_CSV_BYTES', raising=False)
    monkeypatch.setenv('LANDING_MAX_PARSE_BYTES', str(256 * 1024 ** 2))
    body = Body()
    source = landing.SourceObject('bucket', 'key', 'crm', 'batch', 'customers', 'csv', None)
    with pytest.raises(ReadReached):
        landing._download_object(S3(), source)
    assert body.closed
    body = Body()
    non_csv = landing.SourceObject('bucket', 'key', 'crm', 'batch', 'customers', 'json', None)
    with pytest.raises(landing.SourceCapacityError):
        landing._download_object(S3(), non_csv)
    assert body.closed


def test_default_csv_cap_routes_above_budget_before_read(monkeypatch):
    class Body:
        closed = False
        def read(self, size):
            pytest.fail('oversized source must not be downloaded')
        def close(self):
            self.closed = True
    body = Body()
    monkeypatch.delenv('LANDING_MAX_CSV_BYTES', raising=False)
    source = landing.SourceObject('bucket', 'key', 'crm', 'batch', 'customers', 'csv', None)
    s3 = type('S3', (), {'get_object': lambda self, **kwargs: {'Body': body, 'ContentLength': 2 * 1024 ** 3}})()
    with pytest.raises(landing.SourceCapacityError):
        landing._download_object(s3, source)
    assert body.closed


def test_spooled_non_csv_parser_rejects_oversized_input_before_materializing(monkeypatch):
    monkeypatch.setenv('LANDING_MAX_PARSE_BYTES', '8')
    with pytest.raises(landing.SourceCapacityError):
        landing.parse_source_evidence_spooled(b'{"source_customer_id":"a"}', 'json', 'source_customer_id')


class MemoryS3:
    def __init__(self):
        self.objects = {}
        self.puts = 0

    def put_object(self, **request):
        assert request['IfNoneMatch'] == '*'
        key = (request['Bucket'], request['Key'])
        if key in self.objects:
            raise landing.ClientError({'Error': {'Code': 'PreconditionFailed'}, 'ResponseMetadata': {'HTTPStatusCode': 412}}, 'PutObject')
        body = request['Body']
        assert hasattr(body, 'seek')  # production path must not reconstruct bytes
        self.objects[key] = (body.read(), request['Metadata'])
        self.puts += 1
        return {'ETag': '"fixture-etag"'}

    def head_object(self, **request):
        _, metadata = self.objects[(request['Bucket'], request['Key'])]
        return {'ETag': '"fixture-etag"', 'Metadata': metadata}


def test_process_recovers_registered_hard_kill_and_replay(db):
    content = b'source_customer_id,first_name\n001,old\n001,last\n,blank\n'
    source, downloaded, batch, file_id, _ = register(db, content)
    s3 = MemoryS3()
    config = landing.Config('unused', 'data_sources/', 'raw/', None, 1000)
    # Registration committed but the process died; no 15-minute delay/no skip.
    first = landing._process_source(source, downloaded, db, s3, config)
    assert first['status'] == 'ingested'
    assert first['database_rows'] == 3
    assert first['output']['output_rows'] == 1
    second = landing._process_source(source, downloaded, db, s3, config)
    assert second['status'] == 'already_processed'
    assert s3.puts == 1
    assert len(evidence(db)) == 3
    with db, db.cursor() as q:
        q.execute('SELECT status,output_status,output_sha256 FROM control.source_landing_batch WHERE batch_id=%s', (batch,))
        status, output, digest = q.fetchone()
    body, metadata = next(iter(s3.objects.values()))
    assert status == 'SOURCES_INGESTED' and output == 'PUBLISHED'
    assert digest == metadata['content-sha256'] == hashlib.sha256(body).hexdigest()


def test_worker_fence_rejects_stale_payload_transaction(db):
    source, downloaded, _, _, _ = register(db, b'source_customer_id\n001\n')
    config = landing.Config('unused', 'data_sources/', 'raw/', None, 1000)
    def fence(cursor):
        raise RuntimeError('lease revoked')
    with pytest.raises(RuntimeError, match='lease revoked'):
        landing._process_source(source, downloaded, db, MemoryS3(), config, before_commit=fence)
    assert evidence(db) == []


def test_spooled_download_is_chunked_checksum_bound_and_version_pinned():
    content = 'source_customer_id,first_name\n0001,ไทย\n'.encode()
    class Body(io.BytesIO):
        def read(self, size=-1):
            assert size > 0, 'download must never read an unbounded source'
            return super().read(size)
    class S3:
        def get_object(self, **request):
            assert request == {'Bucket': 'fixture-bucket', 'Key': 'data_sources/crm/test/customers/source.csv', 'VersionId': 'v1', 'IfMatch': 'event-etag'}
            return {'Body': Body(content), 'ContentLength': len(content), 'VersionId': 'v1', 'ETag': '"event-etag"'}
    source = landing.SourceObject('fixture-bucket', 'data_sources/crm/test/customers/source.csv', 'crm', 'test', 'customers', 'csv', 'v1', event_etag='event-etag')
    with landing._download_object(S3(), source) as downloaded:
        assert downloaded.content.read() == content
        assert downloaded.content_sha256 == hashlib.sha256(content).hexdigest()
        assert downloaded.content_length == len(content)
        assert downloaded.version_id == 'v1'


def test_publish_replay_conflict_preserves_checksum_and_etag():
    s3 = MemoryS3()
    body = io.BytesIO(b'\xef\xbb\xbfx\n')
    args = (s3, 'bucket', 'key', body, 'batch', 'crm', hashlib.sha256(body.getvalue()).hexdigest(), 1)
    assert landing._publish_conditionally(*args) == ('fixture-etag', True)
    body.seek(0)
    assert landing._publish_conditionally(*args) == ('fixture-etag', False)
    with pytest.raises(landing.OutputConflictError):
        landing._publish_conditionally(s3, 'bucket', 'key', io.BytesIO(b'changed'), 'batch', 'crm', 'wrong', 1)
    assert s3.puts == 1

@pytest.mark.parametrize('fmt,content', [
    ('csv', b'other,name\n1,a\n2,b\n'),
    ('csv', b'source_customer_id,customer_id\n001,002\n'),
    ('csv', b'source_customer_id,name\n'),
    ('json', b'{"records":[{"source_customer_id":"001","attributes":{"x":null}},{"source_customer_id":"001","later":1},{"source_customer_id":null}]}'),
    ('json', b'{"data":[{"StoreCode":"001","phone":"00123"}]}'),
    ('json', b'{"source_customer_id":"001","first_name":"one"}'),
])
def test_all_parser_status_and_evidence_contracts(fmt, content):
    expected = landing.parse_source_evidence(content, fmt, 'source_customer_id')
    with landing.parse_source_evidence_spooled(content, fmt, 'source_customer_id') as actual:
        for field in ('column_names', 'file_status', 'file_error', 'blank_key_rows', 'duplicate_key_rows', 'sheet_name'):
            assert getattr(actual, field) == getattr(expected, field)
        if fmt == 'csv' and content.startswith(b'other,name'):
            assert [row.raw_payload for row in actual.rows] == [row.raw_payload for row in expected.rows]
        else:
            assert list(actual.rows) == expected.rows


def test_xlsx_selects_first_matching_sheet_and_preserves_dates():
    from datetime import datetime
    import openpyxl
    workbook = openpyxl.Workbook()
    workbook.active.append(['unrelated'])
    sheet = workbook.create_sheet('Customers')
    sheet.append(['StoreCode', 'FirstName', 'date_of_birth', 'ContactNumber'])
    sheet.append(['0001', 'ไทย', datetime(2000, 1, 2), '001234'])
    sheet.append(['0001', 'last', datetime(2001, 1, 2), '001235'])
    content = io.BytesIO()
    workbook.save(content)
    expected = landing.parse_excel_evidence(content.getvalue(), 'source_customer_id')
    with landing.parse_source_evidence_spooled(content.getvalue(), 'xlsx', 'source_customer_id') as actual:
        assert actual.sheet_name == 'Customers'
        assert list(actual.rows) == expected.rows
        row = list(actual.rows)[0]
        canonical = landing._canonical_customer_row(row.source_key, row.raw_payload)
        assert canonical['phone'] == '001234'
        assert canonical['date_of_birth'] == '2000-01-02'


def test_canonical_wide_payload_scanned_once_preserving_alias_precedence():
    class CountingPayload(dict):
        calls = 0
        def items(self):
            self.calls += 1
            return super().items()
    payload = CountingPayload({'FirstName': 'first', 'first_name': 'second', 'phone_number': '00123', 'attributes': {'n': None}, **{f'extra{i}': i for i in range(50)}})
    actual = landing._canonical_customer_row('001', payload)
    assert actual['first_name'] == 'first'
    assert actual['phone'] == '00123'
    assert payload.calls <= 2, 'canonical mapping must not rescan a wide payload per field'


def test_another_active_session_is_retryable_not_acknowledged(db):
    source, downloaded, _, _, _ = register(db, b'source_customer_id\n001\n')
    owner = psycopg2.connect(os.environ['LANDING_TEST_DSN'])
    try:
        with owner, owner.cursor() as q:
            q.execute('SELECT pg_advisory_lock(hashtextextended(%s,0))', ('landing-worker:crm:test',))
        config = landing.Config('unused', 'data_sources/', 'raw/', None, 1000)
        with pytest.raises(RuntimeError, match='landing_source_busy'):
            landing._process_source(source, downloaded, db, MemoryS3(), config)
        assert evidence(db) == []
    finally:
        owner.close()


def test_different_content_cannot_replace_registered_provenance(db):
    source, downloaded, batch, file_id, _ = register(db)
    changed = landing.DownloadedObject(b'changed', 'version-2', 'etag-2', 7, hashlib.sha256(b'changed').hexdigest())
    with pytest.raises(landing.SourceConflictError):
        with db, db.cursor() as q:
            landing._register_file(q, batch, source, changed, resume_processing=True)
    with db, db.cursor() as q:
        q.execute('SELECT content_sha256,s3_version_id,s3_etag FROM control.source_landing_file WHERE file_id=%s', (file_id,))
        assert q.fetchone() == (downloaded.content_sha256, 'version-1', 'etag-1')


def test_worker_fence_also_protects_invalid_parse_completion(db):
    source, downloaded, batch, _, _ = register(db, b'not json', 'json')
    def revoked(cursor):
        raise RuntimeError('lease revoked')
    config = landing.Config('unused', 'data_sources/', 'raw/', None, 1000)
    with pytest.raises(RuntimeError, match='lease revoked'):
        landing._process_source(source, downloaded, db, MemoryS3(), config, before_commit=revoked)
    with db, db.cursor() as q:
        q.execute('SELECT status FROM control.source_landing_file WHERE batch_id=%s', (batch,))
        assert q.fetchone() == ('PROCESSING',)


def test_retry_after_put_before_output_commit_reconciles_same_object(db):
    source, downloaded, _, _, _ = register(db, b'source_customer_id,first_name\n001,one\n')
    class HardKill(BaseException):
        pass
    class InterruptedS3(MemoryS3):
        interrupted = False
        def put_object(self, **request):
            response = super().put_object(**request)
            if not self.interrupted:
                self.interrupted = True
                raise HardKill()
            return response
    s3 = InterruptedS3()
    config = landing.Config('unused', 'data_sources/', 'raw/', None, 1000)
    with pytest.raises(HardKill):
        landing._process_source(source, downloaded, db, s3, config)
    assert len(evidence(db)) == 1
    result = landing._process_source(source, downloaded, db, s3, config)
    assert result['status'] == 'already_processed'
    assert result['output']['status'] == 'published'
    assert result['output']['created'] is False
    assert s3.puts == 1


def test_spooled_export_is_identical_to_legacy_csv_not_just_new_wrapper(db):
    import subprocess
    import types
    original = subprocess.check_output(['git', '-C', str(ROOT), 'show', 'HEAD:_lambda/landing_data/lambda_function.py'])
    module = types.ModuleType('landing_original_byte_contract')
    sys.modules[module.__name__] = module
    exec(compile(original, 'landing_original_byte_contract.py', 'exec'), module.__dict__)
    _, _, batch, file_id, _ = register(db)
    rows = [landing.EvidenceRow(i + 2, str(i), {'FirstName': 'ไทย', 'first_name': None,
            'birthday': '43832', 'source_created_at': '2026-09-18T10:00:00Z',
            'attributes': {'n': None, 'text': 'comma,quote"newline\n'},
            'device_id(s)': 'a;b;a', 'StorePostCode': '00102'}, None) for i in range(5)]
    with db, db.cursor() as q:
        landing._replace_evidence_rows(q, 'customers', batch, file_id, rows, 2)
    expected, count = module._build_customer_csv(db, batch, 'crm')
    with landing._build_customer_csv_spooled(db, batch, 'crm') as output:
        assert output.body.read() == expected
        assert output.row_count == count


def test_revoked_output_completion_cannot_write_unfenced_failure(db):
    source, downloaded, batch, _, _ = register(db, b'source_customer_id\n001\n')
    calls=[]
    def fence(q):
        q.execute('SELECT output_status FROM control.source_landing_batch WHERE batch_id=%s',(batch,))
        state=q.fetchone()[0]
        calls.append(state)
        if state in {'PUBLISHED','FAILED'}:
            raise RuntimeError('lease revoked')
    with pytest.raises(RuntimeError,match='lease revoked'):
        landing._process_source(source,downloaded,db,MemoryS3(),
            landing.Config('unused','data_sources/','raw/',None,1000),before_commit=fence)
    with db,db.cursor() as q:
        q.execute('SELECT output_status FROM control.source_landing_batch WHERE batch_id=%s',(batch,))
        assert q.fetchone()==('BUILDING',), 'stale worker must not commit a failure marker'
    assert 'FAILED' in calls


def test_every_alias_matches_legacy_canonical_selection():
    import subprocess
    import types
    original=subprocess.check_output(['git','-C',str(ROOT),'show','HEAD:_lambda/landing_data/lambda_function.py'])
    module=types.ModuleType('landing_alias_legacy')
    sys.modules[module.__name__]=module
    exec(compile(original,'landing_alias_legacy.py','exec'),module.__dict__)
    checked=0
    for field,aliases in landing.COLUMN_ALIASES.items():
        for alias in aliases:
            for spelling in (alias,' '+alias.upper()+' '):
                payload={spelling:{'value':'00123'} if field in {'external_ids','attributes'} else '00123'}
                assert landing._canonical_customer_row('001',payload)==module._canonical_customer_row('001',payload)
                checked+=1
    assert checked>=100


def test_output_fence_is_immediately_before_building_commit(db):
    source,downloaded,batch,_,_=register(db,b'source_customer_id\n001\n')
    states=[]
    def fence(q):
        q.execute('SELECT output_status FROM control.source_landing_batch WHERE batch_id=%s',(batch,))
        states.append(q.fetchone()[0])
    landing._process_source(source,downloaded,db,MemoryS3(),
        landing.Config('unused','data_sources/','raw/',None,1000),before_commit=fence)
    assert 'BUILDING' in states, 'readiness fence must follow BUILDING update'



def test_new_registration_rolls_back_when_worker_is_revoked(db):
    source=landing.SourceObject('bucket','data_sources/crm/new/customers/source.csv','crm','new','customers','csv','v1','etag')
    content=b'source_customer_id\n001\n'
    downloaded=landing.DownloadedObject(content,'v1','etag',len(content),hashlib.sha256(content).hexdigest())
    def revoked(q):
        raise RuntimeError('lease revoked')
    with pytest.raises(RuntimeError,match='lease revoked'):
        landing._process_source(source,downloaded,db,MemoryS3(),
            landing.Config('unused','data_sources/','raw/',None,1000),before_commit=revoked)
    with db,db.cursor() as q:
        q.execute('SELECT count(*) FROM control.source_landing_file')
        assert q.fetchone()==(0,)


