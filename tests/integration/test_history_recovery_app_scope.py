"""Recovery compatibility with real 026; task-owned loopback PostgreSQL only.

Run serially from the repo with .venv/bin/python -m pytest this file.
Imports the unchanged pre-026 recovery cases into the same isolated fixture.
"""
from contextlib import closing
from pathlib import Path
from uuid import uuid4

import psycopg2
from psycopg2 import sql
import pytest

# Reuse the reviewed recovery baseline and its regression tests, never its shared DB.
from test_history_recovery import *  # noqa: F403

ROOT = Path(__file__).resolve().parents[2]


def query(c, statement, args=None):
    with c.cursor() as q:
        q.execute(statement, args)
        return q.fetchall() if q.description else []


@pytest.fixture(scope='module')
def owned_database():
    name = 'recovery_app_test_' + uuid4().hex
    params: dict = dict(host='127.0.0.1', port=61577, user='postgres', connect_timeout=3)
    with closing(psycopg2.connect(dbname='postgres', **params)) as admin:
        admin.autocommit = True
        assert not query(admin, 'SELECT 1 FROM pg_database WHERE datname=%s', (name,))
        query(admin, sql.SQL("CREATE DATABASE {} TEMPLATE template0 ENCODING 'UTF8'").format(sql.Identifier(name)))
        try:
            yield dict(dbname=name, **params)
        finally:
            query(admin, sql.SQL('DROP DATABASE {}').format(sql.Identifier(name)))
            assert not query(admin, 'SELECT 1 FROM pg_database WHERE datname=%s', (name,))


@pytest.fixture
def db(owned_database):
    with closing(psycopg2.connect(**owned_database)) as c:
        assert c.info.host == '127.0.0.1' and c.info.port == 61577
        assert c.info.dbname.startswith('recovery_app_test_')
        c.autocommit = True
        query(c, 'DROP SCHEMA IF EXISTS connector CASCADE')
        query(c, (ROOT/'tests/integration/fixtures/customer_history_base.sql').read_text())
        for name in ('019_customer_typed_history.sql', '020_customer_history_recovery.sql'):
            query(c, (ROOT/'database/migrations'/name).read_text())
        c.autocommit = False
        try:
            yield c
        finally:
            c.rollback()


def install_app_scope(c, batch, ref='2026-09-19-120001-000222-123456789abc', connector=False):
    c.commit()
    c.autocommit = True
    query(c, '''ALTER TABLE control.ingestion_batch ADD COLUMN batch_ref text;
        ALTER TABLE control.source_landing_batch ADD COLUMN batch_ref text, ADD COLUMN source_code text;
        CREATE TABLE control.source_system(source_system_id bigint PRIMARY KEY,source_code text);
        INSERT INTO control.source_system VALUES(1,'source-not-an-app');
        CREATE TABLE control.stitching_job(job_id uuid PRIMARY KEY,ingestion_batch_id uuid);
        CREATE SCHEMA connector;
        CREATE TABLE connector.sync_run(ingestion_batch_id uuid,landing_batch_id uuid,stitching_job_id uuid);''')
    query(c, 'UPDATE control.ingestion_batch SET batch_ref=%s WHERE batch_id=%s', (ref, batch))
    query(c, "UPDATE control.source_landing_batch SET batch_ref=%s,source_code='source-not-an-app'", (ref,))
    if connector:
        query(c, 'INSERT INTO connector.sync_run(ingestion_batch_id) VALUES(%s)', (batch,))
    for name in ('022_customer_bulk_ingestion.sql', '026_customer_app_scope.sql'):
        query(c, (ROOT/'database/migrations'/name).read_text())
    # Exercise the actual ingestion trigger, with the control app backfill completed.
    query(c, 'UPDATE control.ingestion_batch SET batch_ref=batch_ref')
    c.autocommit = False


def test_literal_app_is_derived_into_expected_stage_and_exact_retry(db):
    batch, body = seed(db)
    install_app_scope(db, batch)
    with db:
        before = query(db, 'SELECT to_jsonb(c) FROM raw.customer_current c ORDER BY source_customer_id')
    assert recovery.recover_batch(db, batch, body, 'version-1', apply=True)['inserted_rows'] == 2
    assert recovery.recover_batch(db, batch, body, 'version-1', apply=True)['inserted_rows'] == 0
    with db:
        assert query(db, 'SELECT DISTINCT app_id FROM raw.customer WHERE batch_id=%s', (batch,)) == [('000222',)]
        assert query(db, 'SELECT to_jsonb(c) FROM raw.customer_current c ORDER BY source_customer_id') == before
        assert query(db, 'SELECT count(*) FROM control.customer_history_recovery') == [(1,)]


@pytest.mark.parametrize('drift', [
    "ALTER TABLE raw.customer ALTER COLUMN app_id TYPE varchar(80)",
    "ALTER TABLE raw.customer ALTER COLUMN app_id SET DEFAULT '000222'",
    "ALTER TABLE raw.customer DROP COLUMN app_id; ALTER TABLE raw.customer ADD COLUMN app_id text NOT NULL DEFAULT '000222'; ALTER TABLE raw.customer ALTER COLUMN app_id DROP DEFAULT",
    "ALTER TABLE raw.customer_current ALTER COLUMN app_id TYPE varchar(80)",
    "DROP TRIGGER customer_app_id_derive ON control.ingestion_batch; ALTER TABLE control.ingestion_batch ALTER COLUMN app_id TYPE varchar(80)",
    "ALTER TABLE raw.customer_current DROP COLUMN app_id",
    "ALTER TABLE raw.customer ADD COLUMN unreviewed_business text",
])
def test_only_exact_nullable_text_app_schema_is_accepted(db, drift):
    batch, body = seed(db)
    install_app_scope(db, batch)
    with db:
        # No fixture history exists yet for the old batch; only newer current rows.
        query(db, drift)
    with pytest.raises(recovery.RecoveryError, match='^TYPED_SCHEMA_MISMATCH$'):
        recovery.recover_batch(db, batch, body, 'version-1', apply=True)
    with db:
        assert query(db, 'SELECT count(*) FROM raw.customer WHERE batch_id=%s', (batch,)) == [(0,)]
        assert query(db, 'SELECT count(*) FROM control.customer_history_recovery') == [(0,)]


@pytest.mark.parametrize('ref,connector', [
    ('bad-ref', False),
    ('2026-09-19-120001-000222-123456789abc ', False),
    ('2026-09-19-120001-000222-123456789abc', True),
])
def test_unattributed_and_connector_app_is_exact_null(db, ref, connector):
    batch, body = seed(db)
    install_app_scope(db, batch, ref=ref, connector=connector)
    assert recovery.recover_batch(db, batch, body, 'version-1', apply=True)['inserted_rows'] == 2
    assert recovery.recover_batch(db, batch, body, 'version-1', apply=True)['inserted_rows'] == 0
    with db:
        assert query(db, 'SELECT DISTINCT app_id FROM raw.customer WHERE batch_id=%s', (batch,)) == [(None,)]


def test_connector_metadata_before_registration_keeps_app_null(db):
    batch, body = seed(db)
    install_app_scope(db, batch)
    with db:
        query(db, "UPDATE control.ingestion_batch SET metadata=metadata || '{\"connector_sync_run_id\":\"durable\"}' WHERE batch_id=%s", (batch,))
    assert recovery.recover_batch(db, batch, body, 'version-1', apply=True)['inserted_rows'] == 2
    with db:
        assert query(db, 'SELECT DISTINCT app_id FROM raw.customer WHERE batch_id=%s', (batch,)) == [(None,)]


@pytest.mark.parametrize('field,value', [
    ('app_id', '222'), ('app_id', None),
    ('email', 'changed@example.test'), ('date_of_birth', '1999-01-01'),
    ('attributes', Json({'v': 2})), ('raw_payload', Json({})), ('status', None),
])
def test_app_and_every_old_typed_value_still_conflict_atomically(db, field, value):
    batch, body = seed(db)
    install_app_scope(db, batch)
    row = recovery.parse_artifact(body, hashlib.sha256(body).hexdigest(), 2)[0]
    row['app_id'] = '000222'
    row[field] = value
    with db:
        # Simulate immutable partial evidence captured before migration/backfill.
        # INSERT-only trigger disabled in this disposable synthetic fixture only.
        query(db, 'ALTER TABLE raw.customer DISABLE TRIGGER customer_app_id_derive')
        query(db, sql.SQL('INSERT INTO raw.customer(raw_customer_id,batch_id,source_system_id,{}) SELECT raw_customer_id,%s,source_system_id,{} FROM raw.customer_current WHERE source_customer_id=%s').format(
            sql.SQL(',').join(map(sql.Identifier, row)), sql.SQL(',').join(sql.Placeholder() for _ in row)),
            (batch, *[Json(v) if isinstance(v, dict) else v for v in row.values()], row['source_customer_id']))
        query(db, 'ALTER TABLE raw.customer ENABLE TRIGGER customer_app_id_derive')
        before = query(db, 'SELECT to_jsonb(h) FROM raw.customer h WHERE batch_id=%s', (batch,))
    with pytest.raises(recovery.RecoveryError, match='^TYPED_HISTORY_MISMATCH$'):
        recovery.recover_batch(db, batch, body, 'version-1', apply=True)
    with db:
        assert query(db, 'SELECT to_jsonb(h) FROM raw.customer h WHERE batch_id=%s', (batch,)) == before
        assert query(db, 'SELECT count(*) FROM control.customer_history_recovery') == [(0,)]


def test_unbackfilled_control_app_fails_instead_of_accepting_null(db):
    batch, body = seed(db)
    install_app_scope(db, batch)
    with db:
        query(db, 'ALTER TABLE control.ingestion_batch DISABLE TRIGGER customer_app_id_derive')
        query(db, 'UPDATE control.ingestion_batch SET app_id=NULL WHERE batch_id=%s', (batch,))
        query(db, 'ALTER TABLE control.ingestion_batch ENABLE TRIGGER customer_app_id_derive')
    with pytest.raises(recovery.RecoveryError, match='^POST_INSERT_MISMATCH$'):
        recovery.recover_batch(db, batch, body, 'version-1', apply=True)
    with db:
        assert query(db, 'SELECT count(*) FROM raw.customer WHERE batch_id=%s', (batch,)) == [(0,)]
        assert query(db, 'SELECT count(*) FROM control.customer_history_recovery') == [(0,)]


def test_post_026_unversioned_audit_and_exact_partial_history(db, tmp_path):
    batch, body = seed(db)
    install_app_scope(db, batch)
    row = recovery.parse_artifact(body, hashlib.sha256(body).hexdigest(), 2)[0]
    with db:
        query(db, "UPDATE control.ingestion_batch SET metadata='{}' WHERE batch_id=%s", (batch,))
        query(db, sql.SQL('INSERT INTO raw.customer(raw_customer_id,batch_id,source_system_id,{}) SELECT raw_customer_id,%s,source_system_id,{} FROM raw.customer_current WHERE source_customer_id=%s').format(
            sql.SQL(',').join(map(sql.Identifier, row)), sql.SQL(',').join(sql.Placeholder() for _ in row)),
            (batch, *[Json(v) if isinstance(v, dict) else v for v in row.values()], row['source_customer_id']))
        before = query(db, 'SELECT to_jsonb(h) FROM raw.customer h WHERE batch_id=%s', (batch,))
    kwargs = dict(checksum_bound_unversioned=True, archive_dir=tmp_path)
    assert recovery.recover_batch(db, batch, body, None, **kwargs)['inserted_rows'] == 1
    with db:
        assert query(db, 'SELECT to_jsonb(h) FROM raw.customer h WHERE batch_id=%s', (batch,)) == before
        assert query(db, 'SELECT count(*) FROM control.customer_history_recovery') == [(0,)]
    assert recovery.recover_batch(db, batch, body, None, apply=True, **kwargs)['inserted_rows'] == 1
    assert recovery.recover_batch(db, batch, body, None, apply=True, **kwargs)['inserted_rows'] == 0
    with db:
        assert query(db, "SELECT to_jsonb(h) FROM raw.customer h WHERE batch_id=%s AND source_customer_id='k1'", (batch,)) == before
        assert query(db, 'SELECT DISTINCT app_id FROM raw.customer WHERE batch_id=%s', (batch,)) == [('000222',)]


def test_literal_csv_app_cannot_override_physical_provenance(db):
    batch, _ = seed(db)
    body = b'source_customer_id,app_id\nk1,999\nk2,999\n'
    digest = hashlib.sha256(body).hexdigest()
    with db:
        query(db, 'UPDATE control.ingestion_batch SET checksum=%s WHERE batch_id=%s', (digest, batch))
        query(db, 'UPDATE control.source_landing_batch SET output_sha256=%s', (digest,))
    install_app_scope(db, batch)
    assert recovery.recover_batch(db, batch, body, 'version-1', apply=True)['inserted_rows'] == 2
    with db:
        # Frozen parser ignores unknown CSV columns. Do not alter its payload contract.
        assert query(db, "SELECT DISTINCT app_id,raw_payload->>'app_id' FROM raw.customer WHERE batch_id=%s", (batch,)) == [('000222', None)]


@pytest.mark.parametrize('mutation', [
    "UPDATE control.source_system SET source_code='changed'",
    "INSERT INTO control.stitching_job(job_id) VALUES(gen_random_uuid())",
    "INSERT INTO connector.sync_run(landing_batch_id) SELECT landing_batch_id FROM control.ingestion_batch WHERE status='completed'",
])
def test_app_lineage_is_locked_until_recovery_finishes(db, owned_database, monkeypatch, mutation):
    from psycopg2 import extras
    batch, body = seed(db)
    install_app_scope(db, batch)
    original = extras.execute_values
    probes = []

    def concurrent_lineage_change(*args, **kwargs):
        # At expected-stage insertion app_id has been derived, but recovery has
        # not inserted history yet. A competing real connection must block.
        with closing(psycopg2.connect(**owned_database)) as other:
            with other.cursor() as q:
                q.execute("SET LOCAL lock_timeout='150ms'")
                with pytest.raises(psycopg2.errors.LockNotAvailable):
                    q.execute(mutation)
            other.rollback()
        probes.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(extras, 'execute_values', concurrent_lineage_change)
    assert recovery.recover_batch(db, batch, body, 'version-1', apply=True)['inserted_rows'] == 2
    assert probes == [True]
