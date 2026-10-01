"""029: real isolated PostgreSQL tests; existing statuses are never completed by app evidence."""
from contextlib import closing
from pathlib import Path
from uuid import uuid4

import psycopg2
from psycopg2 import sql
import pytest

from test_customer_app_backfill import (
    APP, ROOT, MIGRATIONS, STITCHER, batch, upload, prepare, record, finish, query,
)

MIGRATION = MIGRATIONS / '029_customer_upload_historical_coverage.sql'
ENDPOINT = 'host=127.0.0.1 port=61677 user=postgres connect_timeout=3'
DATABASE = 'cdp_status_repair_test'


@pytest.fixture(scope='module')
def owned_database():
    with closing(psycopg2.connect(ENDPOINT, dbname='postgres')) as admin:
        admin.autocommit = True
        assert (admin.info.host, admin.info.port) == ('127.0.0.1', 61677)
        assert not query(admin, 'SELECT 1 FROM pg_database WHERE datname=%s', (DATABASE,))
        query(admin, sql.SQL('CREATE DATABASE {} TEMPLATE template0').format(sql.Identifier(DATABASE)))
        try:
            yield DATABASE
        finally:
            query(admin, sql.SQL('DROP DATABASE {}').format(sql.Identifier(DATABASE)))


@pytest.fixture
def db(owned_database):
    with closing(psycopg2.connect(ENDPOINT, dbname=owned_database)) as c:
        assert (c.info.host, c.info.port, c.info.dbname) == ('127.0.0.1', 61677, DATABASE)
        c.autocommit = True
        query(c, 'DROP SCHEMA IF EXISTS identity CASCADE; DROP SCHEMA IF EXISTS connector CASCADE; DROP SCHEMA IF EXISTS platform CASCADE')
        query(c, (ROOT / 'tests/integration/fixtures/customer_history_base.sql').read_text())
        query(c, '''ALTER TABLE control.source_landing_batch ADD source_code text, ADD batch_ref text,
            ADD status text, ADD output_published_at timestamptz, ADD updated_at timestamptz DEFAULT now();
            ALTER TABLE control.ingestion_batch ADD batch_ref text, ADD records_received bigint,
            ADD records_rejected bigint DEFAULT 0, ADD completed_at timestamptz, ADD updated_at timestamptz DEFAULT now();
            CREATE TABLE control.source_system(source_system_id bigint PRIMARY KEY,source_code text,source_name text);
            INSERT INTO control.source_system VALUES(1,'not-an-app','synthetic'),(2,'also-not-an-app','synthetic');''')
        query(c, (STITCHER / 'src/identity_stitcher/sql/001_identity_model.sql').read_text())
        for name in ('014_pipeline_tracking.sql', '019_customer_typed_history.sql', '022_customer_bulk_ingestion.sql',
                     '024_customer_identity_publication.sql', '025_customer_publication_status.sql',
                     '026_customer_app_scope.sql', '027_customer_app_backfill.sql', '028_customer_app_backfill_access_paths.sql'):
            query(c, (MIGRATIONS / name).read_text())
        yield c


def install(c):
    assert MIGRATION.exists(), 'additive historical coverage projection 029 missing'
    query(c, MIGRATION.read_text())


def pipeline(c, b):
    return query(c, 'SELECT to_jsonb(p) FROM control.customer_pipeline_status p WHERE ingestion_batch_id=%s', (b,))[0][0]


def succeeded_job(c, b, attempt=1):
    job = str(uuid4())
    # INSERT accurately models old success predating the UPDATE capture trigger.
    query(c, '''INSERT INTO control.stitching_job(job_id,ingestion_batch_id,attempt,status)
        VALUES(%s,%s,%s,'SUCCEEDED')''', (job, b, attempt))
    return job


def published_snapshot(c, batches, source_batch, raw, app=APP, baseline=0):
    c.autocommit = False
    run, workspace = prepare(c, batches, app=app, baseline=baseline)
    record(c, run, workspace, source_batch, raw, app=app)
    finish(c, run)
    c.commit()
    c.autocommit = True
    query(c, '''UPDATE control.customer_identity_transfer SET status='PUBLISHED',source_sha256=%s,
        published_at=clock_timestamp(),updated_at=clock_timestamp() WHERE transfer_id=%s''', ('b'*64, run))
    return run


def test_published_app_evidence_never_manufactures_old_job_publication(db):
    b = batch(db)
    raw = upload(db, b)
    job = succeeded_job(db, b)
    run = published_snapshot(db, [b], b, raw)
    before = pipeline(db, b)
    assert before['overall_status'] == 'TRANSFER_PENDING'
    install(db)
    after = pipeline(db, b)
    assert {k: after[k] for k in before} == before
    assert after['app_snapshot_status'] == 'PUBLISHED'
    assert after['app_snapshot_scope'] == 'LATEST_APP_USERS_AT_CUTOFF'
    assert after['app_snapshot_run_id'] == run
    assert after['app_snapshot_transfer_id'] == run
    assert after['app_snapshot_batch_expected_members'] == 1
    assert after['app_snapshot_batch_available_observations'] == 1
    assert after['app_snapshot_batch_missing_observations'] == 0
    assert after['app_snapshot_app_user_count'] == 1
    assert query(db, 'SELECT status FROM control.stitching_job WHERE job_id=%s', (job,)) == [('SUCCEEDED',)]
    assert query(db, 'SELECT count(*) FROM control.customer_identity_transfer WHERE stitching_job_id=%s', (job,)) == [(0,)]


def test_older_missing_history_is_explicit_and_never_filled(db):
    older = batch(db)
    newer = batch(db, created='2026-09-02T00:00:00Z')
    raw = upload(db, newer)
    query(db, '''INSERT INTO raw.customer_batch_member(ingestion_batch_id,raw_customer_id,source_system_id,source_customer_id)
        VALUES(%s,%s,1,'same-user')''', (older, raw))
    succeeded_job(db, older)
    published_snapshot(db, [older, newer], newer, raw)
    before = pipeline(db, older)
    install(db)
    after = pipeline(db, older)
    assert {k: after[k] for k in before} == before
    assert after['app_snapshot_batch_expected_members'] == 1
    assert after['app_snapshot_batch_available_observations'] == 0
    assert after['app_snapshot_batch_missing_observations'] == 1
    assert query(db, 'SELECT count(*) FROM raw.customer WHERE batch_id=%s', (older,)) == [(0,)]
    assert after['overall_status'] == 'TRANSFER_PENDING'


@pytest.mark.parametrize('stage', ['landing_failed', 'ingestion_failed', 'stitching_failed', 'blocked', 'failed', 'capture_issue', 'retry_running'])
def test_historical_evidence_preserves_every_existing_failure_and_retry(db, stage):
    b = batch(db)
    raw = upload(db, b)
    job = succeeded_job(db, b)
    published_snapshot(db, [b], b, raw)
    if stage == 'landing_failed':
        query(db, "UPDATE control.source_landing_batch SET status='FAILED',output_status='FAILED'")
    elif stage == 'ingestion_failed':
        query(db, "UPDATE control.ingestion_batch SET status='failed' WHERE batch_id=%s", (b,))
    elif stage == 'stitching_failed':
        query(db, "UPDATE control.stitching_job SET status='FAILED',error_code='REAL_FAILURE' WHERE job_id=%s", (job,))
    elif stage in ('blocked', 'failed'):
        resolution = str(uuid4())
        query(db, '''INSERT INTO identity.resolution_run(run_id,stitching_job_id,status,algorithm,algorithm_version,
            normalization_version,match_threshold) VALUES(%s,%s,'SUCCEEDED','test','test','test',0.9)''', (resolution, job))
        query(db, '''INSERT INTO control.customer_identity_transfer(transfer_id,resolution_run_id,stitching_job_id,
            ingestion_batch_id,status,row_count,error_code,app_id,workspace_id)
            SELECT %s,%s,%s,%s,%s,0,'REAL_FAILURE',app_id,workspace_id
            FROM control.customer_identity_upload_scope WHERE app_id=%s''',
            (resolution, resolution, job, b, stage.upper(), APP))
    elif stage == 'capture_issue':
        query(db, "INSERT INTO control.customer_identity_capture_issue(stitching_job_id,error_code) VALUES(%s,'NO_SUCCESSFUL_RESOLUTION')", (job,))
    else:
        retry = succeeded_job(db, b, attempt=2)
        query(db, "UPDATE control.stitching_job SET status='RUNNING' WHERE job_id=%s", (retry,))
    before = pipeline(db, b)
    stored_before = {t: query(db, f'SELECT to_jsonb(t) FROM {t} t ORDER BY to_jsonb(t)::text') for t in (
        'control.stitching_job', 'control.ingestion_batch', 'control.customer_identity_transfer', 'control.customer_identity_export')}
    install(db)
    after = pipeline(db, b)
    assert {k: after[k] for k in before} == before
    assert after['app_snapshot_status'] == 'PUBLISHED'
    assert after['overall_status'] not in ('COMPLETED', 'COMPLETED_WITH_WARNINGS')
    assert {t: query(db, f'SELECT to_jsonb(t) FROM {t} t ORDER BY to_jsonb(t)::text') for t in stored_before} == stored_before


def test_new_upload_and_literal_other_app_never_inherit_old_app_snapshot(db):
    b = batch(db)
    raw = upload(db, b)
    published_snapshot(db, [b], b, raw)
    new = batch(db, created='2026-09-20T00:00:00Z')
    other = batch(db, app='222', created='2026-09-21T00:00:00Z')
    for item in (new, other):
        upload(db, item)
        succeeded_job(db, item)
    install(db)
    for item in (new, other):
        assert pipeline(db, item)['app_snapshot_status'] is None
        assert pipeline(db, item)['overall_status'] == 'TRANSFER_PENDING'


@pytest.mark.parametrize('status', ['PENDING', 'RUNNING', 'FAILED', 'BLOCKED'])
def test_nonpublished_backfill_is_not_coverage(db, status):
    b = batch(db)
    raw = upload(db, b)
    c = db
    c.autocommit = False
    run, workspace = prepare(c, [b])
    record(c, run, workspace, b, raw)
    finish(c, run)
    c.commit()
    c.autocommit = True
    query(c, 'UPDATE control.customer_identity_transfer SET status=%s WHERE transfer_id=%s', (status, run))
    install(c)
    assert pipeline(c, b)['app_snapshot_status'] is None


def test_reader_acl_oid_columns_and_later_extensions_are_preserved(db):
    b = batch(db)
    raw = upload(db, b)
    published_snapshot(db, [b], b, raw)
    reader = 'historical_reader_' + uuid4().hex
    query(db, sql.SQL('CREATE ROLE {}; GRANT USAGE ON SCHEMA control TO {}').format(sql.Identifier(reader), sql.Identifier(reader)))
    query(db, sql.SQL('GRANT SELECT ON control.customer_pipeline_status TO {}').format(sql.Identifier(reader)))
    definition = query(db, "SELECT pg_get_viewdef('control.customer_pipeline_status'::regclass,true)")[0][0].rstrip(';\n ')
    query(db, 'CREATE OR REPLACE VIEW control.customer_pipeline_status AS SELECT p.*,42 AS future_extension FROM (' + definition + ') p')
    before = query(db, "SELECT oid,relowner,relacl FROM pg_class WHERE oid='control.customer_pipeline_status'::regclass")
    try:
        install(db)
        assert query(db, "SELECT oid,relowner,relacl FROM pg_class WHERE oid='control.customer_pipeline_status'::regclass") == before
        query(db, sql.SQL('SET ROLE {}').format(sql.Identifier(reader)))
        # Late reader receives SELECT only, not a manual grant to private helpers.
        row = pipeline(db, b)
        assert row['future_extension'] == 42
        assert row['app_snapshot_status'] == 'PUBLISHED'
        assert query(db, 'SELECT count(*) FROM control.customer_upload_historical_coverage') == [(1,)]
        with pytest.raises(psycopg2.errors.InsufficientPrivilege):
            query(db, 'SELECT * FROM control.customer_app_backfill_record')
    finally:
        query(db, 'RESET ROLE')
        query(db, sql.SQL('DROP OWNED BY {}; DROP ROLE {}').format(sql.Identifier(reader), sql.Identifier(reader)))


def test_latest_published_matching_manifest_wins_without_row_multiplication(db):
    b = batch(db)
    raw = upload(db, b)
    first = published_snapshot(db, [b], b, raw)
    baseline = query(db, 'SELECT publication_seq FROM control.customer_identity_transfer WHERE transfer_id=%s', (first,))[0][0]
    second = published_snapshot(db, [b], b, raw, baseline=baseline)
    install(db)
    assert pipeline(db, b)['app_snapshot_run_id'] == second
    assert query(db, 'SELECT count(*) FROM control.customer_upload_historical_coverage WHERE ingestion_batch_id=%s', (b,)) == [(1,)]


def test_connector_is_excluded_even_if_it_has_historical_manifest(db):
    b = batch(db)
    raw = upload(db, b)
    published_snapshot(db, [b], b, raw)
    query(db, '''CREATE SCHEMA connector; CREATE TABLE connector.sync_run(
        ingestion_batch_id uuid,landing_batch_id uuid,stitching_job_id uuid);
        INSERT INTO connector.sync_run(ingestion_batch_id) VALUES(%s)''', (b,))
    install(db)
    assert pipeline(db, b)['app_snapshot_status'] is None


def test_existing_completed_upload_status_and_capture_are_unchanged(db):
    b = batch(db)
    job = succeeded_job(db, b)
    run = str(uuid4())
    query(db, '''INSERT INTO identity.resolution_run(run_id,stitching_job_id,status,normalization_version,match_threshold)
        VALUES(%s,%s,'SUCCEEDED','synthetic',0.9)''', (run, job))
    query(db, 'INSERT INTO control.customer_identity_upload_scope(app_id) VALUES(%s)', (APP,))
    query(db, '''INSERT INTO control.customer_identity_transfer(transfer_id,resolution_run_id,stitching_job_id,
        ingestion_batch_id,status,row_count,app_id,workspace_id)
        SELECT %s,%s,%s,%s,'PUBLISHED',0,app_id,workspace_id
        FROM control.customer_identity_upload_scope WHERE app_id=%s''', (run, run, job, b, APP))
    before = pipeline(db, b)
    assert before['overall_status'] == 'COMPLETED'
    install(db)
    after = pipeline(db, b)
    assert {k: after[k] for k in before} == before
    assert after['app_snapshot_status'] is None


def test_migration_fails_closed_without_backfill_dependency(db):
    query(db, 'DROP TABLE control.customer_app_backfill_batch CASCADE')
    with pytest.raises(psycopg2.errors.UndefinedTable):
        install(db)
    query(db, 'ROLLBACK')
    assert query(db, "SELECT to_regclass('control.customer_upload_historical_coverage')") == [(None,)]


def test_admin_install_preserves_nonadmin_pipeline_owner(db):
    b = batch(db)
    raw = upload(db, b)
    published_snapshot(db, [b], b, raw)
    owner = 'historical_owner_' + uuid4().hex
    query(db, sql.SQL('CREATE ROLE {}; GRANT USAGE ON SCHEMA control,raw,staging,identity TO {}').format(sql.Identifier(owner), sql.Identifier(owner)))
    query(db, sql.SQL('GRANT SELECT ON ALL TABLES IN SCHEMA control,raw,staging,identity TO {}').format(sql.Identifier(owner)))
    query(db, sql.SQL('ALTER VIEW control.customer_pipeline_status OWNER TO {}').format(sql.Identifier(owner)))
    try:
        install(db)
        query(db, sql.SQL('SET ROLE {}').format(sql.Identifier(owner)))
        assert pipeline(db, b)['app_snapshot_status'] == 'PUBLISHED'
    finally:
        query(db, 'RESET ROLE; ALTER VIEW control.customer_pipeline_status OWNER TO CURRENT_USER')
        query(db, sql.SQL('DROP OWNED BY {}; DROP ROLE {}').format(sql.Identifier(owner), sql.Identifier(owner)))

