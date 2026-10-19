"""Real ClickHouse tests: explicitly owned, disposable database only."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import os
from pathlib import Path
import threading
import urllib.error
import urllib.request
from uuid import uuid4

import pytest

ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / 'scripts/cutover_customer_identity_profile.py'
DDL = ROOT.parent / 'identity-resolution/design/standardized-clickhouse-transfer/006_customer_profile_final.sql'


def load_module():
    assert MODULE.exists(), 'publication-safe compatibility cutover is not implemented'
    spec = importlib.util.spec_from_file_location('publication_cutover', MODULE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@contextmanager
def local_server(handler):
    server = ThreadingHTTPServer(('127.0.0.1', 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}'
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize('status', [301, 302, 303, 307, 308])
def test_client_rejects_redirect_without_forwarding_authorization(status):
    module = load_module()
    forwarded = []
    initial = []

    class Destination(BaseHTTPRequestHandler):
        def do_GET(self):
            forwarded.append(self.headers.get('Authorization'))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'OK')

        do_POST = do_GET

        def log_message(self, format, *args):
            pass

    with local_server(Destination) as destination:
        class Redirect(BaseHTTPRequestHandler):
            def do_POST(self):
                initial.append(self.headers.get('Authorization'))
                self.send_response(status)
                self.send_header('Location', destination + '/must-not-receive-auth')
                self.end_headers()

            def log_message(self, format, *args):
                pass

        with local_server(Redirect) as source:
            error = None
            try:
                module.Client(source, 'fixture-user', 'fixture-password').query('SELECT 1')
            except module.CutoverError as exc:
                error = str(exc)
    assert initial and initial[0].startswith('Basic ')
    assert forwarded == [], 'redirect sent a request with credentials to another origin'
    assert error == 'CLICKHOUSE_REQUEST_FAILED_RECONCILE_BEFORE_RETRY'


@pytest.fixture
def fixture():
    url = os.environ.get('POSTSTITCH_TEST_CH_URL')
    if not url:
        pytest.skip('POSTSTITCH_TEST_CH_URL must name the owned local fixture')
    assert url == 'http://127.0.0.1:61584', 'refuse non-fixture ClickHouse'
    db = 'poststitch_cutover_' + uuid4().hex
    def query(sql):
        req = urllib.request.Request(url, data=sql.encode(), method='POST')
        return urllib.request.urlopen(req, timeout=20).read().decode()
    query(f'CREATE DATABASE {db} ENGINE=Atomic')
    query('CREATE USER IF NOT EXISTS identity_publication_definer IDENTIFIED WITH no_password HOST NONE')
    query('ALTER USER identity_publication_definer HOST NONE')
    for name in ('customer_profile_resolved', 'customer_profile_legacy_snapshot', 'saved_resolved'):
        query(f'GRANT SELECT ON {db}.{name} TO identity_publication_definer')
    start = DDL.read_text().index('CREATE TABLE IF NOT EXISTS cdp_customer.customer_profile')
    ddl = DDL.read_text()[start:].split(';')[0].replace('cdp_customer.', db + '.')
    query(ddl)
    query(f'INSERT INTO {db}.customer_profile (app_id,customer_id,customer_key_id,profile_source_record_id,first_name) VALUES (\'app1\',\'old\',41,42,\'legacy\'),(\'app1\',\'updated\',43,44,\'old name\')')
    query(f'CREATE TABLE {db}.customer_profile_resolved AS {db}.customer_profile')
    query(f'ALTER TABLE {db}.customer_profile_resolved MODIFY COLUMN customer_key_id Nullable(Int64), MODIFY COLUMN profile_source_record_id Nullable(Int64), ADD COLUMN resolution_run_id UUID, ADD COLUMN customer_entity_id UUID, ADD COLUMN workspace_id UUID')
    query(f"INSERT INTO {db}.customer_profile_resolved (app_id,customer_id,customer_key_id,profile_source_record_id,first_name,resolution_run_id) VALUES ('app1','updated',NULL,NULL,'cleaned','00000000-0000-0000-0000-000000000001'),('app1','new',NULL,NULL,'new name','00000000-0000-0000-0000-000000000001')")
    try:
        yield url, db, query
    finally:
        query(f'DROP DATABASE {db} SYNC')
        # CH25.8 EXCHANGE may retain stale definer dependency names. Keep the
        # shared HOST NONE principal, revoke only this UUID database's grants.
        query(f'REVOKE SELECT ON {db}.* FROM identity_publication_definer')


def test_consumer_is_fixed_definer_before_exchange_and_reader_needs_no_sources(fixture):
    module = load_module()
    url, db, query = fixture
    reader = 'cutover_reader_' + uuid4().hex
    query(f'CREATE USER {reader} IDENTIFIED WITH no_password')
    query(f'GRANT SELECT ON {db}.customer_profile TO {reader}')
    client = module.Client(url, reader)
    checked = []

    class VerifyBeforeExchange(module.Client):
        def query(self, sql):
            if sql.startswith('EXCHANGE TABLES'):
                ddl = super().rows(f"SELECT create_table_query FROM system.tables WHERE database='{db}' AND name='customer_profile_identity_candidate'")[0]['create_table_query']
                assert 'DEFINER = identity_publication_definer SQL SECURITY DEFINER' in ddl
                checked.append(True)
            return super().query(sql)

    try:
        assert module.cutover(VerifyBeforeExchange(url), db, writers_stopped=True)['status'] == 'ACTIVE'
        assert checked == [True]
        assert client.query(f'SELECT * FROM {db}.customer_profile FORMAT Null') == ''
        assert client.query(f'SELECT count() FROM {db}.customer_profile') == '3\n'
        for name in ('customer_profile_resolved', 'customer_profile_legacy_snapshot', 'customer_profile_pre_identity'):
            request = urllib.request.Request(url, data=f'SELECT * FROM {db}.{name} LIMIT 1 FORMAT Null'.encode(),
                                             headers={'X-ClickHouse-User': reader})
            with pytest.raises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(request, timeout=20)
            assert error.value.read().startswith(b'Code: 497.')
    finally:
        query(f'DROP USER {reader}')


@pytest.mark.parametrize('resolved_engine', ['MergeTree', 'View'])
def test_atomic_cutover_preserves_legacy_overlays_source_id_and_retries(fixture, resolved_engine):
    module = load_module()
    url, db, query = fixture
    if resolved_engine == 'View':
        query(f'RENAME TABLE {db}.customer_profile_resolved TO {db}.saved_resolved')
        query(f'CREATE VIEW {db}.customer_profile_resolved AS SELECT * FROM {db}.saved_resolved')
    client = module.Client(url)
    before = client.fingerprint(f'{db}.customer_profile')
    original_columns = client.rows(f"SELECT name,type FROM system.columns WHERE database='{db}' AND table='customer_profile' ORDER BY position")
    original_uuid = query(f"SELECT uuid FROM system.tables WHERE database='{db}' AND name='customer_profile'")
    assert len(original_columns) == 38
    report = module.cutover(client, db, writers_stopped=True)
    assert report['status'] == 'ACTIVE'
    rows = [json.loads(line) for line in query(f'SELECT app_id,customer_id,customer_key_id,first_name,resolution_run_id FROM {db}.customer_profile ORDER BY customer_id FORMAT JSONEachRow').splitlines()]
    assert [r['customer_id'] for r in rows] == ['new', 'old', 'updated']
    assert rows[1]['customer_key_id'] == 41 and rows[1]['resolution_run_id'] is None
    assert rows[2]['first_name'] == 'cleaned' and rows[2]['customer_key_id'] is None
    assert query(f'SELECT count() FROM {db}.customer_profile_pre_identity') == '2\n'
    assert client.fingerprint(f'{db}.customer_profile_pre_identity') == before == report['legacy_sha256']
    assert query(f"SELECT uuid FROM system.tables WHERE database='{db}' AND name='customer_profile_pre_identity'") == original_uuid
    rollback_columns = client.rows(f"SELECT name,type FROM system.columns WHERE database='{db}' AND table='customer_profile_pre_identity' ORDER BY position")
    assert rollback_columns == original_columns
    live_columns = client.rows(f"SELECT name,type FROM system.columns WHERE database='{db}' AND table='customer_profile' ORDER BY position")
    expected_columns = [{**r, 'type': 'Nullable(Int64)' if r['name'] in {'customer_key_id', 'profile_source_record_id'} else r['type']} for r in original_columns]
    assert live_columns[:38] == expected_columns
    select = client.rows(f"SELECT as_select FROM system.tables WHERE database='{db}' AND name='customer_profile'")[0]['as_select']
    assert select.count(f'{db}.customer_profile_resolved') == 1
    assert module.cutover(client, db, writers_stopped=True)['status'] == 'ACTIVE'
    assert query(f'SELECT count() FROM {db}.customer_profile') == '3\n'


@pytest.mark.parametrize('kind', ['Nullable(UUID)', 'Nullable(String)', 'Nullable(UInt64)', 'Nullable(Int64)', 'UInt64', 'Int64'])
def test_cutover_accepts_nullable_lineage_without_inventing_legacy_values(fixture, kind):
    module = load_module()
    url, db, query = fixture
    query(f'ALTER TABLE {db}.customer_profile_resolved ADD COLUMN lineage_extra {kind}')
    client = module.Client(url)
    assert module.cutover(client, db, writers_stopped=True)['status'] == 'ACTIVE'
    assert module.cutover(client, db, writers_stopped=True)['status'] == 'ACTIVE'
    rows = client.rows(f"SELECT lineage_extra FROM {db}.customer_profile WHERE customer_id='old'")
    assert rows == [{'lineage_extra': None}]


@pytest.mark.parametrize('stage', ['active-retry', 'exchange-ack-recovery', 'post-exchange'])
@pytest.mark.parametrize('app_id,customer_id', [('app1', 'updated'), ('', 'blank-app'), ('  ', 'space-app'), ('app1', ''), ('app1', '  ')])
def test_every_active_path_rejects_invalid_resolved_business_grain(fixture, stage, app_id, customer_id):
    module = load_module()
    url, db, query = fixture
    client = module.Client(url)
    before = client.fingerprint(f'{db}.customer_profile')
    original_uuid = query(f"SELECT uuid FROM system.tables WHERE database='{db}' AND name='customer_profile'")

    def damage_resolved():
        query(f"INSERT INTO {db}.customer_profile_resolved (app_id,customer_id,first_name) VALUES ('{app_id}','{customer_id}','conflicting')")

    class DamageAfterExchange(module.Client):
        def query(self, sql):
            result = super().query(sql)
            if sql.startswith('EXCHANGE TABLES'):
                damage_resolved()
                if stage == 'exchange-ack-recovery':
                    raise module.CutoverError('SYNTHETIC_ACK_LOSS')
            return result

    if stage == 'active-retry':
        assert module.cutover(client, db, writers_stopped=True)['status'] == 'ACTIVE'
        damage_resolved()
    elif stage == 'exchange-ack-recovery':
        with pytest.raises(module.CutoverError, match='SYNTHETIC_ACK_LOSS'):
            module.cutover(DamageAfterExchange(url), db, writers_stopped=True)
    else:
        client = DamageAfterExchange(url)

    with pytest.raises(module.CutoverError, match='RESOLVED_GRAIN_INVALID'):
        module.cutover(client, db, writers_stopped=True)
    source = 'customer_profile_pre_identity' if stage == 'active-retry' else 'customer_profile_identity_candidate'
    assert query(f"SELECT uuid FROM system.tables WHERE database='{db}' AND name='{source}'") == original_uuid
    assert client.fingerprint(f'{db}.{source}') == before
    if stage != 'active-retry':
        assert query(f"SELECT count() FROM system.tables WHERE database='{db}' AND name='customer_profile_pre_identity'") == '0\n'


def test_active_retry_rejects_missing_snapshot(fixture):
    module = load_module()
    url, db, query = fixture
    client = module.Client(url)
    module.cutover(client, db, writers_stopped=True)
    query(f'DROP TABLE {db}.customer_profile_legacy_snapshot SYNC')
    with pytest.raises(module.CutoverError, match='MISSING_LEGACY_SNAPSHOT'):
        module.cutover(client, db, writers_stopped=True)


@pytest.mark.parametrize('damage', ['view', 'empty-copy', 'identical-copy', 'original-rows-changed'])
def test_active_retry_rejects_replaced_rollback(fixture, damage):
    module = load_module()
    url, db, query = fixture
    client = module.Client(url)
    module.cutover(client, db, writers_stopped=True)
    rollback = f'{db}.customer_profile_pre_identity'
    if damage == 'original-rows-changed':
        query(f"INSERT INTO {rollback} (app_id,customer_id) VALUES ('app1','unexpected')")
    else:
        query(f'RENAME TABLE {rollback} TO {db}.original_rollback')
        if damage == 'view':
            query(f'CREATE VIEW {rollback} AS SELECT * FROM {db}.original_rollback')
        else:
            query(f'CREATE TABLE {rollback} AS {db}.original_rollback')
            if damage == 'identical-copy':
                query(f'INSERT INTO {rollback} SELECT * FROM {db}.original_rollback')
    with pytest.raises(module.CutoverError, match='INVALID_ROLLBACK_IDENTITY'):
        module.cutover(client, db, writers_stopped=True)
    assert query(f'SELECT count() FROM {db}.customer_profile_legacy_snapshot') == '2\n'


@pytest.mark.parametrize('stage', ['active-retry', 'exchange-ack-recovery', 'post-exchange'])
@pytest.mark.parametrize('damage', ['invoker', 'wrong-definer', 'omitted', 'column-default'])
def test_every_active_path_requires_exact_fixed_definer_ddl(fixture, stage, damage):
    module = load_module()
    url, db, query = fixture
    client = module.Client(url)

    def damage_view():
        if damage in ('invoker', 'wrong-definer'):
            security = 'INVOKER' if damage == 'invoker' else 'DEFINER DEFINER = default'
            query(f'ALTER TABLE {db}.customer_profile MODIFY SQL SECURITY {security}')
        else:
            row = client.rows(f"SELECT as_select,comment FROM system.tables WHERE database='{db}' AND name='customer_profile'")[0]
            columns = client.rows(f"SELECT name,type FROM system.columns WHERE database='{db}' AND table='customer_profile' ORDER BY position")
            defs = ','.join(f'`{c["name"]}` {c["type"]}' + (" DEFAULT 'drift'" if c['name'] == 'app_id' else '') for c in columns)
            contract = '' if damage == 'omitted' else f'({defs}) DEFINER = identity_publication_definer SQL SECURITY DEFINER'
            query(f'CREATE OR REPLACE VIEW {db}.customer_profile {contract} COMMENT {module.type_literal(row["comment"])} AS {row["as_select"]}')

    class DamageAfterExchange(module.Client):
        def query(self, sql):
            result = super().query(sql)
            if sql.startswith('EXCHANGE TABLES'):
                damage_view()
                if stage == 'exchange-ack-recovery':
                    raise module.CutoverError('SYNTHETIC_ACK_LOSS')
            return result

    if stage == 'active-retry':
        assert module.cutover(client, db, writers_stopped=True)['status'] == 'ACTIVE'
        damage_view()
    elif stage == 'exchange-ack-recovery':
        with pytest.raises(module.CutoverError, match='SYNTHETIC_ACK_LOSS'):
            module.cutover(DamageAfterExchange(url), db, writers_stopped=True)
    else:
        client = DamageAfterExchange(url)
    with pytest.raises(module.CutoverError, match='UNEXPECTED_EXISTING_VIEW'):
        module.cutover(client, db, writers_stopped=True)
    if stage != 'active-retry':
        assert query(f"SELECT count() FROM system.tables WHERE database='{db}' AND name='customer_profile_pre_identity'") == '0\n'


@pytest.mark.parametrize('damage', ['limit-zero', 'changed-priority'])
def test_active_retry_rejects_unexpected_view_structure(fixture, damage):
    module = load_module()
    url, db, query = fixture
    client = module.Client(url)
    module.cutover(client, db, writers_stopped=True)
    select = client.rows(f"SELECT as_select FROM system.tables WHERE database='{db}' AND name='customer_profile'")[0]['as_select']
    altered = select + ' LIMIT 0' if damage == 'limit-zero' else select.replace('toUInt8(1)', 'toUInt8(2)')
    assert altered != select
    comment = client.rows(f"SELECT comment FROM system.tables WHERE database='{db}' AND name='customer_profile'")[0]['comment']
    query(f"CREATE OR REPLACE VIEW {db}.customer_profile DEFINER = identity_publication_definer SQL SECURITY DEFINER COMMENT '{comment}' AS {altered}")
    with pytest.raises(module.CutoverError, match='UNEXPECTED_EXISTING_VIEW'):
        module.cutover(client, db, writers_stopped=True)


@pytest.mark.parametrize('damage', ['snapshot-view', 'snapshot-empty', 'snapshot-columns', 'resolved-engine', 'broken-resolved-view'])
def test_active_retry_validates_dependencies_and_reads(fixture, damage):
    module = load_module()
    url, db, query = fixture
    client = module.Client(url)
    module.cutover(client, db, writers_stopped=True)
    legacy = f'{db}.customer_profile_legacy_snapshot'
    resolved = f'{db}.customer_profile_resolved'
    if damage == 'snapshot-view':
        query(f'RENAME TABLE {legacy} TO {db}.saved_snapshot')
        query(f'CREATE VIEW {legacy} AS SELECT * FROM {db}.saved_snapshot')
    elif damage == 'snapshot-empty':
        query(f'TRUNCATE TABLE {legacy}')
    elif damage == 'snapshot-columns':
        query(f'ALTER TABLE {legacy} DROP COLUMN first_name')
    else:
        query(f'RENAME TABLE {resolved} TO {db}.saved_resolved')
        if damage == 'resolved-engine':
            query(f'CREATE TABLE {resolved} AS {db}.saved_resolved ENGINE=Memory')
            query(f'INSERT INTO {resolved} SELECT * FROM {db}.saved_resolved')
        else:
            query(f'CREATE VIEW {resolved} AS SELECT * FROM {db}.saved_resolved')
            query(f'DROP TABLE {db}.saved_resolved SYNC')
    with pytest.raises(module.CutoverError):
        module.cutover(client, db, writers_stopped=True)
    assert query(f'SELECT count() FROM {db}.customer_profile_pre_identity') == '2\n'


@pytest.mark.parametrize('damage,error', [
    ('snapshot-memory', 'INVALID_LEGACY_SNAPSHOT_ENGINE'),
    ('snapshot-view', 'INVALID_LEGACY_SNAPSHOT_ENGINE'),
    ('snapshot-columns', 'INVALID_LEGACY_SNAPSHOT_COLUMNS'),
    ('snapshot-rows', 'LEGACY_COPY_MISMATCH_OR_WRITER_ACTIVE'),
    ('resolved-memory', 'UNSUPPORTED_RESOLVED_ENGINE'),
    ('resolved-unreadable', 'CLICKHOUSE_REQUEST_FAILED_RECONCILE_BEFORE_RETRY'),
])
def test_initial_cutover_rejects_invalid_dependencies_before_exchange(fixture, damage, error):
    module = load_module()
    url, db, query = fixture
    client = module.Client(url)
    live = f'{db}.customer_profile'
    legacy = f'{db}.customer_profile_legacy_snapshot'
    resolved = f'{db}.customer_profile_resolved'
    before = client.fingerprint(live)
    original = client.rows(f"SELECT engine,uuid FROM system.tables WHERE database='{db}' AND name='customer_profile'")
    original_columns = client.rows(f"SELECT name,type FROM system.columns WHERE database='{db}' AND table='customer_profile' ORDER BY position")
    if damage.startswith('snapshot-'):
        if damage == 'snapshot-view':
            query(f'CREATE TABLE {db}.saved_snapshot AS {live}')
            query(f'INSERT INTO {db}.saved_snapshot SELECT * FROM {live}')
            query(f'CREATE VIEW {legacy} AS SELECT * FROM {db}.saved_snapshot')
        else:
            engine = ' ENGINE=Memory' if damage == 'snapshot-memory' else ''
            query(f'CREATE TABLE {legacy} AS {live}{engine}')
            query(f'INSERT INTO {legacy} SELECT * FROM {live}')
            if damage == 'snapshot-columns':
                query(f'ALTER TABLE {legacy} MODIFY COLUMN app_id LowCardinality(String)')
            elif damage == 'snapshot-rows':
                query(f"INSERT INTO {legacy} (app_id,customer_id) VALUES ('app1','unexpected')")
        if damage != 'snapshot-rows':
            assert client.fingerprint(legacy) == before, 'row equality does not prove engine/schema safety'
    else:
        query(f'RENAME TABLE {resolved} TO {db}.saved_resolved')
        if damage == 'resolved-memory':
            query(f'CREATE TABLE {resolved} AS {db}.saved_resolved ENGINE=Memory')
            query(f'INSERT INTO {resolved} SELECT * FROM {db}.saved_resolved')
        else:
            query(f"CREATE VIEW {resolved} AS SELECT * REPLACE (CAST(toString(throwIf(notEmpty(customer_id), 'unreadable fixture')), 'Nullable(String)') AS first_name) FROM {db}.saved_resolved")
            assert query(f'SELECT count(),uniqExact(tuple(app_id,customer_id)) FROM {resolved}') == '2\t2\n'

    with pytest.raises(module.CutoverError, match=error):
        module.cutover(client, db, writers_stopped=True)
    assert client.rows(f"SELECT engine,uuid FROM system.tables WHERE database='{db}' AND name='customer_profile'") == original
    assert client.rows(f"SELECT name,type FROM system.columns WHERE database='{db}' AND table='customer_profile' ORDER BY position") == original_columns
    assert client.fingerprint(live) == before
    assert query(f"SELECT count() FROM system.tables WHERE database='{db}' AND name='customer_profile_pre_identity'") == '0\n'


def test_initial_cutover_verifies_live_read_after_exchange(fixture):
    module = load_module()
    url, db, query = fixture

    class BreakAfterExchange(module.Client):
        def query(self, sql):
            result = super().query(sql)
            if sql.startswith('EXCHANGE TABLES'):
                query(f'DROP TABLE {db}.customer_profile_legacy_snapshot SYNC')
            return result

    with pytest.raises(module.CutoverError, match='MISSING_LEGACY_SNAPSHOT'):
        module.cutover(BreakAfterExchange(url), db, writers_stopped=True)


def test_cutover_verifies_rollback_identity_after_rename(fixture):
    module = load_module()
    url, db, query = fixture

    class ReplaceAfterRename(module.Client):
        def query(self, sql):
            result = super().query(sql)
            if sql.startswith('RENAME TABLE'):
                rollback = f'{db}.customer_profile_pre_identity'
                query(f'RENAME TABLE {rollback} TO {db}.saved_rollback')
                query(f'CREATE TABLE {rollback} AS {db}.saved_rollback')
            return result

    with pytest.raises(module.CutoverError, match='INVALID_ROLLBACK_IDENTITY'):
        module.cutover(ReplaceAfterRename(url), db, writers_stopped=True)


def test_exchange_ack_loss_recovers_without_recopy_or_duplicate(fixture):
    module = load_module()
    url, db, query = fixture

    class LostAck(module.Client):
        def query(self, sql):
            result = super().query(sql)
            if sql.startswith('EXCHANGE TABLES'):
                raise module.CutoverError('SYNTHETIC_ACK_LOSS')
            return result

    with pytest.raises(module.CutoverError, match='SYNTHETIC_ACK_LOSS'):
        module.cutover(LostAck(url), db, writers_stopped=True)
    assert query(f'SELECT count() FROM {db}.customer_profile') == '3\n'
    assert module.cutover(module.Client(url), db, writers_stopped=True)['status'] == 'ACTIVE'
    assert query(f'SELECT count() FROM {db}.customer_profile_pre_identity') == '2\n'


def test_copy_ack_loss_refuses_incomplete_legacy_snapshot(fixture):
    module = load_module()
    url, db, query = fixture
    query(f'CREATE TABLE {db}.customer_profile_legacy_snapshot AS {db}.customer_profile')
    with pytest.raises(module.CutoverError, match='LEGACY_COPY_MISMATCH_OR_WRITER_ACTIVE'):
        module.cutover(module.Client(url), db, writers_stopped=True)
    assert query(f'SELECT count() FROM {db}.customer_profile') == '2\n'


def test_cutover_refuses_without_exclusive_writer_window(fixture):
    module = load_module()
    url, db, query = fixture
    with pytest.raises(module.CutoverError, match='WRITERS_NOT_STOPPED'):
        module.cutover(module.Client(url), db, writers_stopped=False)
    assert query(f"SELECT engine FROM system.tables WHERE database='{db}' AND name='customer_profile'") == 'MergeTree\n'


@pytest.mark.parametrize('app_id,customer_id', [('app1', 'updated'), ('', 'blank-app'), ('  ', 'space-app'), ('app1', ''), ('app1', '  ')])
def test_cutover_rejects_invalid_resolved_business_grain(fixture, app_id, customer_id):
    module = load_module()
    url, db, query = fixture
    query(f"INSERT INTO {db}.customer_profile_resolved (app_id,customer_id,first_name) VALUES ('{app_id}','{customer_id}','conflicting')")
    before = module.Client(url).fingerprint(f'{db}.customer_profile')
    with pytest.raises(module.CutoverError, match='RESOLVED_GRAIN_INVALID'):
        module.cutover(module.Client(url), db, writers_stopped=True)
    assert query(f"SELECT engine FROM system.tables WHERE database='{db}' AND name='customer_profile'") == 'MergeTree\n'
    assert module.Client(url).fingerprint(f'{db}.customer_profile') == before
    assert query(f"SELECT count() FROM system.tables WHERE database='{db}' AND name IN ('customer_profile_identity_candidate','customer_profile_legacy_snapshot','customer_profile_pre_identity')") == '0\n'


def test_cutover_rejects_legacy_duplicate_business_grain(fixture):
    module = load_module()
    url, db, query = fixture
    query(f"INSERT INTO {db}.customer_profile (app_id,customer_id) VALUES ('app1','old')")
    with pytest.raises(module.CutoverError, match='LEGACY_GRAIN_INVALID'):
        module.cutover(module.Client(url), db, writers_stopped=True)
    assert query(f"SELECT engine FROM system.tables WHERE database='{db}' AND name='customer_profile'") == 'MergeTree\n'
