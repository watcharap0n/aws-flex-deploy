"""Migration 026 tests: only a newly-created, task-owned local database.

Run with the sibling stitcher .venv/bin/python -m pytest
 tests/integration/test_customer_app_scope.py (includes real normalizer dependencies).
No live clients, production DSNs, or shared fixture resets.
"""
import importlib.util
import sys
from pathlib import Path
from uuid import uuid4

import psycopg2
import pytest
from psycopg2 import sql

ROOT = Path(__file__).resolve().parents[2]
MIGRATION = ROOT / 'database/migrations/026_customer_app_scope.sql'
TABLES = ('control.ingestion_batch', 'raw.customer', 'raw.customer_current',
          'staging.customer_profile_history', 'staging.customer_profile')


def query(c, statement, args=None):
    with c.cursor() as q:
        q.execute(statement, args)
        return q.fetchall() if q.description else []


@pytest.fixture(scope='module')
def owned_database():
    name = 'app_id_columns_test'
    admin = psycopg2.connect(host='127.0.0.1', port=61577, user='postgres', dbname='postgres', connect_timeout=3)
    admin.autocommit = True
    assert not query(admin, 'SELECT 1 FROM pg_database WHERE datname=%s', (name,)), 'refuse preexisting database'
    query(admin, sql.SQL("CREATE DATABASE {} TEMPLATE template0 ENCODING 'UTF8'").format(sql.Identifier(name)))
    try:
        yield name
    finally:
        query(admin, sql.SQL('DROP DATABASE {}').format(sql.Identifier(name)))
        admin.close()


@pytest.fixture
def db(owned_database):
    c = psycopg2.connect(host='127.0.0.1', port=61577, user='postgres', dbname=owned_database)
    assert (c.info.host, c.info.port, c.info.dbname) == ('127.0.0.1', 61577, 'app_id_columns_test')
    c.autocommit = True
    query(c, 'DROP SCHEMA IF EXISTS connector CASCADE')
    query(c, (ROOT / 'tests/integration/fixtures/customer_history_base.sql').read_text())
    query(c, '''ALTER TABLE control.ingestion_batch ADD COLUMN batch_ref text;
        ALTER TABLE control.source_landing_batch ADD COLUMN batch_ref text, ADD COLUMN source_code text;
        CREATE TABLE control.source_system(source_system_id bigint PRIMARY KEY, source_code text);
        INSERT INTO control.source_system VALUES (1,'source-not-an-app');
        CREATE TABLE control.stitching_job(job_id uuid PRIMARY KEY, ingestion_batch_id uuid);
        CREATE SCHEMA connector;
        CREATE TABLE connector.sync_run(ingestion_batch_id uuid,landing_batch_id uuid,stitching_job_id uuid);
        CREATE FUNCTION raw.set_customer_updated_at() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN NEW.updated_at=now(); RETURN NEW; END $$;
        CREATE TRIGGER trg_raw_customer_updated_at BEFORE UPDATE ON raw.customer
        FOR EACH ROW EXECUTE FUNCTION raw.set_customer_updated_at();''')
    query(c, (ROOT / 'database/migrations/019_customer_typed_history.sql').read_text())
    query(c, (ROOT / 'database/migrations/022_customer_bulk_ingestion.sql').read_text())
    c.autocommit = False
    try:
        yield c
    finally:
        c.rollback()
        c.close()


def install(c):
    c.commit()
    assert MIGRATION.exists(), 'physical app_id migration 026 missing'
    c.autocommit = True
    query(c, MIGRATION.read_text())
    c.autocommit = False


def batch(c, app='000222', ref=None, connector=False):
    b, landing = str(uuid4()), str(uuid4())
    ref = ref if ref is not None else f'2026-09-19-120001-{app}-{uuid4().hex[:12]}'
    query(c, "INSERT INTO control.source_landing_batch(batch_id,batch_ref,source_code) VALUES(%s,%s,'source-not-an-app')", (landing, ref))
    if connector:
        query(c, 'INSERT INTO connector.sync_run(landing_batch_id) VALUES(%s)', (landing,))
    query(c, "INSERT INTO control.ingestion_batch(batch_id,landing_batch_id,source_system_id,batch_ref,created_at) VALUES(%s,%s,1,%s,clock_timestamp())", (b, landing, ref))
    return b


def upload(c, b, name, key='same-source-customer'):
    # Actual Lambda bulk writer: explicit COPY columns into LIKE-current stage.
    module = ROOT / '_lambda/ingestion_data/customer_bulk.py'
    spec = importlib.util.spec_from_file_location('app_scope_bulk', module)
    assert spec is not None and spec.loader is not None
    bulk = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bulk)
    with c.cursor() as q:
        bulk.upsert_customers(q, [{'source_customer_id': key, 'first_name': name}], b, 1)
    return query(c, 'SELECT raw_customer_id::text FROM raw.customer_current WHERE source_customer_id=%s', (key,))[0][0]


def normalize(c, b, raw, name):
    for table in ('staging.customer_profile_history', 'staging.customer_profile'):
        conflict = 'ON CONFLICT(raw_customer_id) DO UPDATE SET batch_id=EXCLUDED.batch_id,first_name_normalized=EXCLUDED.first_name_normalized' if table.endswith('customer_profile') else 'ON CONFLICT DO NOTHING'
        query(c, f'''INSERT INTO {table}(batch_id,raw_customer_id,source_system_id,source_customer_id,first_name_normalized)
            VALUES(%s,%s,1,'same-source-customer',%s) {conflict}''', (b, raw, name))


def test_future_uploads_have_literal_physical_app_id_per_observation(db):
    install(db)
    a = batch(db, '000222')
    raw = upload(db, a, 'A')
    normalize(db, a, raw, 'a')
    b = batch(db, '222')
    assert upload(db, b, 'B') == raw  # deliberately retains global current key
    normalize(db, b, raw, 'b')
    for table in ('raw.customer', 'staging.customer_profile_history'):
        assert query(db, f'SELECT batch_id::text FROM {table} WHERE app_id=%s', ('000222',)) == [(a,)]
        assert query(db, f'SELECT batch_id::text FROM {table} WHERE app_id=%s', ('222',)) == [(b,)]
    for table in ('raw.customer_current', 'staging.customer_profile'):
        assert query(db, f'SELECT app_id FROM {table}') == [('222',)]
    assert query(db, 'SELECT app_id FROM control.ingestion_batch ORDER BY created_at') == [('000222',), ('222',)]
    # Replay older A and typed normalized retry cannot rewind B or reject app_id.
    upload(db, a, 'A')
    assert query(db, 'SELECT first_name,app_id FROM raw.customer_current') == [('B', '222')]
    assert query(db, "SELECT count(*) FROM information_schema.columns WHERE table_schema IN ('raw','staging','control') AND column_name='app_id' AND data_type='text' AND is_nullable='YES' AND column_default IS NULL") == [(5,)]


@pytest.mark.parametrize('ref,connector,reason', [
    ('bad-ref', False, 'INVALID_UPLOAD_BATCH_REF'),
    ('2026-09-19-120001-000222-123456789abc ', False, 'INVALID_UPLOAD_BATCH_REF'),
    ('2026-09-19-120001-000222-123456789abc', True, 'CONNECTOR_EXCLUDED'),
])
def test_unprovable_or_connector_lineage_remains_null_with_diagnostic(db, ref, connector, reason):
    install(db)
    b = batch(db, ref=ref, connector=connector)
    raw = upload(db, b, 'original')
    normalize(db, b, raw, 'original')
    for table in TABLES:
        assert query(db, f'SELECT app_id FROM {table}') == [(None,)]
    assert query(db, 'SELECT app_id,error_code FROM control.customer_app_scope_status(%s)', (b,)) == [(None, reason)]


def old_values(c):
    return {t: query(c, f"SELECT to_jsonb(t)-'app_id' FROM {t} t ORDER BY (to_jsonb(t)-'app_id')::text") for t in TABLES}


def test_bounded_backfill_only_changes_app_id_preserves_history_and_clocks(db):
    a = batch(db, '000222')
    raw = upload(db, a, 'A')
    normalize(db, a, raw, 'a')
    b = batch(db, '333')
    upload(db, b, 'B')
    normalize(db, b, raw, 'b')
    before = old_values(db)
    install(db)
    # Column addition does not do an unbounded UPDATE or rewrite historical values.
    assert old_values(db) == before
    for table in TABLES:
        assert query(db, f'SELECT count(*) FROM {table} WHERE app_id IS NOT NULL') == [(0,)]
    for batch_id, expected_app in ((a, '000222'), (b, '333')):
        for table in TABLES:
            result = query(db, 'SELECT * FROM control.backfill_customer_app_id(%s::regclass,%s,NULL,1)', (table, batch_id))[0]
            assert result[-2:] == (expected_app, None)
            assert query(db, f'SELECT count(*) FROM {table} WHERE batch_id=%s AND app_id IS DISTINCT FROM %s', (batch_id, expected_app)) == [(0,)]
    assert old_values(db) == before
    assert query(db, 'SELECT app_id FROM raw.customer_current') == [('333',)]
    assert query(db, "SELECT app_id,first_name FROM raw.customer ORDER BY app_id") == [('000222', 'A'), ('333', 'B')]
    # Retry is a no-op, guards restored, rollback is ordinary PostgreSQL atomicity.
    assert query(db, "SELECT updated_count FROM control.backfill_customer_app_id('raw.customer',%s,NULL,1)", (a,)) == [(0,)]
    db.commit()
    for table in ('raw.customer', 'staging.customer_profile_history'):
        with pytest.raises(psycopg2.Error, match='immutable'):
            query(db, f"UPDATE {table} SET app_id='9'")
        db.rollback()
        with pytest.raises(psycopg2.Error, match='immutable'):
            query(db, f'DELETE FROM {table}')
        db.rollback()
    assert all(r[0] == 'O' for r in query(db, "SELECT tgenabled FROM pg_trigger WHERE NOT tgisinternal AND tgrelid IN ('raw.customer'::regclass,'raw.customer_current'::regclass,'staging.customer_profile_history'::regclass)"))


def test_idempotent_rerun_and_fail_closed_function_drift(db):
    install(db)
    before = query(db, "SELECT oid,pg_get_functiondef(oid) FROM pg_proc WHERE proname IN ('set_customer_batch_app_id','set_customer_observation_app_id','backfill_customer_app_id') ORDER BY oid")
    install(db)
    assert query(db, "SELECT oid,pg_get_functiondef(oid) FROM pg_proc WHERE proname IN ('set_customer_batch_app_id','set_customer_observation_app_id','backfill_customer_app_id') ORDER BY oid") == before
    query(db, '''CREATE OR REPLACE FUNCTION raw.set_customer_observation_app_id() RETURNS trigger
        LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,pg_temp AS $$
        BEGIN NEW.app_id:='wrong'; RETURN NEW; END $$''')
    db.commit()
    with pytest.raises(psycopg2.Error, match='incompatible existing function'):
        install(db)
    db.rollback()


def test_backfill_chunk_cursor_failure_rollback_and_private_acl(db):
    a = batch(db)
    for i in range(3):
        upload(db, a, f'A-{i}', key=f'key-{i}')
    install(db)
    query(db, "SELECT * FROM control.backfill_customer_app_id('control.ingestion_batch',%s)", (a,))
    db.commit()
    before = old_values(db)
    first = query(db, "SELECT * FROM control.backfill_customer_app_id('raw.customer',%s,NULL,1)", (a,))[0]
    assert first[:2] == (1, 1)
    db.rollback()
    assert query(db, 'SELECT count(*) FROM raw.customer WHERE app_id IS NOT NULL') == [(0,)]
    assert old_values(db) == before
    cursor, total = None, 0
    while True:
        row = query(db, "SELECT * FROM control.backfill_customer_app_id('raw.customer',%s,%s,1)", (a, cursor))[0]
        db.commit()
        if not row[0]:
            break
        cursor, total = row[2], total + row[1]
    assert total == 3
    assert old_values(db) == before
    assert query(db, "SELECT count(*) FROM pg_proc p CROSS JOIN LATERAL aclexplode(coalesce(p.proacl,acldefault('f',p.proowner))) a WHERE p.oid='control.backfill_customer_app_id(regclass,uuid,uuid,integer)'::regprocedure AND a.grantee=0 AND a.privilege_type='EXECUTE'") == [(0,)]
    query(db, '''CREATE FUNCTION raw.unreviewed_update() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN NEW.first_name='bad'; RETURN NEW; END $$;
        CREATE TRIGGER unreviewed BEFORE UPDATE ON raw.customer FOR EACH ROW EXECUTE FUNCTION raw.unreviewed_update()''')
    db.commit()
    with pytest.raises(psycopg2.Error, match='Unreviewed UPDATE trigger'):
        query(db, "SELECT * FROM control.backfill_customer_app_id('raw.customer',%s)", (a,))
    db.rollback()
    assert query(db, "SELECT tgenabled FROM pg_trigger WHERE tgname='customer_history_immutable'") == [('O',)]
    assert old_values(db) == before


def test_original_typed_business_immutability_survives_new_column(db):
    install(db)
    a = batch(db)
    upload(db, a, 'original')
    db.commit()
    with pytest.raises(psycopg2.Error, match='snapshot cannot change'):
        upload(db, a, 'corrupt')
    db.rollback()
    assert query(db, 'SELECT first_name,app_id FROM raw.customer') == [('original', '000222')]


def test_connector_metadata_excludes_before_sync_run_is_registered(db):
    install(db)
    b, landing = str(uuid4()), str(uuid4())
    ref = '2026-09-19-120001-000222-123456789abc'
    query(db, "INSERT INTO control.source_landing_batch(batch_id,batch_ref,source_code) VALUES(%s,%s,'source-not-an-app')", (landing, ref))
    query(db, "INSERT INTO control.ingestion_batch(batch_id,landing_batch_id,source_system_id,batch_ref,metadata) VALUES(%s,%s,1,%s,'{\"connector_sync_run_id\":\"durable-connector\"}')", (b, landing, ref))
    raw = upload(db, b, 'connector')
    normalize(db, b, raw, 'connector')
    for table in TABLES:
        assert query(db, f'SELECT app_id FROM {table}') == [(None,)]


def test_missing_history_guard_blocks_backfill(db):
    a = batch(db)
    upload(db, a, 'original')
    install(db)
    query(db, 'DROP TRIGGER customer_history_immutable ON raw.customer')
    db.commit()
    with pytest.raises(psycopg2.Error, match='immutable trigger'):
        query(db, "SELECT * FROM control.backfill_customer_app_id('raw.customer',%s)", (a,))
    db.rollback()
    assert query(db, 'SELECT app_id FROM raw.customer') == [(None,)]


def test_real_normalizer_typed_retry_is_compatible(db):
    sys.path.insert(0, str(ROOT.parent / 'fargate-user-stitching/src'))
    from identity_stitcher.config import AppConfig, DatabaseConfig
    from identity_stitcher.standardize import standardize_customers
    config = AppConfig(database=DatabaseConfig('127.0.0.1', 61577, 'app_id_columns_test', 'postgres', '', 'disable'))
    install(db)
    a = batch(db, '000222')
    upload(db, a, 'First')
    assert standardize_customers(db, config, [a]) == 1
    b = batch(db, '333')
    upload(db, b, 'Second')
    assert standardize_customers(db, config, [b]) == 1
    assert standardize_customers(db, config, [a]) == 1
    assert query(db, 'SELECT app_id,first_name_normalized FROM staging.customer_profile_history ORDER BY app_id') == [('000222', 'first'), ('333', 'second')]
    assert query(db, 'SELECT app_id,first_name_normalized FROM staging.customer_profile') == [('333', 'second')]


def test_pinned_catalog_024_025_views_and_functions_unchanged(db):
    from test_customer_identity_publication import member, finish, transfer, batch as publication_batch
    stitcher = ROOT.parent / 'fargate-user-stitching'
    db.commit()
    db.autocommit = True
    query(db, 'DROP SCHEMA IF EXISTS connector CASCADE; DROP SCHEMA IF EXISTS platform CASCADE')
    query(db, (stitcher / 'tests/fixtures/repository_catalog.sql').read_text())
    query(db, (stitcher / 'src/identity_stitcher/sql/001_identity_model.sql').read_text())
    query(db, (ROOT / 'database/migrations/014_pipeline_tracking.sql').read_text())
    # Pinned post-019 tables omit functions; install actual 019/022 guards.
    m19 = (ROOT / 'database/migrations/019_customer_typed_history.sql').read_text()
    query(db, m19[m19.index('CREATE FUNCTION raw.reject_customer_history_mutation'):m19.index('-- Normalized observations')])
    query(db, '''CREATE TRIGGER customer_profile_history_immutable BEFORE UPDATE OR DELETE OR TRUNCATE
        ON staging.customer_profile_history FOR EACH STATEMENT EXECUTE FUNCTION raw.reject_customer_history_mutation()''')
    query(db, (ROOT / 'database/migrations/022_customer_bulk_ingestion.sql').read_text())
    query(db, (ROOT / 'database/migrations/024_customer_identity_publication.sql').read_text())
    query(db, (ROOT / 'database/migrations/025_customer_publication_status.sql').read_text())
    db.autocommit = False
    before_functions = query(db, "SELECT oid,pg_get_functiondef(oid) FROM pg_proc WHERE pronamespace IN ('raw'::regnamespace,'control'::regnamespace) ORDER BY oid")
    before_views = query(db, "SELECT c.oid,c.relacl,pg_get_viewdef(c.oid) FROM pg_class c WHERE c.relnamespace='control'::regnamespace AND c.relkind='v' ORDER BY c.oid")
    install(db)
    ids = [r[0] for r in before_functions]
    assert query(db, 'SELECT oid,pg_get_functiondef(oid) FROM pg_proc WHERE oid=ANY(%s) ORDER BY oid', (ids,)) == before_functions
    assert query(db, "SELECT c.oid,c.relacl,pg_get_viewdef(c.oid) FROM pg_class c WHERE c.relnamespace='control'::regnamespace AND c.relkind='v' ORDER BY c.oid") == before_views
    b = publication_batch(db, app='000222')
    # member fixture duplicates the membership that actual 022 captures.
    from unittest.mock import patch
    import test_customer_identity_publication as base
    execute = base.execute
    def without_redundant_membership(c, statement, args=None):
        if statement.startswith('INSERT INTO raw.customer_batch_member'):
            statement += ' ON CONFLICT DO NOTHING'
        return execute(c, statement, args)
    with patch.object(base, 'execute', without_redundant_membership):
        member(db, b)
    finish(db, b)
    db.commit()
    assert transfer(db, b)['status'] == 'PENDING'
    assert transfer(db, b)['app_id'] == '000222'
    assert query(db, 'SELECT app_id FROM raw.customer') == [('000222',)]


@pytest.mark.parametrize('app', ['0', '000000', '9' * 50])
def test_literal_apps_never_cast_to_numeric_or_drop_leading_zeroes(db, app):
    install(db)
    b = batch(db, app)
    upload(db, b, 'literal')
    assert query(db, 'SELECT app_id FROM raw.customer') == [(app,)]


def test_direct_legacy_upsert_derives_history_without_payload_changes(db):
    install(db)
    a, b = batch(db, '000222'), batch(db, '222')
    for batch_id, name in ((a, 'A'), (b, 'B')):
        query(db, '''INSERT INTO raw.customer_current(batch_id,source_system_id,source_customer_id,first_name,app_id)
            VALUES(%s,1,'same',%s,'caller-lie') ON CONFLICT(source_system_id,source_customer_id)
            DO UPDATE SET batch_id=EXCLUDED.batch_id,first_name=EXCLUDED.first_name''', (batch_id, name))
    assert query(db, 'SELECT app_id,first_name FROM raw.customer ORDER BY app_id') == [('000222', 'A'), ('222', 'B')]


@pytest.mark.parametrize('mutation,reason', [
    ('UPDATE control.source_landing_batch SET batch_ref=\'different\'', 'BATCH_LINEAGE_CONFLICT'),
    ('UPDATE control.source_landing_batch SET source_code=\'different\'', 'BATCH_LINEAGE_CONFLICT'),
    ('UPDATE control.ingestion_batch SET landing_batch_id=NULL', 'MISSING_LANDING_LINEAGE'),
])
def test_backfill_skips_unproven_lineage_without_guessing(db, mutation, reason):
    a = batch(db)
    upload(db, a, 'untouched')
    query(db, mutation)
    install(db)
    before = old_values(db)
    for table in TABLES:
        assert query(db, 'SELECT updated_count,app_id,error_code FROM control.backfill_customer_app_id(%s::regclass,%s)', (table, a)) == [(0, None, reason)]
    assert old_values(db) == before


def test_preexisting_wrong_app_id_is_not_silently_corrected(db):
    a = batch(db)
    upload(db, a, 'original')
    # Simulate a preexisting additive column without relaxing an immutable guard.
    query(db, 'ALTER TABLE control.ingestion_batch ADD COLUMN app_id text')
    query(db, "UPDATE control.ingestion_batch SET app_id='invented'")
    install(db)
    with pytest.raises(psycopg2.Error, match='conflicts with exact durable batch lineage'):
        query(db, "SELECT * FROM control.backfill_customer_app_id('control.ingestion_batch',%s)", (a,))
    db.rollback()
    assert query(db, 'SELECT app_id FROM control.ingestion_batch') == [('invented',)]
