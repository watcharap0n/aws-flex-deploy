"""039 logical app/user winner: fresh, task-owned UTF8 database on localhost:55441.

No staging credentials, external services, shared database resets or historical rewrites.
Run with PYTHONDONTWRITEBYTECODE=1 fargate-user-stitching/.venv/bin/python
    -m pytest --confcutdir=tests/integration -p no:cacheprovider <this file>.
"""
from contextlib import closing
from datetime import datetime, timezone
from uuid import uuid4
import hashlib

import psycopg2
from psycopg2 import sql
import pytest

import test_customer_app_backfill as base
import test_customer_app_backfill_access_paths as access
from identity_stitcher import app_backfill as backfill
from identity_stitcher.config import AppConfig, DatabaseConfig

ENDPOINT = 'host=127.0.0.1 port=55441 user=postgres connect_timeout=3'
# This is the explicitly owned native fixture, NOT a configurable production DSN.
SYSTEM_ID = '7689474389744318792'
APP = '0043'
MIGRATION = base.MIGRATIONS / '039_customer_app_backfill_logical_winner.sql'
FINALIZER = 'control.finalize_customer_app_backfill()'
PRIOR_MD5 = 'e19dedc9a726ab641c8006bd61576126'
q = base.query


@pytest.fixture(scope='module')
def owned_database():
    name = 'customer_app_backfill_logical_' + uuid4().hex[:12]
    with closing(psycopg2.connect(ENDPOINT, dbname='postgres')) as admin:
        admin.autocommit = True
        assert q(admin, '''SELECT host(inet_server_addr()),inet_server_port(),
            (pg_control_system()).system_identifier::text,current_user''') == [
                ('127.0.0.1', 55441, SYSTEM_ID, 'postgres')]
        assert not q(admin, 'SELECT 1 FROM pg_database WHERE datname=%s', (name,))
        q(admin, sql.SQL("CREATE DATABASE {} TEMPLATE template0 ENCODING 'UTF8'").format(sql.Identifier(name)))
        try:
            yield name
        finally:
            q(admin, sql.SQL('DROP DATABASE {}').format(sql.Identifier(name)))
            assert not q(admin, 'SELECT 1 FROM pg_database WHERE datname=%s', (name,))


@pytest.fixture
def db(owned_database):
    with closing(psycopg2.connect(ENDPOINT, dbname=owned_database)) as c:
        assert (c.info.host, c.info.port, c.info.dbname) == ('127.0.0.1', 55441, owned_database)
        c.autocommit = True
        assert q(c, 'SHOW server_encoding') == [('UTF8',)]
        q(c, 'DROP SCHEMA IF EXISTS identity CASCADE; DROP SCHEMA IF EXISTS connector CASCADE')
        q(c, (base.ROOT / 'tests/integration/fixtures/customer_history_base.sql').read_text())
        q(c, '''ALTER TABLE control.source_landing_batch ADD source_code text, ADD batch_ref text,
            ADD status text, ADD output_published_at timestamptz, ADD updated_at timestamptz DEFAULT now();
            ALTER TABLE control.ingestion_batch ADD batch_ref text, ADD records_received bigint,
            ADD records_rejected bigint DEFAULT 0, ADD completed_at timestamptz, ADD updated_at timestamptz DEFAULT now();
            CREATE TABLE control.source_system(source_system_id bigint PRIMARY KEY,source_code text,source_name text);
            INSERT INTO control.source_system VALUES(1,'legacy-source','synthetic'),
                (2,'new-source','synthetic');''')
        q(c, (base.STITCHER / 'src/identity_stitcher/sql/001_identity_model.sql').read_text())
        for file in ('014_pipeline_tracking.sql', '019_customer_typed_history.sql',
                     '022_customer_bulk_ingestion.sql', '024_customer_identity_publication.sql',
                     '025_customer_publication_status.sql', '026_customer_app_scope.sql',
                     '027_customer_app_backfill.sql', '028_customer_app_backfill_access_paths.sql'):
            q(c, (base.MIGRATIONS / file).read_text())
        assert q(c, 'SELECT md5(prosrc) FROM pg_proc WHERE oid=%s::regprocedure', (FINALIZER,)) == [(PRIOR_MD5,)]
        c.autocommit = False
        try:
            yield c
        finally:
            c.rollback()


def install(c):
    access.install_sql(c, MIGRATION.read_text())


def batch(c, *, source=1, created='2026-09-01T00:00:00Z', batch_id=None):
    if batch_id is None:
        return base.batch(c, app=APP, source=source, created=created)
    landing = str(uuid4())
    ref = f'2026-09-01-120001-{APP}-{uuid4().hex[:12]}'
    q(c, '''INSERT INTO control.source_landing_batch(batch_id,batch_ref,source_code,output_status)
        SELECT %s,%s,source_code,'PUBLISHED' FROM control.source_system WHERE source_system_id=%s''',
        (landing, ref, source))
    q(c, '''INSERT INTO control.ingestion_batch(batch_id,landing_batch_id,source_system_id,batch_ref,status,created_at)
        VALUES(%s,%s,%s,%s,'completed',%s)''', (batch_id, landing, source, ref, created))
    return batch_id


def snapshot(c):
    return {table: q(c, f'''SELECT count(*), md5(string_agg(md5(to_jsonb(t)::text), E'\\n'
        ORDER BY md5(to_jsonb(t)::text) COLLATE "C")) FROM {table} t''') for table in (
            'raw.customer', 'raw.customer_batch_member', 'raw.customer_current',
            'staging.customer_profile', 'identity.customer_entity', 'identity.customer_entity_member',
            'identity.match_pair', 'control.stitching_job')}


def configuration(c):
    return AppConfig(DatabaseConfig('127.0.0.1', 55441, c.info.dbname, 'postgres', '', 'disable'),
                     duckdb_memory_limit='256MB', duckdb_threads=1)


def extract(c):
    return backfill.extract_history(c, APP, datetime(2026, 9, 19, tzinfo=timezone.utc))


def test_extraction_ranks_logical_user_across_physical_sources_before_migration(db):
    older = batch(db, source=1)
    first = base.upload(db, older, user='X', name='old', source=1)
    newer = batch(db, source=2, created='2026-09-02T00:00:00Z')
    second = base.upload(db, newer, user='X', name='new', source=2)
    db.commit()
    manifest, rows = extract(db)
    assert [(r['batch_id'], r['source_system_id'], r['raw_customer_id'], r['first_name']) for r in rows] == [
        (newer, 2, second, 'new')]
    assert [(m['batch_id'], m['expected_members'], m['available_observations']) for m in manifest] == [
        (older, 1, 1), (newer, 1, 1)]
    assert first != second


def test_migration_and_runtime_export_only_latest_source_with_immutable_coverage(db):
    install(db)
    older = batch(db, source=1)
    old_raw = base.upload(db, older, user='X', name='old', source=1)
    newer = batch(db, source=2, created='2026-09-02T00:00:00Z')
    new_raw = base.upload(db, newer, user='X', name='new', source=2)
    db.commit()
    before = snapshot(db)
    manifest, rows = extract(db)
    expected_digest = backfill.input_digest(rows)
    db.rollback()
    run = str(uuid4())
    result = backfill.run_backfill(configuration(db), APP, run,
        cutoff=datetime(2026, 9, 19, tzinfo=timezone.utc), expected_batches=[older, newer],
        expected_users=1)
    assert (result['status'], result['transfer_status'], result['row_count'], result['input_sha256']) == (
        'SUCCEEDED', 'PENDING', 1, expected_digest)
    assert q(db, '''SELECT batch_id::text,expected_members,available_observations
        FROM control.customer_app_backfill_batch WHERE run_id=%s ORDER BY batch_created_at,batch_id''',
        (run,)) == [(older, 1, 1), (newer, 1, 1)]
    assert q(db, '''SELECT source_system_id,user_id,raw_customer_id::text,source_batch_id::text,
        input_payload#>>'{raw,first_name}',payload->>'raw_customer_id'
        FROM control.customer_app_backfill_record WHERE run_id=%s''', (run,)) == [
            (2, 'X', new_raw, newer, 'new', new_raw)]
    assert q(db, '''SELECT input_payload->>'record_id' FROM control.customer_app_backfill_record
        WHERE run_id=%s''', (run,)) == [(backfill.record_id(APP, 2, 'X'),)]
    assert backfill.record_id(APP, 1, 'X') != backfill.record_id(APP, 2, 'X')
    assert q(db, '''SELECT user_id,payload->>'source_system_id',payload->>'raw_customer_id'
        FROM control.customer_identity_export WHERE transfer_id=%s''', (run,)) == [('X', '2', new_raw)]
    assert q(db, 'SELECT count(*) FROM control.customer_app_backfill_record WHERE raw_customer_id=%s',
             (old_raw,)) == [(0,)]
    assert snapshot(db) == before
    assert backfill.run_backfill(configuration(db), APP, run)['status'] == 'SUCCEEDED'


def test_migration_pins_028_body_and_preserves_catalog_security_and_access_paths(db):
    before = access.catalog_contract(db)
    identity = q(db, '''SELECT p.prosecdef,p.proconfig,p.proowner,p.proacl,p.oid
        FROM pg_proc p WHERE p.oid=%s::regprocedure''', (FINALIZER,))
    install(db)
    assert access.catalog_contract(db) == before
    assert q(db, '''SELECT p.prosecdef,p.proconfig,p.proowner,p.proacl,p.oid
        FROM pg_proc p WHERE p.oid=%s::regprocedure''', (FINALIZER,)) == identity
    body = q(db, 'SELECT prosrc FROM pg_proc WHERE oid=%s::regprocedure', (FINALIZER,))[0][0]
    prior = access.function_source(base.MIGRATIONS / '028_customer_app_backfill_access_paths.sql')
    assert body.replace('PARTITION BY m.source_customer_id',
                        'PARTITION BY m.source_system_id,m.source_customer_id') == prior
    assert 'EXCEPT SELECT source_batch_id,raw_customer_id,source_system_id,user_id' in body
    assert 'LEFT JOIN LATERAL (' in body and 'LIMIT 1' in body
    assert hashlib.md5(body.encode()).hexdigest() != PRIOR_MD5
    install(db)
    assert access.catalog_contract(db) == before
    assert q(db, 'SELECT prosrc FROM pg_proc WHERE oid=%s::regprocedure', (FINALIZER,)) == [(body,)]


@pytest.mark.parametrize('drift', ['body', 'security', 'search_path'])
def test_migration_rejects_unreviewed_prior_function_without_mutation(db, drift):
    if drift == 'body':
        definition = q(db, 'SELECT pg_get_functiondef(%s::regprocedure)', (FINALIZER,))[0][0]
        q(db, definition.replace('DECLARE r identity.resolution_run%ROWTYPE;',
                                 '-- unreviewed drift\nDECLARE r identity.resolution_run%ROWTYPE;'))
    elif drift == 'security':
        q(db, 'ALTER FUNCTION control.finalize_customer_app_backfill() SECURITY INVOKER')
    else:
        q(db, 'ALTER FUNCTION control.finalize_customer_app_backfill() SET search_path=public')
    db.commit()
    before = q(db, 'SELECT to_jsonb(p) FROM pg_proc p WHERE oid=%s::regprocedure', (FINALIZER,))
    with pytest.raises(psycopg2.Error, match='039 requires the exact 028 or 039'):
        install(db)
    assert q(db, 'SELECT to_jsonb(p) FROM pg_proc p WHERE oid=%s::regprocedure', (FINALIZER,)) == before


def test_migration_rejects_exact_027_body_without_replacing_it(db):
    definition = q(db, 'SELECT pg_get_functiondef(%s::regprocedure)', (FINALIZER,))[0][0]
    old_body = access.function_source(base.MIGRATION)
    assert old_body != access.function_source(base.MIGRATIONS / '028_customer_app_backfill_access_paths.sql')
    q(db, definition.replace(access.function_source(
        base.MIGRATIONS / '028_customer_app_backfill_access_paths.sql'), old_body))
    db.commit()
    before = q(db, 'SELECT to_jsonb(p) FROM pg_proc p WHERE oid=%s::regprocedure', (FINALIZER,))
    with pytest.raises(psycopg2.Error, match='039 requires the exact 028 or 039'):
        install(db)
    assert q(db, 'SELECT to_jsonb(p) FROM pg_proc p WHERE oid=%s::regprocedure', (FINALIZER,)) == before


@pytest.mark.parametrize('lower_source', [False, True])
def test_batch_created_at_wins_over_source_number_in_sql_and_stream(db, lower_source):
    install(db)
    first_source, latest_source = (2, 1) if lower_source else (1, 2)
    older = batch(db, source=first_source, created='2026-09-01T00:00:00Z')
    first_raw = base.upload(db, older, user='X', name='old', source=first_source)
    latest = batch(db, source=latest_source, created='2026-09-02T00:00:00Z')
    latest_raw = base.upload(db, latest, user='X', name='new', source=latest_source)
    db.commit()
    before = snapshot(db)
    manifest, count, rows = backfill.extract_history(db, APP,
        datetime(2026, 9, 19, tzinfo=timezone.utc), max_records=1, stream=True)
    assert count == 1
    assert [(r['batch_id'], r['source_system_id'], r['raw_customer_id']) for r in rows] == [
        (latest, latest_source, latest_raw)]
    assert [(r['expected_members'], r['available_observations']) for r in manifest] == [(1, 1), (1, 1)]
    db.rollback()
    run = str(uuid4())
    report = backfill.run_sparse_singleton_backfill(configuration(db), APP, run,
        cutoff=datetime(2026, 9, 19, tzinfo=timezone.utc), max_records=1,
        expected_batches=[older, latest], expected_users=1)
    assert (report['status'], report['row_count']) == ('SUCCEEDED', 1)
    assert q(db, '''SELECT source_system_id,source_batch_id::text,raw_customer_id::text
        FROM control.customer_app_backfill_record WHERE run_id=%s''', (run,)) == [
            (latest_source, latest, latest_raw)]
    assert q(db, 'SELECT count(*) FROM control.customer_app_backfill_record WHERE raw_customer_id=%s',
             (first_raw,)) == [(0,)]
    assert snapshot(db) == before


def test_equal_created_at_uses_batch_uuid_even_when_source_number_is_lower(db):
    install(db)
    instant = '2026-09-01T00:00:00Z'
    smaller = batch(db, source=2, created=instant,
                    batch_id='10000000-0000-0000-0000-000000000001')
    base.upload(db, smaller, user='X', name='loser', source=2)
    larger = batch(db, source=1, created=instant,
                   batch_id='f0000000-0000-0000-0000-000000000001')
    winner = base.upload(db, larger, user='X', name='winner', source=1)
    db.commit()
    manifest, rows = extract(db)
    assert [(r['batch_id'], r['source_system_id'], r['first_name']) for r in rows] == [
        (larger, 1, 'winner')]
    assert [m['batch_id'] for m in manifest] == [smaller, larger]
    db.rollback()
    run, workspace = base.prepare(db, [smaller, larger], app=APP)
    base.record(db, run, workspace, larger, winner, user='X', app=APP, source=1)
    base.finish(db, run)
    assert q(db, 'SELECT row_count FROM control.customer_identity_transfer WHERE backfill_run_id=%s',
             (run,)) == [(1,)]


@pytest.mark.parametrize('wrong', ['old_source', 'old_batch', 'wrong_raw', 'wrong_user', 'missing'])
def test_sql_exact_latest_tuple_rejects_nonwinner_and_rolls_back(db, wrong):
    install(db)
    older = batch(db, source=1)
    old_raw = base.upload(db, older, user='X', name='old', source=1)
    newer = batch(db, source=2, created='2026-09-02T00:00:00Z')
    new_raw = base.upload(db, newer, user='X', name='new', source=2)
    run, workspace = base.prepare(db, [older, newer], app=APP)
    if wrong != 'missing':
        base.record(db, run, workspace, older if wrong in ('old_source', 'old_batch') else newer,
                    old_raw if wrong == 'old_source' else str(uuid4()) if wrong == 'wrong_raw' else new_raw,
                    user='other-user' if wrong == 'wrong_user' else 'X', app=APP,
                    source=1 if wrong in ('old_source', 'old_batch') else 2)
    before = snapshot(db)
    db.commit()
    with pytest.raises(psycopg2.Error, match='BACKFILL_LATEST_INPUT_INCOMPLETE'):
        base.finish(db, run)
    db.rollback()
    assert q(db, 'SELECT status FROM control.customer_app_backfill_run WHERE run_id=%s', (run,)) == [('BUILDING',)]
    assert q(db, 'SELECT status FROM identity.resolution_run WHERE run_id=%s', (run,)) == [('RUNNING',)]
    assert q(db, 'SELECT count(*) FROM control.customer_identity_transfer') == [(0,)]
    assert q(db, 'SELECT count(*) FROM control.customer_identity_export') == [(0,)]
    assert snapshot(db) == before


@pytest.mark.parametrize('missing_latest', [False, True])
def test_missing_latest_raw_does_not_fall_back_to_old_source(db, missing_latest):
    install(db)
    older = batch(db, source=1)
    old_raw = base.upload(db, older, user='X', name='old', source=1)
    newer = batch(db, source=2, created='2026-09-02T00:00:00Z')
    new_raw = base.upload(db, newer, user='X', name='new', source=2)
    if missing_latest:
        # Only the task-owned database simulates raw retention, not a production mutation.
        q(db, 'ALTER TABLE raw.customer DISABLE TRIGGER USER')
        q(db, 'DELETE FROM raw.customer WHERE batch_id=%s', (newer,))
        q(db, 'ALTER TABLE raw.customer ENABLE TRIGGER USER')
    else:
        q(db, 'ALTER TABLE raw.customer DISABLE TRIGGER USER')
        q(db, 'DELETE FROM raw.customer WHERE batch_id=%s', (older,))
        q(db, 'ALTER TABLE raw.customer ENABLE TRIGGER USER')
    db.commit()
    run, workspace = base.prepare(db, [older, newer], app=APP)
    base.record(db, run, workspace, older if missing_latest else newer,
                old_raw if missing_latest else new_raw, user='X', app=APP,
                source=1 if missing_latest else 2)
    assert q(db, '''SELECT batch_id::text,expected_members,available_observations
        FROM control.customer_app_backfill_batch WHERE run_id=%s ORDER BY batch_created_at''',
        (run,)) == [(older, 1, int(missing_latest)), (newer, 1, int(not missing_latest))]
    db.commit()
    if missing_latest:
        with pytest.raises(backfill.BackfillError, match='LATEST_RAW_MISSING'):
            extract(db)
        db.rollback()
        with pytest.raises(psycopg2.Error, match='BACKFILL_LATEST_INPUT_INCOMPLETE'):
            base.finish(db, run)
        db.rollback()
        assert q(db, 'SELECT status FROM control.customer_app_backfill_run WHERE run_id=%s', (run,)) == [('BUILDING',)]
        assert q(db, 'SELECT count(*) FROM control.customer_identity_transfer') == [(0,)]
    else:
        assert [(row['batch_id'], row['source_system_id']) for row in extract(db)[1]] == [(newer, 2)]
        db.rollback()
        base.finish(db, run)
        assert q(db, 'SELECT row_count FROM control.customer_identity_transfer WHERE backfill_run_id=%s',
                 (run,)) == [(1,)]


def test_source_transition_retains_candidate_bearing_fail_closed(db):
    install(db)
    older = batch(db, source=1)
    base.upload(db, older, user='X', name='old', source=1)
    newer = batch(db, source=2, created='2026-09-02T00:00:00Z')
    base.upload(db, newer, user='X', name='new', source=2)
    # Two different logical users share a blocker across the streaming pages.
    for user in ('Y', 'Z'):
        q(db, '''INSERT INTO raw.customer_current(batch_id,source_system_id,
            source_customer_id,email) VALUES(%s,2,%s,'shared@example.com')''', (newer, user))
    db.commit()
    before = snapshot(db)
    with pytest.raises(backfill.BackfillError, match='SPARSE_APP_HAS_CANDIDATE_PAIRS'):
        backfill.run_backfill(configuration(db), APP, str(uuid4()),
            cutoff=datetime(2026, 9, 19, tzinfo=timezone.utc), max_records=1)
    assert q(db, 'SELECT count(*) FROM control.customer_app_backfill_run') == [(0,)]
    assert q(db, 'SELECT count(*) FROM control.customer_identity_transfer') == [(0,)]
    assert snapshot(db) == before


def test_prior_028_rejects_cross_source_app_user_without_mutating_run(db):
    older = batch(db, source=1)
    base.upload(db, older, user='X', source=1)
    newer = batch(db, source=2, created='2026-09-02T00:00:00Z')
    current = base.upload(db, newer, user='X', source=2)
    run, workspace = base.prepare(db, [older, newer], app=APP)
    base.record(db, run, workspace, newer, current, user='X', source=2, app=APP)
    db.commit()
    assert q(db, 'SELECT md5(prosrc) FROM pg_proc WHERE oid=%s::regprocedure', (FINALIZER,)) == [(PRIOR_MD5,)]
    with pytest.raises(psycopg2.Error, match='BACKFILL_LATEST_INPUT_INCOMPLETE'):
        base.finish(db, run)
    db.rollback()
    assert q(db, 'SELECT status FROM control.customer_app_backfill_run WHERE run_id=%s', (run,)) == [('BUILDING',)]
    assert q(db, 'SELECT count(*) FROM control.customer_identity_transfer') == [(0,)]


def test_staged_run_prepared_under_028_can_finalize_under_039(db):
    older = batch(db, source=1)
    base.upload(db, older, user='X', source=1)
    newer = batch(db, source=2, created='2026-09-02T00:00:00Z')
    latest = base.upload(db, newer, user='X', source=2)
    run, workspace = base.prepare(db, [older, newer], app=APP)
    base.record(db, run, workspace, newer, latest, user='X', source=2, app=APP)
    db.commit()
    before = snapshot(db)
    assert q(db, 'SELECT status FROM control.customer_app_backfill_run WHERE run_id=%s', (run,)) == [('BUILDING',)]
    install(db)
    base.finish(db, run)
    db.commit()
    assert q(db, 'SELECT status,expected_users FROM control.customer_app_backfill_run WHERE run_id=%s',
             (run,)) == [('SUCCEEDED', 1)]
    assert q(db, 'SELECT status,source_record_count FROM identity.resolution_run WHERE run_id=%s',
             (run,)) == [('SUCCEEDED', 1)]
    assert q(db, 'SELECT status,row_count FROM control.customer_identity_transfer WHERE backfill_run_id=%s',
             (run,)) == [('PENDING', 1)]
    assert snapshot(db) == before


def test_all_batch_coverage_cannot_silently_shrink_after_prepare(db):
    install(db)
    older = batch(db, source=1)
    base.upload(db, older, user='X', source=1)
    newer = batch(db, source=2, created='2026-09-02T00:00:00Z')
    current = base.upload(db, newer, user='X', source=2)
    run, workspace = base.prepare(db, [older, newer], app=APP)
    base.record(db, run, workspace, newer, current, user='X', source=2, app=APP)
    db.commit()
    before = snapshot(db)
    # Source activity after the frozen manifest must fail, not publish one user.
    base.upload(db, older, user='Y', source=1)
    db.commit()
    with pytest.raises(psycopg2.Error, match='BACKFILL_COVERAGE_CHANGED'):
        base.finish(db, run)
    db.rollback()
    assert q(db, 'SELECT status FROM control.customer_app_backfill_run WHERE run_id=%s', (run,)) == [('BUILDING',)]
    assert q(db, 'SELECT count(*) FROM control.customer_identity_transfer') == [(0,)]
    assert q(db, 'SELECT expected_members,available_observations FROM control.customer_app_backfill_batch '
             'WHERE run_id=%s ORDER BY batch_created_at', (run,)) == [(1, 1), (1, 1)]
    assert snapshot(db) != before  # only the new source input changed, never ledger.


def test_auto_sparse_runtime_keeps_logical_winner_and_input_digest_across_pages(db):
    install(db)
    older = batch(db, source=1)
    base.upload(db, older, user='X', name='obsolete', source=1)
    newer = batch(db, source=2, created='2026-09-02T00:00:00Z')
    latest = base.upload(db, newer, user='X', name='required', source=2)
    extra = base.upload(db, newer, user='Y', name='independent', source=2)
    db.commit()
    before = snapshot(db)
    manifest, rows = extract(db)
    assert len(rows) == 2
    expected_digest = backfill.input_digest(rows)
    db.rollback()
    run = str(uuid4())
    report = backfill.run_backfill(configuration(db), APP, run,
        cutoff=datetime(2026, 9, 19, tzinfo=timezone.utc),
        max_records=1, expected_batches=[older, newer], expected_users=2)
    assert (report['status'], report['input_sha256'], report['row_count']) == (
        'SUCCEEDED', expected_digest, 2)
    assert q(db, '''SELECT row_number,user_id,source_system_id,raw_customer_id::text
        FROM control.customer_app_backfill_record WHERE run_id=%s ORDER BY row_number''',
        (run,)) == [(1, 'X', 2, latest), (2, 'Y', 2, extra)]
    assert q(db, '''SELECT row_number,user_id FROM control.customer_identity_export
        WHERE transfer_id=%s ORDER BY row_number''', (run,)) == [(1, 'X'), (2, 'Y')]
    assert q(db, '''SELECT sum(expected_members),sum(available_observations)
        FROM control.customer_app_backfill_batch WHERE run_id=%s''', (run,)) == [(3, 3)]
    assert snapshot(db) == before


@pytest.mark.parametrize('case', [
    'success', 'pairs', 'deterministic', 'bad_pair', 'runtime_acl', 'stale', 'failed',
    'connector', 'manifest', 'payload', 'older_missing', 'latest_missing',
])
def test_028_unmodified_guards_and_success_paths_still_apply_to_039(db, monkeypatch, case):
    # Invoke existing 027/028 PG scenarios against 039 without editing old files
    # or redirecting their hard-coded 61577 fixture to a different database.
    monkeypatch.setattr(base, 'install', install)
    if case == 'success':
        base.test_historical_success_is_atomic_owned_resolution_and_export_without_current_writes(db)
    elif case == 'pairs':
        base.test_pair_evidence_is_run_local_and_never_exported(db)
    elif case == 'deterministic':
        base.test_deterministic_accepted_pairs_can_exceed_probabilistic_candidates(db)
    elif case == 'bad_pair':
        base.test_accepted_pair_cannot_cross_exported_entities(db)
    elif case == 'runtime_acl':
        base.test_private_default_acls_and_narrow_runtime_can_finalize(db)
    elif case == 'stale':
        base.test_any_newer_same_app_outbox_invalidates_prepared_inputs(db, 'PENDING')
    elif case == 'failed':
        base.test_failed_ledger_marks_only_its_resolution_failed(db)
    elif case == 'connector':
        base.test_connector_lineage_cannot_be_included_as_file_history(db)
    elif case == 'manifest':
        base.test_exact_manifest_and_source_tuple_cannot_be_narrowed(db)
    elif case == 'payload':
        base.test_invalid_payload_fails_without_success_or_outbox(db, 'source_system_id', 2)
    elif case == 'older_missing':
        base.test_membership_winners_not_available_history_define_completeness(db, False)
    else:
        base.test_membership_winners_not_available_history_define_completeness(db, True)


@pytest.mark.parametrize('case', ['missing_record', 'source_batch_id', 'raw_customer_id',
                                   'source_system_id', 'user_id', 'left_record_id', 'right_record_id'])
def test_028_indexed_tuple_and_pair_guards_still_apply_to_039(db, monkeypatch, case):
    monkeypatch.setattr(base, 'install', install)
    if case in ('left_record_id', 'right_record_id'):
        access.test_pair_endpoint_must_exist_in_same_run_with_atomic_rollback(db, case)
    else:
        access.test_exact_winner_tuple_is_required_with_atomic_rollback(db, case)


def test_empty_selected_app_with_completed_empty_batch_can_finalize(db):
    install(db)
    only = batch(db, source=2)
    run, workspace = base.prepare(db, [only], app=APP, expected=0)
    assert workspace
    base.finish(db, run)
    assert q(db, 'SELECT row_count FROM control.customer_identity_transfer WHERE backfill_run_id=%s',
             (run,)) == [(0,)]
    assert q(db, 'SELECT count(*) FROM control.customer_identity_export WHERE transfer_id=%s', (run,)) == [(0,)]


@pytest.mark.parametrize('case', ['live_capture', 'immutable_ledger', 'report',
                                   'export_rollback', 'running_batch'])
def test_027_other_guards_still_hold_after_039(db, monkeypatch, case):
    monkeypatch.setattr(base, 'install', install)
    if case == 'live_capture':
        base.test_027_keeps_existing_views_and_live_capture_working(db)
    elif case == 'immutable_ledger':
        base.test_persisted_ledger_cannot_update_delete_or_truncate(db, 'batch')
    elif case == 'report':
        base.test_incomplete_or_pretend_resolution_cannot_finalize(
            db, 'UPDATE identity.resolution_run SET entity_count=0', 'INCOMPLETE_REPORT')
    elif case == 'export_rollback':
        base.test_export_failure_rolls_back_resolution_and_outbox_together(db)
    else:
        base.test_running_ingestion_is_not_part_of_fixed_completed_manifest(db)


def test_039_concurrent_finalizers_keep_stale_publication_gate(db, owned_database, monkeypatch):
    monkeypatch.setattr(base, 'install', install)
    monkeypatch.setattr(base, 'ENDPOINT', ENDPOINT)
    base.test_concurrent_historical_finalizers_serialize_and_reject_stale_loser(
        db, owned_database)


def test_sql_winner_and_pair_validation_still_use_028_indexed_access_paths(db):
    install(db)
    older = batch(db, source=1)
    base.upload(db, older, user='X', source=1)
    latest = batch(db, source=2, created='2026-09-02T00:00:00Z')
    raw = base.upload(db, latest, user='X', source=2)
    run, workspace = base.prepare(db, [older, latest], app=APP)
    base.record(db, run, workspace, latest, raw, user='X', source=2, app=APP)
    installed = q(db, 'SELECT md5(prosrc) FROM pg_proc WHERE oid=%s::regprocedure', (FINALIZER,))[0][0]
    assert installed == 'e286571338e2bcc0369d462a7aa27dfc'
    plans = access.capture_stale_plans(db, run, 1, installed)
    assert set(plans) == {'winners', 'pairs'}
