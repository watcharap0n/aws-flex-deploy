"""Isolated local PostgreSQL acceptance for opt-in app source namespace (no AWS)."""
import importlib.util
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from pathlib import Path
from uuid import uuid4

import psycopg2
from psycopg2 import sql
import pytest

ROOT = Path(__file__).resolve().parents[2]
PORT = 55441
MIGRATION = ROOT / 'database/migrations/037_customer_app_source_namespace.sql'



def run(q, statement, args=None):
    q.execute(statement, args)
    return q.fetchall() if q.description else []


def writer():
    spec = importlib.util.spec_from_file_location('namespace_writer', ROOT / '_lambda/ingestion_data/customer_bulk.py')
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.upsert_customers


def ingestion():
    spec = importlib.util.spec_from_file_location('namespace_ingestion', ROOT / '_lambda/ingestion_data/lambda_function.py')
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def db():
    dbname = 'cdp_namespace_review_' + uuid4().hex
    admin = psycopg2.connect(host='127.0.0.1', port=PORT, user='postgres', dbname='postgres', connect_timeout=3)
    admin.autocommit = True
    conn = None
    try:
        with admin.cursor() as q:
            assert run(q, 'SELECT pg_is_in_recovery()') == [(False,)]
            assert run(q, 'SELECT rolcreatedb OR rolsuper FROM pg_roles WHERE rolname=current_user') == [(True,)]
            assert run(q, 'SELECT 1 FROM pg_database WHERE datname=%s', (dbname,)) == []
            run(q, sql.SQL('CREATE DATABASE {} TEMPLATE template0 ENCODING %s').format(sql.Identifier(dbname)), ('UTF8',))
        conn = psycopg2.connect(host='127.0.0.1', port=PORT, user='postgres', dbname=dbname, connect_timeout=3)
        assert (conn.info.host, conn.info.port, conn.info.dbname) == ('127.0.0.1', PORT, dbname)
        conn.autocommit = True
        with conn.cursor() as q:
            run(q, (ROOT / 'tests/integration/fixtures/customer_history_base.sql').read_text())
            run(q, '''ALTER TABLE control.ingestion_batch ADD COLUMN batch_ref text;
                ALTER TABLE control.source_landing_batch ADD COLUMN batch_ref text, ADD COLUMN source_code text;
                CREATE TABLE control.source_system(source_system_id bigint PRIMARY KEY, source_code text);
                INSERT INTO control.source_system VALUES (1,'fixture-pos');
                CREATE TABLE control.stitching_job(job_id uuid PRIMARY KEY, ingestion_batch_id uuid);
                CREATE SCHEMA connector;
                CREATE TABLE connector.sync_run(ingestion_batch_id uuid, landing_batch_id uuid, stitching_job_id uuid);
                CREATE SCHEMA identity;
                CREATE TABLE identity.customer_entity(customer_entity_id uuid PRIMARY KEY);
                CREATE TABLE identity.customer_entity_member (
                    standardized_record_id uuid PRIMARY KEY REFERENCES staging.customer_profile,
                    customer_entity_id uuid NOT NULL REFERENCES identity.customer_entity,
                    source_system_id bigint NOT NULL, source_customer_id text NOT NULL,
                    UNIQUE(source_system_id,source_customer_id));
                CREATE FUNCTION raw.set_customer_updated_at() RETURNS trigger LANGUAGE plpgsql AS $$
                BEGIN NEW.updated_at=now(); RETURN NEW; END $$;
                CREATE TRIGGER trg_raw_customer_updated_at BEFORE UPDATE ON raw.customer
                FOR EACH ROW EXECUTE FUNCTION raw.set_customer_updated_at();''')
            for name in ('019_customer_typed_history.sql', '022_customer_bulk_ingestion.sql', '026_customer_app_scope.sql'):
                run(q, (ROOT / 'database/migrations' / name).read_text())
        conn.autocommit = False
        yield conn
    finally:
        if conn is not None:
            conn.close()
        with admin.cursor() as q:
            assert dbname.startswith('cdp_namespace_review_') and len(dbname) == len('cdp_namespace_review_') + 32
            run(q, sql.SQL('DROP DATABASE IF EXISTS {} WITH (FORCE)').format(sql.Identifier(dbname)))
        admin.close()


def batch(q, app, *, connector=False):
    landing, ingestion = str(uuid4()), str(uuid4())
    ref = '2026-09-27-120000-' + app + '-' + uuid4().hex[:12]
    run(q, 'INSERT INTO control.source_landing_batch(batch_id,batch_ref,source_code) VALUES (%s,%s,%s)',
        (landing, ref, 'fixture-pos'))
    run(q, '''INSERT INTO control.ingestion_batch(batch_id,landing_batch_id,source_system_id,batch_ref,metadata,created_at)
        VALUES (%s,%s,1,%s,%s,clock_timestamp())''',
        (ingestion, landing, ref, '{"connector_sync_run_id":"synthetic"}' if connector else '{}'))
    return ingestion


def install(q):
    # Explicit operator opt-in; migration itself also refuses unresolved/aliased evidence.
    run(q, "SET cdp.customer_source_namespace_opt_in = 'reviewed-file-upload-only'")
    run(q, MIGRATION.read_text())
    # The migration commits itself; start the runtime transaction explicitly.
    run(q, 'BEGIN')
    run(q, "SET LOCAL cdp.customer_source_namespace_writer = 'file-v1'")


def test_new_uploads_keep_distinct_raw_and_staged_identities_with_global_entity(db):
    upsert = writer()
    with db.cursor() as q:
        install(q)
        a, b = batch(q, '1282009980020330'), batch(q, '43')
        for batch_id, value in ((a, 'A'), (b, 'B')):
            upsert(q, [{'source_customer_id': 'synthetic-shared-key', 'first_name': value}], batch_id, 1)
        run(q, '''INSERT INTO staging.customer_profile(raw_customer_id,batch_id,source_system_id,source_customer_id)
            SELECT raw_customer_id,batch_id,source_system_id,source_customer_id FROM raw.customer_current''')
        entity = str(uuid4())
        run(q, 'INSERT INTO identity.customer_entity(customer_entity_id) VALUES (%s)', (entity,))
        run(q, '''INSERT INTO identity.customer_entity_member
            (standardized_record_id,customer_entity_id,source_system_id,source_customer_id)
            SELECT standardized_record_id,%s,source_system_id,source_customer_id FROM staging.customer_profile''', (entity,))
        before = run(q, '''SELECT batch_id,raw_customer_id,snapshot_id,first_name,app_id FROM raw.customer ORDER BY app_id''')
        assert len(before) == 2 and len({r[1] for r in before}) == 2
        assert {r[3] for r in before} == {'A', 'B'}
        assert len({r[2] for r in before}) == 2
        rows = run(q, '''SELECT c.app_id,c.raw_customer_id,c.first_name,h.app_id,p.app_id,m.app_id,m.customer_entity_id
            FROM raw.customer_current c JOIN raw.customer h USING (raw_customer_id)
            JOIN staging.customer_profile p USING(raw_customer_id)
            JOIN identity.customer_entity_member m USING(standardized_record_id)
            ORDER BY c.app_id''')
        assert len(rows) == 2 and all(r[0] == r[3] == r[4] == r[5] and r[6] == entity for r in rows)
        assert run(q, 'SELECT count(*) FROM raw.customer_batch_member') == [(2,)]
        upsert(q, [{'source_customer_id': 'synthetic-shared-key', 'first_name': 'A'}], a, 1)
        assert run(q, 'SELECT batch_id,raw_customer_id,snapshot_id,first_name,app_id FROM raw.customer ORDER BY app_id') == before
    db.commit()
    with db.cursor() as q:
        assert run(q, 'SELECT count(DISTINCT raw_customer_id) FROM raw.customer_current') == [(2,)]
        assert run(q, 'SELECT count(DISTINCT customer_entity_id) FROM identity.customer_entity_member') == [(1,)]


def test_existing_aliased_raw_id_refuses_migration_without_mutation(db):
    upsert = writer()
    with db.cursor() as q:
        a, b = batch(q, '1282009980020330'), batch(q, '43')
        upsert(q, [{'source_customer_id': 'synthetic-shared-key', 'first_name': 'A'}], a, 1)
        upsert(q, [{'source_customer_id': 'synthetic-shared-key', 'first_name': 'B'}], b, 1)
    db.commit()
    with pytest.raises(psycopg2.Error, match='historical raw ID alias'):
        with db.cursor() as q:
            install(q)
    db.rollback()
    with db.cursor() as q:
        assert run(q, 'SELECT count(DISTINCT raw_customer_id),count(*) FROM raw.customer') == [(1, 2)]
        assert run(q, "SELECT to_regclass('control.customer_source_namespace_activation')") == [(None,)]


def test_same_app_later_batch_keeps_id_and_immutable_snapshots(db):
    upsert = writer()
    with db.cursor() as q:
        install(q)
        a, b, c = batch(q, '43'), batch(q, '1282009980020330'), batch(q, '43')
        for batch_id, name in ((a, 'A'), (b, 'B'), (c, 'C')):
            upsert(q, [{'source_customer_id': 'synthetic-shared-key', 'first_name': name}], batch_id, 1)
        assert run(q, '''SELECT count(*),count(DISTINCT raw_customer_id),count(DISTINCT snapshot_id)
            FROM raw.customer''') == [(3, 2, 3)]
        assert run(q, '''SELECT count(DISTINCT raw_customer_id) FROM raw.customer
            WHERE app_id='43' ''') == [(1,)]
        assert run(q, '''SELECT first_name FROM raw.customer_current WHERE app_id='43' ''') == [('C',)]
        assert run(q, '''SELECT first_name FROM raw.customer WHERE batch_id=%s''', (a,)) == [('A',)]
        with pytest.raises(psycopg2.Error, match='snapshot cannot change'):
            run(q, 'SAVEPOINT mismatch')
            upsert(q, [{'source_customer_id': 'synthetic-shared-key', 'first_name': 'wrong'}], a, 1)
        run(q, 'ROLLBACK TO SAVEPOINT mismatch')
        assert run(q, 'SELECT count(*) FROM raw.customer') == [(3,)]
    db.commit()


def test_namespace_activation_mismatch_fails_closed_before_snapshot(db, monkeypatch):
    ingest = ingestion()
    with db.cursor() as q:
        install(q)
        file_batch = batch(q, '43')
        landing = run(q, 'SELECT landing_batch_id FROM control.ingestion_batch WHERE batch_id=%s', (file_batch,))[0][0]
        monkeypatch.delenv('CUSTOMER_SOURCE_NAMESPACE_ENABLED', raising=False)
        monkeypatch.setenv('CUSTOMER_BULK_ENABLED', 'true')
        with pytest.raises(RuntimeError, match='schema/writer activation mismatch'):
            ingest._create_existing_source_snapshot(q, landing_batch_id=str(landing),
                source_system_id=1, ingestion_batch_id=file_batch)
        assert run(q, "SELECT to_regclass('pg_temp.ingestion_existing_source_snapshot')") == [(None,)]
    db.rollback()


def test_namespace_activated_but_legacy_page_writer_refused(db, monkeypatch):
    ingest = ingestion()
    with db.cursor() as q:
        install(q)
        file_batch = batch(q, '43')
        landing = run(q, 'SELECT landing_batch_id FROM control.ingestion_batch WHERE batch_id=%s', (file_batch,))[0][0]
        monkeypatch.setenv('CUSTOMER_SOURCE_NAMESPACE_ENABLED', 'true')
        monkeypatch.delenv('CUSTOMER_BULK_ENABLED', raising=False)
        with pytest.raises(RuntimeError, match='requires a batch and bulk writer'):
            ingest._create_existing_source_snapshot(q, landing_batch_id=str(landing),
                source_system_id=1, ingestion_batch_id=file_batch)
    db.rollback()


def test_unactivated_direct_file_writer_is_rejected(db):
    with db.cursor() as q:
        install(q)
        file_batch = batch(q, '43')
        run(q, 'RESET cdp.customer_source_namespace_writer')
        run(q, 'SAVEPOINT direct_write')
        with pytest.raises(psycopg2.Error, match='file writer not activated'):
            run(q, '''INSERT INTO raw.customer_current(batch_id,source_system_id,source_customer_id)
                VALUES (%s,1,%s)''', (file_batch, 'synthetic-direct-key'))
        run(q, 'ROLLBACK TO SAVEPOINT direct_write')
        assert run(q, 'SELECT count(*) FROM raw.customer_current') == [(0,)]
    db.rollback()


def test_connector_file_overlap_concurrent_transactions_cannot_both_commit(db):
    upsert = writer()
    with db.cursor() as q:
        install(q)
        file_batch = batch(q, '43')
        connector_batch = batch(q, '43', connector=True)
    db.commit()

    def insert(batch_id, ready, proceed):
        with psycopg2.connect(host='127.0.0.1', port=PORT, user='postgres', dbname=db.info.dbname,
                              connect_timeout=3) as conn:
            with conn.cursor() as q:
                q.execute('SET LOCAL lock_timeout=%s', ('4s',))
                q.execute("SET LOCAL cdp.customer_source_namespace_writer='file-v1'")
                upsert(q, [{'source_customer_id': 'concurrent-shared-key', 'first_name': 'test'}], batch_id, 1)
                ready.set()
                if not proceed.wait(8):
                    raise RuntimeError('concurrent fixture barrier timed out')
        return True

    from threading import Event
    first_ready, release_first = Event(), Event()
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(insert, file_batch, first_ready, release_first)
        assert first_ready.wait(8)
        def competing_insert():
            with psycopg2.connect(host='127.0.0.1', port=PORT, user='postgres', dbname=db.info.dbname,
                                  connect_timeout=3) as probe:
                with probe.cursor() as q:
                    q.execute('SET LOCAL lock_timeout=%s', ('8s',))
                    q.execute('SET LOCAL statement_timeout=%s', ('8s',))
                    q.execute("SET LOCAL cdp.customer_source_namespace_writer='file-v1'")
                    upsert(q, [{'source_customer_id': 'concurrent-shared-key', 'first_name': 'test'}], connector_batch, 1)
        second = pool.submit(competing_insert)
        try:
            with pytest.raises(FutureTimeout):
                second.result(timeout=0.2)
        finally:
            release_first.set()
        assert first.result(timeout=8)
        with pytest.raises(psycopg2.Error, match='connector/file source overlap'):
            second.result(timeout=8)
    with db.cursor() as q:
        assert run(q, 'SELECT count(*) FROM raw.customer_current WHERE source_customer_id=%s',
                   ('concurrent-shared-key',)) == [(1,)]


def test_direct_connector_insert_waits_for_file_commit_then_rejects_overlap(db):
    upsert = writer()
    with db.cursor() as q:
        install(q)
        file_batch = batch(q, '43')
        connector_batch = batch(q, '43', connector=True)
    db.commit()

    from threading import Event
    first_ready, release_first = Event(), Event()

    def file_insert():
        with psycopg2.connect(host='127.0.0.1', port=PORT, user='postgres',
                              dbname=db.info.dbname, connect_timeout=3) as conn:
            with conn.cursor() as q:
                q.execute('SET LOCAL statement_timeout=%s', ('8s',))
                q.execute("SET LOCAL cdp.customer_source_namespace_writer='file-v1'")
                upsert(q, [{'source_customer_id': 'direct-overlap-key'}], file_batch, 1)
                first_ready.set()
                assert release_first.wait(8)

    def connector_insert():
        with psycopg2.connect(host='127.0.0.1', port=PORT, user='postgres',
                              dbname=db.info.dbname, connect_timeout=3) as conn:
            with conn.cursor() as q:
                q.execute('SET LOCAL lock_timeout=%s', ('8s',))
                q.execute('SET LOCAL statement_timeout=%s', ('8s',))
                run(q, '''INSERT INTO raw.customer_current(batch_id,source_system_id,source_customer_id)
                    VALUES (%s,1,%s)''', (connector_batch, 'direct-overlap-key'))

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(file_insert)
        assert first_ready.wait(8)
        second = pool.submit(connector_insert)
        try:
            with pytest.raises(FutureTimeout):
                second.result(timeout=0.2)
        finally:
            release_first.set()
        first.result(timeout=8)
        with pytest.raises(psycopg2.Error, match='connector/file source overlap'):
            second.result(timeout=8)
    with db.cursor() as q:
        assert run(q, '''SELECT app_id,count(*) FROM raw.customer_current
            WHERE source_customer_id='direct-overlap-key' GROUP BY app_id''') == [('43', 1)]


def test_file_bulk_waits_for_direct_connector_commit_before_overlap_probe(db):
    upsert = writer()
    with db.cursor() as q:
        install(q)
        file_batch = batch(q, '43')
        connector_batch = batch(q, '43', connector=True)
    db.commit()
    from threading import Event
    ready, release = Event(), Event()

    def connector_insert():
        with psycopg2.connect(host='127.0.0.1', port=PORT, user='postgres',
                              dbname=db.info.dbname, connect_timeout=3) as conn:
            with conn.cursor() as q:
                q.execute('SET LOCAL statement_timeout=%s', ('8s',))
                run(q, '''INSERT INTO raw.customer_current(batch_id,source_system_id,source_customer_id)
                    VALUES (%s,1,%s)''', (connector_batch, 'reverse-overlap-key'))
                ready.set()
                assert release.wait(8)

    def file_insert():
        with psycopg2.connect(host='127.0.0.1', port=PORT, user='postgres',
                              dbname=db.info.dbname, connect_timeout=3) as conn:
            with conn.cursor() as q:
                q.execute('SET LOCAL lock_timeout=%s', ('8s',))
                q.execute('SET LOCAL statement_timeout=%s', ('8s',))
                q.execute("SET LOCAL cdp.customer_source_namespace_writer='file-v1'")
                upsert(q, [{'source_customer_id': 'reverse-overlap-key'}], file_batch, 1)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(connector_insert)
        assert ready.wait(8)
        second = pool.submit(file_insert)
        try:
            with pytest.raises(FutureTimeout):
                second.result(timeout=0.2)
        finally:
            release.set()
        first.result(timeout=8)
        with pytest.raises(psycopg2.Error, match='connector/file source overlap'):
            second.result(timeout=8)
    with db.cursor() as q:
        assert run(q, '''SELECT app_id,count(*) FROM raw.customer_current
            WHERE source_customer_id='reverse-overlap-key' GROUP BY app_id''') == [(None, 1)]


def test_connector_disjoint_source_key_keeps_legacy_null_app_namespace(db):
    upsert = writer()
    with db.cursor() as q:
        install(q)
        file_batch = batch(q, '43')
        connector_batch = batch(q, '43', connector=True)
        upsert(q, [{'source_customer_id': 'file-key', 'first_name': 'file'}], file_batch, 1)
        upsert(q, [{'source_customer_id': 'connector-key', 'first_name': 'connector'}], connector_batch, 1)
        assert run(q, '''SELECT app_id,source_customer_id,first_name
                         FROM raw.customer_current ORDER BY source_customer_id''') == [
                             (None, 'connector-key', 'connector'), ('43', 'file-key', 'file')]
        assert run(q, 'SELECT app_id FROM raw.customer WHERE batch_id=%s',
                   (connector_batch,)) == [(None,)]
    db.commit()


def test_scoped_file_writer_cannot_replace_connector_source(db):
    upsert = writer()
    with db.cursor() as q:
        install(q)
        connector_batch = batch(q, '43', connector=True)
        file_batch = batch(q, '43')
        upsert(q, [{'source_customer_id': 'synthetic-shared-key', 'first_name': 'connector'}], connector_batch, 1)
        run(q, 'SAVEPOINT file_overlap')
        with pytest.raises(psycopg2.Error, match='connector/file source overlap'):
            upsert(q, [{'source_customer_id': 'synthetic-shared-key', 'first_name': 'file'}], file_batch, 1)
        run(q, 'ROLLBACK TO SAVEPOINT file_overlap')
        assert run(q, 'SELECT app_id,first_name FROM raw.customer_current') == [(None, 'connector')]
    db.rollback()


def test_connector_source_key_overlap_is_rejected_without_misbound_history(db):
    upsert = writer()
    with db.cursor() as q:
        install(q)
        file_batch = batch(q, '43')
        connector_batch = batch(q, '43', connector=True)
        upsert(q, [{'source_customer_id': 'synthetic-shared-key', 'first_name': 'file'}], file_batch, 1)
        run(q, 'SAVEPOINT connector_overlap')
        with pytest.raises(psycopg2.Error, match='connector/file source overlap'):
            upsert(q, [{'source_customer_id': 'synthetic-shared-key', 'first_name': 'connector'}], connector_batch, 1)
        run(q, 'ROLLBACK TO SAVEPOINT connector_overlap')
        assert run(q, '''SELECT app_id,first_name FROM raw.customer_current''') == [('43', 'file')]
        assert run(q, '''SELECT count(*) FROM raw.customer WHERE batch_id=%s''', (connector_batch,)) == [(0,)]
    db.rollback()


def test_validation_snapshot_and_selected_raw_id_never_resolve_another_app(db, monkeypatch):
    ingest = ingestion()
    upsert = writer()
    monkeypatch.setenv('CUSTOMER_SOURCE_NAMESPACE_ENABLED', 'true')
    monkeypatch.setenv('CUSTOMER_BULK_ENABLED', 'true')
    _assert_validation_namespace(db, ingest, upsert)


def _assert_validation_namespace(db, ingest, upsert):
    with db.cursor() as q:
        install(q)
        q.execute("SET LOCAL cdp.customer_source_namespace_writer = 'file-v1'")
        a, b = batch(q, '1282009980020330'), batch(q, '43')
        upsert(q, [{'source_customer_id': 'synthetic-shared-key', 'first_name': 'A'}], a, 1)
        upsert(q, [{'source_customer_id': 'synthetic-shared-key', 'first_name': 'B'}], b, 1)
        raw_a = run(q, 'SELECT raw_customer_id FROM raw.customer_current WHERE app_id=%s', ('1282009980020330',))[0][0]
        raw_b = run(q, 'SELECT raw_customer_id FROM raw.customer_current WHERE app_id=%s', ('43',))[0][0]
        landing = run(q, 'SELECT landing_batch_id FROM control.ingestion_batch WHERE batch_id=%s', (a,))[0][0]
        source_record = str(uuid4())
        run(q, '''CREATE TABLE raw.customer_landing_source (
            source_record_id uuid PRIMARY KEY, batch_id uuid NOT NULL,
            source_row_number integer NOT NULL, source_customer_id text,
            raw_payload jsonb NOT NULL, validation_errors jsonb)''')
        run(q, '''INSERT INTO raw.customer_landing_source
            (source_record_id,batch_id,source_row_number,source_customer_id,raw_payload)
            VALUES (%s,%s,1,'synthetic-shared-key','{}')''', (source_record, landing))
        ingest._create_existing_source_snapshot(q, landing_batch_id=str(landing), source_system_id=1,
                                                ingestion_batch_id=str(a))
        assert run(q, 'SELECT raw_customer_id FROM ingestion_existing_source_snapshot') == [(raw_a,)]
        run(q, '''CREATE SCHEMA quality;
            CREATE EXTENSION IF NOT EXISTS pgcrypto;
            CREATE TABLE quality.customer_validation_record (
              validation_run_id uuid, source_record_id uuid, raw_customer_id uuid,
              source_row_number integer, selected_for_canonical boolean,
              disposition text, existing_source_record boolean,
              preexisting_entity_id uuid, identity_classification text,
              record_fingerprint text, issue_codes text[])''')
        run_id = str(uuid4())
        ingest._seed_validation_records(q, validation_run_id=run_id,
                                        landing_batch_id=str(landing), source_system_id=1,
                                        ingestion_batch_id=str(a))
        assert run(q, '''SELECT raw_customer_id,existing_source_record
                          FROM quality.customer_validation_record WHERE validation_run_id=%s''',
                   (run_id,)) == [(raw_a, True)]
        assert raw_a != raw_b
    db.rollback()
