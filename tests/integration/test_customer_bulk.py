"""Isolated, real PostgreSQL regression for the coordinated bulk contract.

CDP_BULK_TEST_DSN selects local PostgreSQL at 127.0.0.1:55441 only.
Each test creates/drops a fresh guarded cdp_bulk_review_<uuid> database.
"""
import importlib.util
import os
from contextlib import closing
from pathlib import Path
import shutil
import uuid

import psycopg2
from psycopg2 import sql
from psycopg2.extensions import make_dsn, parse_dsn
import pytest

ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / '_lambda/ingestion_data/customer_bulk.py'
MIGRATION = ROOT / 'database/migrations/022_customer_bulk_ingestion.sql'
DSN = os.getenv('CDP_BULK_TEST_DSN', 'host=127.0.0.1 port=55441 user=postgres dbname=cdp_bulk_regression')
OWNED_DATABASE = None


@pytest.fixture(autouse=True)
def isolated_database():
    global DSN, OWNED_DATABASE
    original_dsn = DSN
    params = parse_dsn(original_dsn)
    assert params.get('host') == '127.0.0.1'
    assert params.get('port') == '55441'
    assert params.get('dbname') in ('postgres', 'cdp_bulk_regression')
    assert params.get('hostaddr', '127.0.0.1') == '127.0.0.1'
    assert not params.get('service'), 'service files cannot override the local guard'
    assert shutil.disk_usage(ROOT).free >= 512 * 1024 * 1024, 'fixture disk reserve'
    database = 'cdp_bulk_review_' + uuid.uuid4().hex
    admin = psycopg2.connect(make_dsn(original_dsn, dbname='postgres'), connect_timeout=3)
    admin.autocommit = True
    created = False
    try:
        with admin.cursor() as q:
            q.execute('SELECT 1 FROM pg_database WHERE datname=%s', (database,))
            assert q.fetchone() is None, 'refuse to reset a preexisting database'
            q.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0 ENCODING 'UTF8'").format(
                sql.Identifier(database)))
            created = True
        OWNED_DATABASE = database
        DSN = make_dsn(original_dsn, dbname=database, client_encoding='UTF8')
        probe = connect()
        try:
            with probe.cursor() as q:
                q.execute('SELECT current_database()')
                assert q.fetchone() == (database,)
                q.execute('SHOW server_encoding')
                assert q.fetchone() == ('UTF8',)
        finally:
            probe.close()
        yield
    finally:
        DSN, OWNED_DATABASE = original_dsn, None
        try:
            if created:
                with admin.cursor() as q:
                    q.execute(sql.SQL('DROP DATABASE {}').format(sql.Identifier(database)))
                    q.execute('SELECT 1 FROM pg_database WHERE datname=%s', (database,))
                    assert q.fetchone() is None
        finally:
            admin.close()


def connect():
    params = parse_dsn(DSN)
    assert params.get('host') == '127.0.0.1'
    assert params.get('port') == '55441'
    assert OWNED_DATABASE is not None and params.get('dbname') == OWNED_DATABASE
    return psycopg2.connect(DSN, connect_timeout=3)


def bulk_module():
    assert MODULE.exists(), 'COPY bulk writer has not been implemented'
    spec = importlib.util.spec_from_file_location('customer_bulk_test_target', MODULE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def reset_schema(q, *, optimized=True):
    q.execute((ROOT / 'tests/integration/fixtures/customer_bulk_live_base.sql').read_text())
    q.execute((ROOT / 'database/migrations/019_customer_typed_history.sql').read_text())
    q.execute((ROOT / 'database/migrations/020_customer_history_recovery.sql').read_text())
    if optimized:
        assert MIGRATION.exists(), 'Set-based integrity migration has not been implemented'
        q.execute(MIGRATION.read_text())
    q.execute('BEGIN')


@pytest.fixture
def db():
    conn = connect()
    try:
        with conn.cursor() as q:
            reset_schema(q)
        conn.commit()
        yield conn
    finally:
        conn.rollback()
        conn.close()


def batch(q, day, source=1):
    value = str(uuid.uuid4())
    q.execute('INSERT INTO control.ingestion_batch(batch_id,source_system_id,created_at) VALUES (%s,%s,%s)',
              (value, source, '2025-01-%02dT00:00:00Z' % day))
    return value


def customer(key='key', **changes):
    result = dict(source_customer_id=key, first_name='สมชาย', email=f'{key}@example.com',
                  date_of_birth='1990-02-03', age=35, status='ACTIVE',
                  external_ids={'crm': key, 'loyalty': 'member-' + key},
                  attributes={'tier': 'gold', 'consent': True},
                  raw_payload={'id': key, 'name': 'สมชาย', 'null': None},
                  source_created_at='2020-01-02T03:04:05+07:00')
    result.update(changes)
    return result


def counts(q):
    q.execute('SELECT (SELECT count(*) FROM raw.customer_current),'
              '(SELECT count(*) FROM raw.customer),'
              '(SELECT count(*) FROM raw.customer_batch_member)')
    return q.fetchone()


def test_copy_writer_commits_typed_history_current_and_membership():
    writer = bulk_module()
    conn = connect()
    try:
        with conn.cursor() as q:
            reset_schema(q)
            first = batch(q, 1)
            writer.upsert_customers(q, iter([customer()]), first, 1)
        conn.commit()
        with conn.cursor() as q:
            assert counts(q) == (1, 1, 1)
            q.execute('SELECT c.raw_customer_id=h.raw_customer_id, c.date_of_birth::text, '
                      'h.attributes, m.raw_customer_id=c.raw_customer_id '
                      'FROM raw.customer_current c JOIN raw.customer h USING(raw_customer_id) '
                      'JOIN raw.customer_batch_member m USING(raw_customer_id)')
            assert q.fetchone() == (True, '1990-02-03', {'tier': 'gold', 'consent': True}, True)
    finally:
        conn.close()


def test_preexisting_membership_mismatch_rejects_atomically(db):
    writer = bulk_module()
    with db.cursor() as q:
        first = batch(q, 1)
        writer.upsert_customers(q, [customer('other')], first, 1)
    db.commit()
    with db.cursor() as q:
        second = batch(q, 2)
        q.execute('INSERT INTO raw.customer_batch_member '
                  'SELECT %s,raw_customer_id,source_system_id,%s,now() FROM raw.customer_current',
                  (second, 'key'))
    db.commit()
    with pytest.raises(psycopg2.Error, match='membership'):
        with db.cursor() as q:
            writer.upsert_customers(q, [customer('key')], second, 1)
    db.rollback()
    with db.cursor() as q:
        assert counts(q) == (1, 1, 2)


def test_bulk_preserves_non_ingestion_customer_id(db):
    writer = bulk_module()
    with db.cursor() as q:
        first, second = batch(q, 1), batch(q, 2)
        q.execute("INSERT INTO raw.customer_current(batch_id,source_system_id,source_customer_id,customer_id) "
                  "VALUES (%s,1,'key','legacy-customer-id')", (first,))
        writer.upsert_customers(q, [customer()], second, 1)
        q.execute('SELECT customer_id FROM raw.customer_current')
        assert q.fetchone() == ('legacy-customer-id',)
        q.execute('SELECT customer_id FROM raw.customer WHERE batch_id=%s', (second,))
        assert q.fetchone() == ('legacy-customer-id',)


@pytest.mark.parametrize('late_first', [False, True])
def test_a_b_exact_replay_never_rewinds_b(db, late_first):
    writer = bulk_module()
    with db.cursor() as q:
        a, b = batch(q, 1), batch(q, 2)
        if not late_first:
            writer.upsert_customers(q, [customer(email='a@example.com')], a, 1)
        writer.upsert_customers(q, [customer(email='b@example.com')], b, 1)
        q.execute('SELECT raw_customer_id FROM raw.customer_current')
        raw_id = q.fetchone()[0]
        writer.upsert_customers(q, [customer(email='a@example.com')], a, 1)
        q.execute('SELECT batch_id::text,email,raw_customer_id FROM raw.customer_current')
        assert q.fetchone() == (b, 'b@example.com', raw_id)
        q.execute('SELECT batch_id::text,email FROM raw.customer')
        assert dict(q.fetchall()) == {a: 'a@example.com', b: 'b@example.com'}
        q.execute('SELECT snapshot_id,snapshot_recorded_at FROM raw.customer ORDER BY batch_id')
        before = q.fetchall()
        writer.upsert_customers(q, [customer(email='a@example.com')], a, 1)
        writer.upsert_customers(q, [customer(email='b@example.com')], b, 1)
        q.execute('SELECT snapshot_id,snapshot_recorded_at FROM raw.customer ORDER BY batch_id')
        assert q.fetchall() == before
        assert counts(q) == (1, 2, 2)


@pytest.mark.parametrize('changed', [dict(email='different@example.com'), dict(age=99),
    dict(attributes={'tier': None}), dict(raw_payload=None), dict(date_of_birth=None)])
def test_same_batch_typed_mismatch_rolls_back_new_keys(db, changed):
    writer = bulk_module()
    with db.cursor() as q:
        a = batch(q, 1)
        writer.upsert_customers(q, [customer()], a, 1)
    db.commit()
    with pytest.raises(psycopg2.Error, match='snapshot cannot change'):
        with db.cursor() as q:
            writer.upsert_customers(q, [customer('new'), customer(**changed)], a, 1)
    db.rollback()
    with db.cursor() as q:
        assert counts(q) == (1, 1, 1)
        q.execute('SELECT email FROM raw.customer_current')
        assert q.fetchone() == ('key@example.com',)


def test_typed_equality_timezone_json_order_and_copy_escaping(db):
    writer = bulk_module()
    text = '\\N\t\n\r\\雪'
    with db.cursor() as q:
        a = batch(q, 1)
        writer.upsert_customers(q, [customer(first_name=text)], a, 1)
        writer.upsert_customers(q, [customer(first_name=text,
            source_created_at='2020-01-01T20:04:05Z',
            attributes={'consent': True, 'tier': 'gold'})], a, 1)
        q.execute('SELECT first_name FROM raw.customer')
        assert q.fetchone() == (text,)
        assert counts(q) == (1, 1, 1)


def test_partial_replay_plus_new_overlap_batch(db):
    writer = bulk_module()
    with db.cursor() as q:
        a, b = batch(q, 1), batch(q, 2)
        writer.upsert_customers(q, [customer('a'), customer('b')], a, 1)
        writer.upsert_customers(q, [customer('b'), customer('c')], a, 1)
        writer.upsert_customers(q, [customer('b', age=36), customer('d')], b, 1)
        assert counts(q) == (4, 5, 5)
        q.execute("SELECT array_agg(age ORDER BY age) FROM raw.customer WHERE source_customer_id='b'")
        assert q.fetchone() == ([35, 36],)


def test_transaction_rollback_after_all_bulk_writes(db):
    writer = bulk_module()
    with db.cursor() as q:
        a = batch(q, 1)
        writer.upsert_customers(q, [customer()], a, 1)
        assert counts(q) == (1, 1, 1)
    db.rollback()
    with db.cursor() as q:
        assert counts(q) == (0, 0, 0)


@pytest.mark.parametrize('values', [[customer(), customer()], [customer(age=-1)], [customer(status='BAD')]])
def test_copy_stage_rejects_duplicate_or_invalid_rows(db, values):
    writer = bulk_module()
    with pytest.raises(psycopg2.Error):
        with db.cursor() as q:
            a = batch(q, 1)
            writer.upsert_customers(q, iter(values), a, 1)
    db.rollback()
    with db.cursor() as q:
        assert counts(q) == (0, 0, 0)


def test_same_source_concurrent_batches_serialize_and_keep_late_history(db):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    import time
    writer = bulk_module()
    with db.cursor() as q:
        a, b = batch(q, 1), batch(q, 2)
    db.commit()
    with db.cursor() as q:
        writer.upsert_customers(q, [customer(email='b@example.com')], b, 1)
    entered = threading.Event()
    def late():
        with closing(connect()) as other, other, other.cursor() as q:
            q.execute("SET statement_timeout='10s'")
            entered.set()
            writer.upsert_customers(q, [customer(email='a@example.com')], a, 1)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(late)
        assert entered.wait(3)
        with closing(connect()) as observer, observer, observer.cursor() as q:
            for _ in range(100):
                q.execute("SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() "
                          "AND wait_event='advisory'")
                if q.fetchone()[0]:
                    break
                time.sleep(0.02)
            else:
                pytest.fail('competing writer never reached source advisory lock')
        assert not future.done()
        db.commit()
        future.result(timeout=10)
    with db.cursor() as q:
        assert counts(q) == (1, 2, 2)
        q.execute('SELECT batch_id::text,email FROM raw.customer_current')
        assert q.fetchone() == (b, 'b@example.com')


def test_direct_legacy_writer_forward_safe_late_fails_closed(db):
    with db.cursor() as q:
        a, b = batch(q, 1), batch(q, 2)
        q.execute("INSERT INTO raw.customer_current(batch_id,source_system_id,source_customer_id) VALUES (%s,1,'key')", (b,))
    db.commit()
    with pytest.raises(psycopg2.Error, match='cannot change or rewind'):
        with db.cursor() as q:
            q.execute("SET LOCAL cdp.customer_bulk='on'")
            q.execute('UPDATE raw.customer_current SET batch_id=%s', (a,))
    db.rollback()
    with db.cursor() as q:
        assert counts(q) == (1, 1, 1)
        q.execute('SELECT batch_id::text FROM raw.customer_current')
        assert q.fetchone() == (b,)


def test_merge_emits_separate_database_stage_timings(db):
    with db.cursor() as q:
        a = batch(q, 1)
        bulk_module().upsert_customers(q, [customer()], a, 1)
    assert any('customer_bulk_sql' in n and 'history' in n and 'membership' in n for n in db.notices)


def test_typed_comparison_plan_avoids_quadratic_replay_scan(db):
    import re
    writer=bulk_module()
    with db.cursor() as q:
        a=batch(q,1)
        writer.upsert_customers(q,(customer(str(i)) for i in range(2000)),a,1)
        q.execute("SELECT pg_get_functiondef('raw.apply_customer_bulk(uuid,bigint)'::regprocedure)")
        definition=q.fetchone()[0]
        match=re.search(r'IF (EXISTS \(\s*SELECT FROM pg_temp.customer_bulk_stage s JOIN.*?ROW\(.*?\))\s*THEN',definition,re.S)
        if match:
            expression=match.group(1)
        else:
            match=re.search(r'IF (\(SELECT count\(\*\).*?FROM pg_temp.customer_bulk_stage s JOIN.*?ROW\(.*?\) > 0)\s*THEN',definition,re.S)
            assert match, 'cannot extract exact runtime comparison expression'
            expression=match.group(1)
        q.execute("SELECT proconfig FROM pg_proc WHERE oid='raw.apply_customer_bulk(uuid,bigint)'::regprocedure")
        settings=q.fetchone()[0] or []
        assert 'enable_nestloop=off' in settings, 'bulk scope must avoid quadratic fresh-batch nested loops'
        q.execute('SET LOCAL enable_nestloop=off')
        expression=expression.replace('p_source','1').replace('p_batch',"'"+a+"'::uuid")
        q.execute('EXPLAIN (ANALYZE,FORMAT JSON) SELECT '+expression)
        plan=q.fetchone()[0][0]['Plan']
        def discarded(node):
            return node.get('Rows Removed by Join Filter',0)*node.get('Actual Loops',1)+sum(discarded(n) for n in node.get('Plans',[]))
        assert discarded(plan)<20000, 'typed retry comparator used a quadratic materialized nested-loop scan'


def test_statement_writer_rejects_contradictory_membership(db):
    with db.cursor() as q:
        a, b=batch(q,1),batch(q,2)
        q.execute("INSERT INTO raw.customer_current(batch_id,source_system_id,source_customer_id) VALUES (%s,1,'other')",(a,))
        q.execute("INSERT INTO raw.customer_batch_member SELECT %s,raw_customer_id,1,'key',now() FROM raw.customer_current",(b,))
    db.commit()
    with pytest.raises(psycopg2.Error,match='membership'):
        with db.cursor() as q:
            q.execute("INSERT INTO raw.customer_current(batch_id,source_system_id,source_customer_id) VALUES (%s,1,'key')",(b,))
    db.rollback()
    with db.cursor() as q:
        assert counts(q)==(1,1,2)


def test_late_replay_with_changed_values_rejects_after_b(db):
    writer=bulk_module()
    with db.cursor() as q:
        a,b=batch(q,1),batch(q,2)
        writer.upsert_customers(q,[customer(email='a@example.com')],a,1)
        writer.upsert_customers(q,[customer(email='b@example.com')],b,1)
    db.commit()
    with pytest.raises(psycopg2.Error,match='snapshot cannot change'):
        with db.cursor() as q:
            writer.upsert_customers(q,[customer(email='changed@example.com')],a,1)
    db.rollback()
    with db.cursor() as q:
        assert counts(q)==(1,2,2)
        q.execute('SELECT email FROM raw.customer_current')
        assert q.fetchone()==('b@example.com',)


def test_different_sources_preserve_identical_key_names(db):
    with db.cursor() as q:
        a,b=batch(q,1,source=1),batch(q,2,source=2)
        bulk_module().upsert_customers(q,[customer()],a,1)
        bulk_module().upsert_customers(q,[customer()],b,2)
        assert counts(q)==(2,2,2)


def test_function_planner_settings_restore_to_caller(db):
    with db.cursor() as q:
        q.execute('SET LOCAL enable_nestloop=on')
        a=batch(q,1)
        bulk_module().upsert_customers(q,[customer()],a,1)
        q.execute('SHOW enable_nestloop')
        assert q.fetchone()==('on',)


def test_bulk_statistics_sampling_is_function_scoped_and_restored(db):
    # Aurora 150k evidence: default target 100 spent ~60s sampling a wide temp
    # table; target 10 took ~5-7s. Never change the caller/global statistics.
    with db.cursor() as q:
        q.execute('SET LOCAL default_statistics_target=200')
        q.execute("SELECT proconfig FROM pg_proc WHERE oid='raw.apply_customer_bulk(uuid,bigint)'::regprocedure")
        assert 'default_statistics_target=10' in (q.fetchone()[0] or [])
        a = batch(q, 1)
        bulk_module().upsert_customers(q, [customer()], a, 1)
        q.execute('SHOW default_statistics_target')
        assert q.fetchone() == ('200',)
        q.execute('SAVEPOINT rejected_replay')
        with pytest.raises(psycopg2.Error, match='snapshot cannot change'):
            bulk_module().upsert_customers(q, [customer(email='changed@example.com')], a, 1)
        q.execute('ROLLBACK TO SAVEPOINT rejected_replay')
        q.execute('SHOW default_statistics_target')
        assert q.fetchone() == ('200',)
        assert counts(q) == (1, 1, 1)


def test_batch_source_mismatch_rejects_before_mutation(db):
    with db.cursor() as q:
        a=batch(q,1,source=2)
    db.commit()
    with pytest.raises(psycopg2.Error,match='lineage'):
        with db.cursor() as q:
            bulk_module().upsert_customers(q,[customer()],a,1)
    db.rollback()
    with db.cursor() as q:
        assert counts(q)==(0,0,0)


def test_concurrent_same_batch_mismatch_does_not_commit_second_value(db):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    writer=bulk_module()
    with db.cursor() as q:
        a=batch(q,1)
    db.commit()
    with db.cursor() as q:
        writer.upsert_customers(q,[customer()],a,1)
    entered=threading.Event()
    def second_writer():
        with closing(connect()) as other, other, other.cursor() as q:
            q.execute("SET statement_timeout='10s'")
            entered.set()
            writer.upsert_customers(q,[customer(email='different@example.com')],a,1)
    with ThreadPoolExecutor(max_workers=1) as pool:
        f=pool.submit(second_writer)
        assert entered.wait(3)
        db.commit()
        with pytest.raises(psycopg2.Error,match='snapshot cannot change'):
            f.result(timeout=10)
    with db.cursor() as q:
        assert counts(q)==(1,1,1)
        q.execute('SELECT email FROM raw.customer')
        assert q.fetchone()==('key@example.com',)


def test_invoker_role_has_no_integrity_bypass(db):
    # Role creation rolls back; no cluster-wide privilege changes persist.
    with db.cursor() as q:
        q.execute('CREATE ROLE cdp_bulk_test_invoker NOLOGIN NOSUPERUSER')
        q.execute('GRANT cdp_bulk_test_invoker TO CURRENT_USER')
        q.execute('GRANT USAGE ON SCHEMA raw,control TO cdp_bulk_test_invoker')
        q.execute('GRANT SELECT,INSERT,UPDATE ON raw.customer_current TO cdp_bulk_test_invoker')
        q.execute('GRANT SELECT,INSERT ON raw.customer,raw.customer_batch_member TO cdp_bulk_test_invoker')
        q.execute('GRANT SELECT ON control.ingestion_batch TO cdp_bulk_test_invoker')
        a=batch(q,1)
        q.execute('SET LOCAL ROLE cdp_bulk_test_invoker')
        bulk_module().upsert_customers(q,[customer()],a,1)
        assert counts(q)==(1,1,1)
        q.execute('SAVEPOINT hostile')
        with pytest.raises(psycopg2.Error):
            q.execute('SET LOCAL session_replication_role=replica')
        q.execute('ROLLBACK TO SAVEPOINT hostile')
        q.execute("SET LOCAL cdp.customer_bulk='on'")
        with pytest.raises(psycopg2.Error,match='snapshot cannot change'):
            q.execute("UPDATE raw.customer_current SET email='bad@example.com'")
    db.rollback()
    with db.cursor() as q:
        assert counts(q)==(0,0,0)
