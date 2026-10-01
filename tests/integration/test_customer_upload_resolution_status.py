"""032: actual 025/027/031/032, only task-owned localhost:61677 database.

Run: ../fargate-user-stitching/.venv/bin/python -m pytest <this file> -q
Refuses a preexisting database. Never touches sibling fixtures or production.
"""
from contextlib import closing
from uuid import uuid4

import psycopg2
from psycopg2 import sql
import pytest

from test_customer_app_backfill import (
    APP, ROOT, MIGRATIONS, STITCHER, batch, upload, record, finish, query,
)

MIGRATION = MIGRATIONS / '032_customer_upload_resolution_status.sql'
ENDPOINT = 'host=127.0.0.1 port=61677 user=postgres connect_timeout=3'
DATABASE = 'cdp_scoped_status_test'


@pytest.fixture(scope='module')
def owned_database():
    with closing(psycopg2.connect(ENDPOINT, dbname='postgres')) as admin:
        admin.autocommit = True
        assert (admin.info.host, admin.info.port, admin.info.dbname) == ('127.0.0.1', 61677, 'postgres')
        assert not query(admin, 'SELECT 1 FROM pg_database WHERE datname=%s', (DATABASE,)), 'Refusing existing database'
        query(admin, sql.SQL("CREATE DATABASE {} TEMPLATE template0 ENCODING 'UTF8'").format(sql.Identifier(DATABASE)))
        try:
            yield DATABASE
        finally:
            query(admin, sql.SQL('DROP DATABASE {}').format(sql.Identifier(DATABASE)))
            assert not query(admin, 'SELECT 1 FROM pg_database WHERE datname=%s', (DATABASE,))


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
        for name in ('014_pipeline_tracking.sql', '019_customer_typed_history.sql', '020_customer_history_recovery.sql',
                     '021_customer_history_recovery_outcomes.sql', '022_customer_bulk_ingestion.sql',
                     '024_customer_identity_publication.sql', '025_customer_publication_status.sql',
                     '026_customer_app_scope.sql', '027_customer_app_backfill.sql', '028_customer_app_backfill_access_paths.sql',
                     '031_customer_upload_resolution.sql', '033_customer_upload_continuation.sql'):
            query(c, (MIGRATIONS / name).read_text())
            if name == '014_pipeline_tracking.sql':
                # Actual 015 validation table/view, as in the 025 integration fixture.
                quality = (MIGRATIONS / '015_customer_data_quality.sql').read_text()
                query(c, 'CREATE SCHEMA quality')
                query(c, quality[quality.index('CREATE TABLE IF NOT EXISTS quality.customer_validation_run'):
                                 quality.index('CREATE INDEX IF NOT EXISTS ix_customer_validation_run_landing')])
                query(c, quality[quality.index('CREATE OR REPLACE VIEW control.customer_pipeline_status AS'):
                                 quality.index('COMMENT ON VIEW control.customer_pipeline_status')])
        yield c


@pytest.fixture
def continuation(db):
    return db


def install(c):
    # RED must exercise the old observable behavior, not fail on missing DDL.
    if MIGRATION.exists():
        query(c, MIGRATION.read_text())


def pipeline(c, b, view='customer_pipeline_status'):
    return query(c, f'SELECT to_jsonb(p) FROM control.{view} p WHERE ingestion_batch_id=%s', (b,))[0][0]


def global_job(c, b, raw, *, mixed=True):
    job, run, profile, entity = (str(uuid4()) for _ in range(4))
    query(c, "INSERT INTO control.stitching_job(job_id,ingestion_batch_id,status) VALUES(%s,%s,'RUNNING')", (job, b))
    query(c, '''INSERT INTO identity.resolution_run(run_id,stitching_job_id,status,normalization_version,match_threshold)
        VALUES(%s,%s,'RUNNING','test',0.9)''', (run, job))
    query(c, '''INSERT INTO staging.customer_profile(standardized_record_id,raw_customer_id,batch_id,source_system_id,source_customer_id)
        VALUES(%s,%s,%s,1,'same-user')''', (profile, raw, b))
    query(c, 'INSERT INTO identity.customer_entity(customer_entity_id,last_resolution_run_id) VALUES(%s,%s)', (entity, run))
    query(c, '''INSERT INTO identity.customer_entity_member(standardized_record_id,customer_entity_id,source_system_id,
        source_customer_id,match_method,resolution_run_id) VALUES(%s,%s,1,'same-user','singleton',%s)''', (profile, entity, run))
    query(c, "UPDATE identity.resolution_run SET status='SUCCEEDED',completed_at=clock_timestamp() WHERE run_id=%s", (run,))
    query(c, '''UPDATE control.stitching_job SET status='SUCCEEDED',selected_record_count=1,member_record_count=1,
        standardized_record_count=1,ready_record_count=1,completed_at=clock_timestamp() WHERE job_id=%s''', (job,))
    assert query(c, 'SELECT status FROM control.customer_identity_transfer WHERE transfer_id=%s', (run,)) == [('BLOCKED' if mixed else 'PENDING',)]
    return job, run


def blocked(c):
    foreign = batch(c, app='222')  # Literal APP='000222' must stay distinct.
    raw = upload(c, foreign, name='foreign')
    b = batch(c, created='2026-09-02T00:00:00Z')
    assert upload(c, b, name='local') == raw
    job, run = global_job(c, b, raw)
    return b, job, run, raw


def intent(c, b, job, attempt=1):
    run = str(uuid4())
    query(c, '''INSERT INTO control.customer_upload_resolution_attempt
        (stitching_job_id,attempt,run_id,ingestion_batch_id,app_id) VALUES(%s,%s,%s,%s,%s)''',
        (job, attempt, run, b, APP))
    return run


def prepare_scoped(c, run, batches, *, app=APP):
    query(c, 'INSERT INTO control.customer_identity_upload_scope(app_id) VALUES(%s) ON CONFLICT DO NOTHING', (app,))
    workspace = query(c, 'SELECT workspace_id::text FROM control.customer_identity_upload_scope WHERE app_id=%s', (app,))[0][0]
    baseline = query(c, 'SELECT coalesce(max(publication_seq),0) FROM control.customer_identity_transfer WHERE app_id=%s', (app,))[0][0]
    query(c, '''INSERT INTO identity.resolution_run(run_id,status,algorithm,algorithm_version,normalization_version,
        match_threshold,source_record_count,selected_record_count,standardized_record_count,entity_count)
        VALUES(%s,'RUNNING','splink','test','app-historical-v1',0.9,1,1,1,1)''', (run,))
    query(c, '''INSERT INTO control.customer_app_backfill_run(run_id,app_id,workspace_id,expected_users,
        input_sha256,cutoff,baseline_publication_seq,config) VALUES(%s,%s,%s,1,%s,'2026-09-19Z',%s,'{}')''',
        (run, app, workspace, 'a'*64, baseline))
    query(c, '''INSERT INTO control.customer_app_backfill_batch(run_id,batch_id,expected_members,available_observations,batch_created_at)
        SELECT %s,b.batch_id,count(m.raw_customer_id),count(h.raw_customer_id),b.created_at
        FROM control.ingestion_batch b LEFT JOIN raw.customer_batch_member m ON m.ingestion_batch_id=b.batch_id
        LEFT JOIN raw.customer h ON (h.batch_id,h.raw_customer_id,h.source_system_id,h.source_customer_id)=
            (m.ingestion_batch_id,m.raw_customer_id,m.source_system_id,m.source_customer_id)
        WHERE b.batch_id=ANY(%s::uuid[]) GROUP BY b.batch_id''', (run, batches))
    return workspace


def publish(c, run):
    query(c, '''UPDATE control.customer_identity_transfer SET status='PUBLISHED',source_sha256=%s,
        published_at=clock_timestamp(),updated_at=clock_timestamp() WHERE transfer_id=%s''', ('b'*64, run))


def successful_scoped(c, b, job, raw):
    run = intent(c, b, job)
    workspace = prepare_scoped(c, run, [b])
    record(c, run, workspace, b, raw)
    finish(c, run)
    return run


def stored_truth(c):
    tables = ('control.stitching_job', 'control.ingestion_batch', 'raw.customer', 'raw.customer_current',
              'identity.resolution_run', 'identity.customer_entity', 'identity.customer_entity_member',
              'control.customer_identity_transfer', 'control.customer_identity_export')
    return {t: query(c, f'SELECT to_jsonb(t) FROM {t} t ORDER BY to_jsonb(t)::text') for t in tables}


def test_pinned_intent_is_pending_not_old_global_block_and_keeps_truth(db):
    b, job, original_run, _ = blocked(db)
    run = intent(db, b, job)
    before = stored_truth(db)
    install(db)
    row = pipeline(db, b)
    assert row['overall_status'] == 'SCOPED_RESOLUTION_PENDING'
    assert row['resolution_run_id'] == original_run
    assert row['stitching_status'] == 'SUCCEEDED'
    assert row['effective_publication_resolution_run_id'] == run
    assert row['effective_publication_origin'] == 'SCOPED_UPLOAD_RESOLUTION'
    assert row['scoped_resolution_status'] == 'PENDING'
    assert row['scoped_resolution_attempt'] == 1
    assert row['transfer_id'] is None and row['transfer_status'] is None
    assert row['original_transfer_id'] == original_run
    assert row['original_transfer_status'] == 'BLOCKED'
    assert row['original_transfer_error_code'] == 'MIXED_OR_UNKNOWN_OWNER'
    assert row['updated_at'] >= row['effective_publication_updated_at']
    assert stored_truth(db) == before


def test_actual_scoped_resolution_and_transfer_stage_transitions(db):
    b, job, original_run, raw = blocked(db)
    run = intent(db, b, job)
    install(db)
    workspace = prepare_scoped(db, run, [b])
    assert pipeline(db, b)['overall_status'] == 'SCOPED_RESOLVING'
    assert pipeline(db, b)['scoped_resolution_status'] == 'BUILDING'
    record(db, run, workspace, b, raw)
    finish(db, run)
    for state, expected in [('PENDING', 'TRANSFER_PENDING'), ('RUNNING', 'TRANSFERRING'),
                            ('FAILED', 'TRANSFER_FAILED'), ('PUBLISHED', 'COMPLETED')]:
        if state == 'PUBLISHED':
            publish(db, run)
        else:
            query(db, '''UPDATE control.customer_identity_transfer SET status=%s,updated_at=clock_timestamp(),
                error_code=CASE WHEN %s='FAILED' THEN 'REMOTE_FAILURE' ELSE NULL END WHERE transfer_id=%s''', (state, state, run))
        for view in ('customer_pipeline_status', 'customer_publication_status'):
            row = pipeline(db, b, view)
            assert row['overall_status'] == expected
            assert row['scoped_resolution_status'] == 'SUCCEEDED'
            assert row['resolution_run_id'] == original_run
            assert row['effective_publication_resolution_run_id'] == run
            assert row['transfer_id'] == run and row['transfer_status'] == state
            assert row['transfer_app_id'] == APP
            assert row['effective_publication_scope'] == 'LATEST_APP_USERS_AT_CUTOFF'
            assert row['scoped_resolution_selected_expected_members'] == 1
            assert row['scoped_resolution_selected_available_observations'] == 1
            assert row['scoped_resolution_selected_missing_observations'] == 0
            assert row['scoped_resolution_selected_coverage_complete'] is True
            assert row['scoped_resolution_last_input_batch_id'] == b
            assert row['scoped_resolution_last_input_created_at'].startswith('2026-09-02')
            assert row['scoped_resolution_cutoff'].startswith('2026-09-19')
    assert query(db, 'SELECT status FROM control.customer_identity_transfer WHERE transfer_id=%s', (original_run,)) == [('BLOCKED',)]


def test_failed_scoped_ledger_and_new_intent_follow_latest_not_old_failure(db):
    b, job, _, _ = blocked(db)
    first = intent(db, b, job)
    prepare_scoped(db, first, [b])
    query(db, "UPDATE control.customer_app_backfill_run SET status='FAILED',error_code='MATCH_BUDGET' WHERE run_id=%s", (first,))
    install(db)
    row = pipeline(db, b)
    assert row['overall_status'] == 'SCOPED_RESOLUTION_FAILED'
    assert row['scoped_resolution_error_code'] == 'MATCH_BUDGET'
    assert row['transfer_status'] is None and row['transfer_error_code'] is None
    second = intent(db, b, job, attempt=2)
    row = pipeline(db, b)
    assert row['overall_status'] == 'SCOPED_RESOLUTION_PENDING'
    assert row['effective_publication_resolution_run_id'] == second
    assert row['scoped_resolution_error_code'] is None
    assert row['scoped_resolution_attempt'] == 2


@pytest.mark.parametrize('field,value', [
    ('published_at', None), ('source_sha256', None), ('row_count', 2),
    ('app_id', '222'), ('workspace_id', '00000000-0000-0000-0000-000000000001'),
    ('backfill_run_id', None), ('resolution_run_id', '00000000-0000-0000-0000-000000000001'),
])
def test_scoped_published_transfer_requires_exact_binding_and_publication_evidence(db, field, value):
    b, job, _, raw = blocked(db)
    run = successful_scoped(db, b, job, raw)
    publish(db, run)
    # Fault injection into the actual table, never an alternate status schema.
    # Exclusive-origin CHECK makes two identity corruptions impossible even with
    # triggers off; remove just that CHECK in this disposable fixture.
    query(db, 'ALTER TABLE control.customer_identity_transfer DROP CONSTRAINT customer_identity_transfer_exclusive_origin')
    query(db, 'SET session_replication_role=replica')
    try:
        query(db, sql.SQL('UPDATE control.customer_identity_transfer SET {}=%s WHERE transfer_id=%s').format(sql.Identifier(field)), (value, run))
    finally:
        query(db, 'SET session_replication_role=origin')
    install(db)
    assert pipeline(db, b)['overall_status'] not in ('COMPLETED', 'COMPLETED_WITH_WARNINGS')


@pytest.mark.parametrize('defect', ['missing_manifest', 'missing_history', 'selected_count', 'created_at', 'cutoff', 'resolution_failed'])
def test_published_ledger_cannot_complete_with_invalid_selected_coverage(db, defect):
    b, job, _, raw = blocked(db)
    run = successful_scoped(db, b, job, raw)
    publish(db, run)
    query(db, 'SET session_replication_role=replica')
    try:
        if defect == 'missing_manifest':
            query(db, 'DELETE FROM control.customer_app_backfill_batch WHERE run_id=%s', (run,))
        elif defect == 'missing_history':
            query(db, 'UPDATE control.customer_app_backfill_batch SET available_observations=0 WHERE run_id=%s', (run,))
        elif defect == 'selected_count':
            query(db, 'UPDATE control.stitching_job SET selected_record_count=2 WHERE job_id=%s', (job,))
        elif defect == 'created_at':
            query(db, "UPDATE control.customer_app_backfill_batch SET batch_created_at='2026-09-01Z' WHERE run_id=%s", (run,))
        elif defect == 'cutoff':
            query(db, "UPDATE control.customer_app_backfill_run SET cutoff='2026-09-01Z' WHERE run_id=%s", (run,))
        else:
            query(db, "UPDATE identity.resolution_run SET status='FAILED' WHERE run_id=%s", (run,))
    finally:
        query(db, 'SET session_replication_role=origin')
    install(db)
    row = pipeline(db, b)
    assert row['transfer_status'] == 'PUBLISHED'  # Stored truth, not a fake transfer failure.
    assert row['overall_status'] not in ('COMPLETED', 'COMPLETED_WITH_WARNINGS')
    assert row['effective_publication_error_code'] is not None


def test_031_rejects_selected_missing_history_without_fabricating_observations(db):
    b, job, _, raw = blocked(db)
    newer = batch(db, created='2026-09-03T00:00:00Z')
    assert upload(db, newer, name='latest') == raw
    # Simulate a selected pre-cutover historical gap, using the real immutable ledger.
    query(db, 'SET session_replication_role=replica')
    query(db, 'DELETE FROM raw.customer WHERE batch_id=%s', (b,))
    query(db, 'SET session_replication_role=origin')
    run = intent(db, b, job)
    workspace = prepare_scoped(db, run, [b, newer])
    record(db, run, workspace, newer, raw)
    install(db)
    with pytest.raises(psycopg2.Error, match='UPLOAD_RESOLUTION_SELECTED_COVERAGE'):
        finish(db, run)
    row = pipeline(db, b)
    assert row['overall_status'] == 'SCOPED_RESOLVING'
    assert row['scoped_resolution_selected_missing_observations'] == 1
    assert row['scoped_resolution_selected_coverage_complete'] is False
    assert query(db, 'SELECT count(*) FROM raw.customer WHERE batch_id=%s', (b,)) == [(0,)]
    assert query(db, 'SELECT count(*) FROM control.customer_identity_transfer WHERE backfill_run_id=%s', (run,)) == [(0,)]


def test_old_unselected_gap_stays_explicit_and_boundary_does_not_float(db):
    b, job, _, raw = blocked(db)
    old = batch(db, created='2026-09-01T00:00:00Z')
    query(db, '''INSERT INTO raw.customer_batch_member(ingestion_batch_id,raw_customer_id,source_system_id,source_customer_id)
        VALUES(%s,%s,1,'same-user')''', (old, raw))
    run = intent(db, b, job)
    workspace = prepare_scoped(db, run, [old, b])
    record(db, run, workspace, b, raw)
    finish(db, run)
    publish(db, run)
    query(db, (MIGRATIONS / '029_customer_upload_historical_coverage.sql').read_text())
    install(db)
    row = pipeline(db, b)
    assert row['overall_status'] == 'COMPLETED'
    assert row['scoped_resolution_app_user_count'] == 1
    assert row['scoped_resolution_last_input_batch_id'] == b
    assert query(db, 'SELECT available_observations FROM control.customer_app_backfill_batch WHERE run_id=%s AND batch_id=%s', (run, old)) == [(0,)]
    later = batch(db, created='2026-09-20T00:00:00Z')
    upload(db, later, name='new arrival')
    assert pipeline(db, b)['scoped_resolution_last_input_batch_id'] == b
    assert pipeline(db, later)['effective_publication_resolution_run_id'] is None
    assert pipeline(db, old)['overall_status'] != 'COMPLETED'
    assert pipeline(db, old)['app_snapshot_batch_missing_observations'] == 1


@pytest.mark.parametrize('warning', ['rejected', 'not_ready'])
def test_scoped_completion_retains_warnings(db, warning):
    b, job, _, raw = blocked(db)
    run = successful_scoped(db, b, job, raw)
    if warning == 'rejected':
        query(db, 'UPDATE control.ingestion_batch SET records_rejected=1 WHERE batch_id=%s', (b,))
    else:
        query(db, 'UPDATE control.stitching_job SET ready_record_count=0 WHERE job_id=%s', (job,))
    install(db)
    assert pipeline(db, b)['overall_status'] == 'TRANSFER_PENDING'
    publish(db, run)
    assert pipeline(db, b)['overall_status'] == 'COMPLETED_WITH_WARNINGS'


@pytest.mark.parametrize('stage,expected', [('landing', 'LANDING'), ('ingestion', 'INGESTING'), ('failed', 'FAILED')])
def test_scoped_success_does_not_erase_prior_stage_truth(db, stage, expected):
    b, job, _, raw = blocked(db)
    run = successful_scoped(db, b, job, raw)
    publish(db, run)
    if stage == 'landing':
        query(db, 'UPDATE control.source_landing_batch SET output_status=NULL WHERE batch_id=(SELECT landing_batch_id FROM control.ingestion_batch WHERE batch_id=%s)', (b,))
    elif stage == 'ingestion':
        query(db, "UPDATE control.ingestion_batch SET status='running' WHERE batch_id=%s", (b,))
    else:
        query(db, "UPDATE control.ingestion_batch SET status='failed' WHERE batch_id=%s", (b,))
    install(db)
    assert pipeline(db, b)['overall_status'] == expected
    if stage != 'landing':
        assert pipeline(db, b, 'customer_upload_resolution_status')['overall_status'] == expected


def test_new_global_retry_does_not_inherit_older_scoped_completion(db):
    b, job, _, raw = blocked(db)
    run = successful_scoped(db, b, job, raw)
    publish(db, run)
    install(db)
    assert pipeline(db, b)['overall_status'] == 'COMPLETED'
    retry = str(uuid4())
    query(db, '''INSERT INTO control.stitching_job(job_id,ingestion_batch_id,attempt,status)
        VALUES(%s,%s,2,'RUNNING')''', (retry, b))
    row = pipeline(db, b)
    assert row['overall_status'] == 'STITCHING'
    assert row['scoped_resolution_attempt'] is None
    assert row['transfer_id'] is None


@pytest.mark.parametrize('lineage', ['ingestion_batch_id', 'landing_batch_id', 'stitching_job_id'])
def test_connector_exclusion_keeps_old_contract_and_clock(db, lineage):
    b, job, _, raw = blocked(db)
    run = successful_scoped(db, b, job, raw)
    publish(db, run)
    target = {'ingestion_batch_id': b, 'stitching_job_id': job,
              'landing_batch_id': query(db, 'SELECT landing_batch_id::text FROM control.ingestion_batch WHERE batch_id=%s', (b,))[0][0]}[lineage]
    query(db, '''CREATE SCHEMA connector; CREATE TABLE connector.sync_run(
        ingestion_batch_id uuid,landing_batch_id uuid,stitching_job_id uuid)''')
    query(db, sql.SQL('INSERT INTO connector.sync_run({}) VALUES(%s)').format(sql.Identifier(lineage)), (target,))
    before = pipeline(db, b)
    install(db)
    after = pipeline(db, b)
    assert {k: after[k] for k in before} == before
    assert after['scoped_resolution_attempt'] is None
    assert query(db, 'SELECT count(*) FROM control.customer_publication_status WHERE ingestion_batch_id=%s', (b,)) == [(0,)]


def test_unlinked_legacy_missing_capture_is_not_repaired_by_app_evidence(db):
    from test_customer_upload_historical_coverage import published_snapshot
    b = batch(db)
    raw = upload(db, b)
    job = str(uuid4())
    query(db, "INSERT INTO control.stitching_job(job_id,ingestion_batch_id,status) VALUES(%s,%s,'SUCCEEDED')", (job, b))
    published_snapshot(db, [b], b, raw)
    query(db, (MIGRATIONS / '029_customer_upload_historical_coverage.sql').read_text())
    before = pipeline(db, b)
    install(db)
    after = pipeline(db, b)
    assert {k: after[k] for k in before} == before
    assert after['overall_status'] == 'TRANSFER_PENDING'
    assert after['app_snapshot_status'] == 'PUBLISHED'
    assert after['effective_publication_resolution_run_id'] is None


@pytest.mark.parametrize('state,expected', [('PENDING', 'TRANSFER_PENDING'), ('RUNNING', 'TRANSFERRING'),
    ('FAILED', 'TRANSFER_FAILED'), ('PUBLISHED', 'COMPLETED')])
def test_025_safe_original_transfer_contract_is_unchanged(db, state, expected):
    b = batch(db)
    raw = upload(db, b)
    _, run = global_job(db, b, raw, mixed=False)
    query(db, 'UPDATE control.customer_identity_transfer SET status=%s WHERE transfer_id=%s', (state, run))
    before = pipeline(db, b)
    install(db)
    after = pipeline(db, b)
    assert {k: after[k] for k in before} == before
    assert after['overall_status'] == expected
    assert after['effective_publication_origin'] == 'GLOBAL_STITCHING'
    assert after['effective_publication_resolution_run_id'] == run


def test_025_capture_issue_without_resolution_and_history_outcome_are_preserved(db):
    b = batch(db)
    job = str(uuid4())
    query(db, "INSERT INTO control.stitching_job(job_id,ingestion_batch_id,status) VALUES(%s,%s,'RUNNING')", (job, b))
    query(db, "UPDATE control.stitching_job SET status='SUCCEEDED' WHERE job_id=%s", (job,))
    query(db, '''INSERT INTO control.customer_history_recovery_outcome(attempt_id,ingestion_batch_id,status,reason_code,evidence)
        VALUES(gen_random_uuid(),%s,'FAILED','MISSING_HISTORICAL_MEMBERSHIP','{}')''', (b,))
    before = pipeline(db, b)
    assert before['overall_status'] == 'TRANSFER_BLOCKED'
    install(db)
    after = pipeline(db, b)
    assert {k: after[k] for k in before} == before
    assert after['transfer_error_code'] == 'NO_SUCCESSFUL_RESOLUTION'
    assert after['history_recovery_status'] == 'FAILED'


@pytest.mark.parametrize('historical', [False, True])
def test_existing_view_oids_columns_acl_extensions_and_narrow_reader_preserved(db, historical):
    b, job, _, raw = blocked(db)
    run = successful_scoped(db, b, job, raw)
    publish(db, run)
    if historical:
        query(db, (MIGRATIONS / '029_customer_upload_historical_coverage.sql').read_text())
    role = 'scoped_status_reader_' + uuid4().hex
    query(db, sql.SQL('CREATE ROLE {}; GRANT USAGE ON SCHEMA control TO {}').format(*[sql.Identifier(role)]*2))
    query(db, sql.SQL('GRANT SELECT ON control.customer_pipeline_status,control.customer_publication_status TO {}').format(sql.Identifier(role)))
    for view in ('customer_pipeline_status', 'customer_publication_status'):
        definition = query(db, 'SELECT pg_get_viewdef(%s::regclass,true)', ('control.'+view,))[0][0].rstrip(';\n ')
        query(db, f'CREATE OR REPLACE VIEW control.{view} AS SELECT p.*,42 AS future_extension,7 AS transfer_future_extension FROM ('+definition+') p')
    catalog = """SELECT c.relname,c.oid,c.relowner,c.relacl,a.attname,a.atttypid,a.atttypmod,a.attnum
        FROM pg_class c JOIN pg_attribute a ON a.attrelid=c.oid
        WHERE c.oid=ANY(ARRAY['control.customer_pipeline_status'::regclass,'control.customer_publication_status'::regclass])
        AND a.attnum>0 AND NOT a.attisdropped ORDER BY c.relname,a.attnum"""
    before = query(db, catalog)
    stored = stored_truth(db)
    try:
        install(db)
        after = query(db, catalog)
        assert [r for r in after if (r[0], r[4]) in {(old[0], old[4]) for old in before}] == before
        assert stored_truth(db) == stored
        query(db, sql.SQL('SET ROLE {}').format(sql.Identifier(role)))
        for view in ('customer_pipeline_status', 'customer_publication_status'):
            row = pipeline(db, b, view)
            assert row['overall_status'] == 'COMPLETED'
            assert row['future_extension'] == 42
        assert query(db, 'SELECT count(*) FROM control.customer_upload_resolution_status') == [(1,)]
        for table in ('control.customer_upload_resolution_attempt', 'control.customer_app_backfill_batch',
                      'control.customer_app_backfill_record'):
            with pytest.raises(psycopg2.errors.InsufficientPrivilege):
                query(db, 'SELECT * FROM '+table)
        with pytest.raises(psycopg2.errors.InsufficientPrivilege):
            query(db, 'SELECT control.customer_app_scope_status(%s)', (b,))
    finally:
        query(db, 'ROLLBACK; RESET ROLE')
        query(db, sql.SQL('DROP OWNED BY {}; DROP ROLE {}').format(*[sql.Identifier(role)]*2))


def test_admin_install_keeps_nonadmin_public_view_owners_readable(db):
    b, job, _, raw = blocked(db)
    run = successful_scoped(db, b, job, raw)
    publish(db, run)
    owner = 'scoped_status_owner_' + uuid4().hex
    query(db, sql.SQL('CREATE ROLE {}; GRANT USAGE ON SCHEMA control,raw,staging,identity,quality TO {}').format(*[sql.Identifier(owner)]*2))
    query(db, sql.SQL('GRANT SELECT ON ALL TABLES IN SCHEMA control,raw,staging,identity,quality TO {}').format(sql.Identifier(owner)))
    for view in ('customer_pipeline_status', 'customer_publication_status'):
        query(db, sql.SQL('ALTER VIEW control.{} OWNER TO {}').format(sql.Identifier(view), sql.Identifier(owner)))
    try:
        install(db)
        query(db, sql.SQL('SET ROLE {}').format(sql.Identifier(owner)))
        assert pipeline(db, b)['overall_status'] == 'COMPLETED'
    finally:
        query(db, 'RESET ROLE; ALTER VIEW control.customer_pipeline_status OWNER TO CURRENT_USER; ALTER VIEW control.customer_publication_status OWNER TO CURRENT_USER')
        query(db, sql.SQL('DROP OWNED BY {}; DROP ROLE {}').format(*[sql.Identifier(owner)]*2))


def test_missing_031_dependency_fails_atomically(db):
    before = query(db, "SELECT pg_get_viewdef('control.customer_publication_status'::regclass)")
    query(db, 'DROP TABLE control.customer_upload_resolution_attempt CASCADE')
    with pytest.raises(psycopg2.errors.UndefinedTable):
        install(db)
    query(db, 'ROLLBACK')
    assert query(db, "SELECT pg_get_viewdef('control.customer_publication_status'::regclass)") == before
    assert query(db, "SELECT to_regclass('control.customer_upload_resolution_status')") == [(None,)]


def test_scoped_read_plan_has_no_raw_history_scan_or_per_row_subplan(db):
    b, job, _, raw = blocked(db)
    run = successful_scoped(db, b, job, raw)
    publish(db, run)
    install(db)
    plan = query(db, '''EXPLAIN (ANALYZE,BUFFERS,FORMAT JSON) SELECT *
        FROM control.customer_upload_resolution_status''')[0][0][0]
    def nodes(node):
        yield node
        for child in node.get('Plans', []):
            yield from nodes(child)
    all_nodes = list(nodes(plan['Plan']))
    assert not any(n.get('Parent Relationship') == 'SubPlan' for n in all_nodes)
    assert not any(n.get('Relation Name') in ('customer', 'customer_current', 'customer_batch_member',
        'customer_app_backfill_record', 'customer_identity_export') for n in all_nodes)
    assert plan['Plan']['Actual Rows'] == 1


@pytest.mark.parametrize('state,expected', [('FAILED', 'FAILED'), ('PENDING', 'VALIDATION_PENDING'), ('RUNNING', 'VALIDATING')])
def test_actual_015_validation_precedes_scoped_publication(db, state, expected):
    b, job, _, raw = blocked(db)
    run = successful_scoped(db, b, job, raw)
    publish(db, run)
    query(db, '''INSERT INTO quality.customer_validation_run(landing_batch_id,ingestion_batch_id,
        stitching_job_id,attempt,run_status,ruleset_version)
        SELECT landing_batch_id,batch_id,%s,1,%s,'test' FROM control.ingestion_batch WHERE batch_id=%s''', (job, state, b))
    install(db)
    assert pipeline(db, b)['overall_status'] == expected


def test_new_intent_cannot_fall_back_to_older_published_scoped_transfer(db):
    b, job, _, raw = blocked(db)
    old = successful_scoped(db, b, job, raw)
    publish(db, old)
    # 031 forbids retry after success; fault injection tests projection independently
    # and protects any future authorized retry policy from stale completion.
    latest = str(uuid4())
    query(db, 'SET session_replication_role=replica')
    try:
        query(db, '''INSERT INTO control.customer_upload_resolution_attempt
            (stitching_job_id,attempt,run_id,ingestion_batch_id,app_id) VALUES(%s,2,%s,%s,%s)''', (job, latest, b, APP))
    finally:
        query(db, 'SET session_replication_role=origin')
    install(db)
    row = pipeline(db, b)
    assert row['overall_status'] == 'SCOPED_RESOLUTION_PENDING'
    assert row['effective_publication_resolution_run_id'] == latest
    assert row['transfer_id'] is None


@pytest.mark.parametrize('state,public', [('RUNNING', 'SCOPED_RESOLVING'),
    ('RETRY_WAIT', 'SCOPED_RESOLUTION_RETRY_WAIT'), ('FAILED', 'SCOPED_RESOLUTION_FAILED')])
def test_preledger_continuation_status_is_real_without_fake_resolution(continuation, state, public):
    db = continuation
    b, job, original, _ = blocked(db)
    run = intent(db, b, job)
    query(db, '''INSERT INTO control.customer_upload_resolution_attempt_status
        (run_id,status,retry_count,next_attempt_at,error_code,updated_at)
        VALUES(%s,%s,1,CASE WHEN %s IN ('RUNNING','RETRY_WAIT') THEN '2099-01-01Z'::timestamptz ELSE NULL END,
            CASE WHEN %s='RUNNING' THEN NULL ELSE 'INPUT_UNAVAILABLE' END,'2098-01-01Z')''', (run, state, state, state))
    install(db)
    for view in ('customer_pipeline_status', 'customer_publication_status'):
        row = pipeline(db, b, view)
        assert row['overall_status'] == public
        assert row['scoped_resolution_status'] == state
        assert row['scoped_resolution_continuation_status'] == state
        assert row['scoped_resolution_error_code'] == (None if state == 'RUNNING' else 'INPUT_UNAVAILABLE')
        assert row['scoped_resolution_retry_count'] == 1
        assert row['scoped_resolution_next_attempt_at'] == ('2099-01-01T00:00:00+00:00' if state in ('RUNNING', 'RETRY_WAIT') else None)
        assert row['transfer_id'] is None
        assert row['resolution_run_id'] == original
        assert row['effective_publication_updated_at'].startswith('2098-01-01')
    assert query(db, 'SELECT count(*) FROM identity.resolution_run WHERE run_id=%s', (run,)) == [(0,)]


def test_committed_success_outbox_precedes_failed_client_acknowledgement(continuation):
    db = continuation
    b, job, _, raw = blocked(db)
    run = successful_scoped(db, b, job, raw)
    # Only a diagnostic fault: producer should reconcile before storing FAILED.
    query(db, 'SET session_replication_role=replica')
    try:
        query(db, '''INSERT INTO control.customer_upload_resolution_attempt_status
            (run_id,status,retry_count,error_code) VALUES(%s,'FAILED',1,'ACK_LOST')''', (run,))
    finally:
        query(db, 'SET session_replication_role=origin')
    install(db)
    row = pipeline(db, b)
    assert row['overall_status'] == 'TRANSFER_PENDING'
    assert row['scoped_resolution_status'] == 'SUCCEEDED'
    assert row['scoped_resolution_error_code'] is None
    publish(db, run)
    assert pipeline(db, b)['overall_status'] == 'COMPLETED'


def test_terminal_preledger_failure_allows_explicit_new_intent(continuation):
    db = continuation
    b, job, _, _ = blocked(db)
    first = intent(db, b, job)
    query(db, '''INSERT INTO control.customer_upload_resolution_attempt_status
        (run_id,status,retry_count,error_code) VALUES(%s,'FAILED',1,'INPUT_UNAVAILABLE')''', (first,))
    second = intent(db, b, job, attempt=2)
    install(db)
    row = pipeline(db, b)
    assert row['overall_status'] == 'SCOPED_RESOLUTION_PENDING'
    assert row['effective_publication_resolution_run_id'] == second
    assert row['scoped_resolution_error_code'] is None


def test_preledger_journal_success_without_ledger_never_completes(continuation):
    db = continuation
    b, job, _, _ = blocked(db)
    run = intent(db, b, job)
    query(db, 'SET session_replication_role=replica')
    try:
        query(db, '''INSERT INTO control.customer_upload_resolution_attempt_status
            (run_id,status,retry_count) VALUES(%s,'SUCCEEDED',0)''', (run,))
    finally:
        query(db, 'SET session_replication_role=origin')
    install(db)
    assert pipeline(db, b)['overall_status'] == 'SCOPED_RESOLUTION_BLOCKED'
    assert pipeline(db, b)['transfer_id'] is None


def test_complete_selected_older_upload_can_bind_latest_app_without_relabeling_history(db):
    b, job, original, raw = blocked(db)
    newer = batch(db, created='2026-09-03T00:00:00Z')
    upload(db, newer, name='later values')
    run = intent(db, b, job)
    workspace = prepare_scoped(db, run, [b, newer])
    record(db, run, workspace, newer, raw)
    finish(db, run)
    publish(db, run)
    install(db)
    row = pipeline(db, b)
    assert row['overall_status'] == 'COMPLETED'
    assert row['resolution_run_id'] == original
    assert row['scoped_resolution_last_input_batch_id'] == newer
    assert row['scoped_resolution_selected_expected_members'] == 1
    assert row['scoped_resolution_selected_coverage_complete'] is True
    assert row['effective_publication_scope'] == 'LATEST_APP_USERS_AT_CUTOFF'
    assert query(db, 'SELECT count(*) FROM control.customer_identity_export WHERE transfer_id=%s', (run,)) == [(1,)]
    assert query(db, 'SELECT first_name FROM raw.customer WHERE batch_id=%s', (b,)) == [('local',)]
    assert query(db, 'SELECT first_name FROM raw.customer WHERE batch_id=%s', (newer,)) == [('later values',)]


def test_terminal_continuation_failure_precedes_nonterminal_building_ledger(continuation):
    db = continuation
    b, job, _, _ = blocked(db)
    run = intent(db, b, job)
    prepare_scoped(db, run, [b])
    query(db, '''INSERT INTO control.customer_upload_resolution_attempt_status
        (run_id,status,retry_count,error_code) VALUES(%s,'FAILED',3,'RETRY_EXHAUSTED')''', (run,))
    install(db)
    row = pipeline(db, b)
    assert row['overall_status'] == 'SCOPED_RESOLUTION_FAILED'
    assert row['scoped_resolution_error_code'] == 'RETRY_EXHAUSTED'


def test_missing_033_dependency_is_atomic_deployment_gate(db):
    query(db, 'DROP TABLE control.customer_upload_resolution_attempt_status')
    before = query(db, "SELECT pg_get_viewdef('control.customer_publication_status'::regclass)")
    with pytest.raises(psycopg2.errors.UndefinedTable):
        install(db)
    query(db, 'ROLLBACK')
    assert query(db, "SELECT pg_get_viewdef('control.customer_publication_status'::regclass)") == before
    assert query(db, "SELECT to_regclass('control.customer_upload_resolution_status')") == [(None,)]
