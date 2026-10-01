"""Upload-only acceptance against a strictly owned local PostgreSQL database.

CUSTOMER_UPLOAD_SCOPE_TEST_DSN must be loopback:61577/poststitch_upload_scope_test.
Never reset the parent schema/status/worker fixture databases.
"""
import os

import psycopg2
import pytest

from test_customer_identity_publication import (
    ROOT, batch, connector_batch, execute, finish, member, scalar, setup_database, transfer, uid,
)


@pytest.fixture
def db():
    dsn = os.getenv('CUSTOMER_UPLOAD_SCOPE_TEST_DSN')
    if not dsn:
        pytest.skip('Set CUSTOMER_UPLOAD_SCOPE_TEST_DSN for owned upload-only fixture')
    params = psycopg2.extensions.parse_dsn(dsn)
    assert (params.get('host'), params.get('port'), params.get('dbname')) == (
        '127.0.0.1', '61577', 'poststitch_upload_scope_test')
    conn = psycopg2.connect(dsn)
    try:
        assert scalar(conn, 'SHOW server_encoding') == 'UTF8'
        setup_database(conn, with_connector=True)
        yield conn
    finally:
        conn.close()


def test_upload_auto_scopes_all_literal_apps_without_platform_workspaces(db):
    apps = ['1282009980020330', '222', '000222', '0', '9' * 25]
    scopes = {}
    for i, app in enumerate(apps):
        for attempt in range(2):
            b = batch(db, app=app, source=i + 1)
            m = member(db, b, user=f'original-source-{attempt}')
            finish(db, b)
            db.commit()
            t = transfer(db, b)
            assert t['status'] == 'PENDING', t
            assert t['app_id'] == app
            if app in scopes:
                assert scopes[app] == t['workspace_id']
            scopes[app] = t['workspace_id']
            payload = scalar(db, 'SELECT payload FROM control.customer_identity_export WHERE transfer_id=%s', (b['run'],))
            assert payload['workspace_id'] == t['workspace_id']
            assert payload['app_id'] == app
            assert payload['user_id'] == payload['profile']['customer_id'] == m['user']
    assert len(set(scopes.values())) == len(apps)
    assert scalar(db, 'SELECT count(*) FROM platform.workspace') == 0
    assert scalar(db, 'SELECT count(*) FROM control.customer_identity_upload_scope') == len(apps)
    assert scalar(db, "SELECT count(*) FROM pg_constraint WHERE conrelid='control.customer_identity_upload_scope'::regclass AND contype='f'") == 0


@pytest.mark.parametrize('mutation', [
    'UPDATE control.customer_identity_upload_scope SET workspace_id=gen_random_uuid()',
    "UPDATE control.customer_identity_upload_scope SET app_id='123'",
    'DELETE FROM control.customer_identity_upload_scope',
    'TRUNCATE control.customer_identity_upload_scope',
])
def test_upload_scope_registry_cannot_be_remapped_or_reused(db, mutation):
    b = batch(db)
    finish(db, b)
    db.commit()
    with pytest.raises(psycopg2.Error, match='immutable'):
        execute(db, mutation)
    db.rollback()


@pytest.mark.parametrize('evidence', ['normal', 'file_ref', 'landing_only', 'job_only', 'retry', 'no_resolution'])
def test_connector_never_captures_or_auto_registers_even_with_file_ref(db, evidence):
    workspace = uid()
    execute(db, "INSERT INTO platform.workspace(workspace_id,slug,name) VALUES (%s,'connector','Connector')", (workspace,))
    b = connector_batch(db, workspace)
    if evidence != 'normal':
        ref = '2026-08-26-083337-222-123456789abc'
        execute(db, 'UPDATE control.ingestion_batch SET batch_ref=%s', (ref,))
        execute(db, 'UPDATE control.source_landing_batch SET batch_ref=%s', (ref,))
    if evidence in ('landing_only', 'job_only'):
        other = batch(db, app='333', source=2)
        execute(db, 'UPDATE connector.sync_run SET ingestion_batch_id=%s', (other['batch'],))
        column, value = ('stitching_job_id', other['job']) if evidence == 'landing_only' else ('landing_batch_id', other['landing'])
        execute(db, f'UPDATE connector.sync_run SET {column}=%s', (value,))
    elif evidence == 'retry':
        retry = uid()
        execute(db, "INSERT INTO control.stitching_job(job_id,ingestion_batch_id,status,attempt) VALUES (%s,%s,'RUNNING',2)", (retry, b['batch']))
        execute(db, 'UPDATE identity.resolution_run SET stitching_job_id=%s WHERE run_id=%s', (retry, b['run']))
        b['job'] = retry
    if evidence == 'no_resolution':
        execute(db, 'DELETE FROM identity.resolution_run')
        execute(db, "UPDATE control.stitching_job SET status='SUCCEEDED' WHERE job_id=%s", (b['job'],))
    else:
        member(db, b)
        finish(db, b)
    db.commit()
    assert transfer(db, b) is None
    assert scalar(db, 'SELECT count(*) FROM control.customer_identity_capture_issue') == 0
    assert scalar(db, 'SELECT count(*) FROM control.customer_identity_export') == 0
    assert scalar(db, 'SELECT count(*) FROM control.customer_identity_upload_scope') == 0
    # The direct private entry must also ignore connectors before run lookup.
    execute(db, 'SELECT control.capture_customer_identity_snapshot(%s)', (b['job'],))
    assert transfer(db, b) is None


@pytest.mark.parametrize('problem', ['none', 'file_ref', 'retry', 'missing_resolution', 'stale_issue', 'stale_outbox'])
def test_connector_sql_status_is_unchanged_and_has_no_transfer_metadata(db, problem):
    from test_customer_publication_status import status

    workspace = uid()
    execute(db, "INSERT INTO platform.workspace(workspace_id,slug,name) VALUES (%s,'connector','Connector')", (workspace,))
    b = connector_batch(db, workspace)
    if problem == 'file_ref':
        ref = '2026-08-26-083337-222-123456789abc'
        execute(db, 'UPDATE control.ingestion_batch SET batch_ref=%s', (ref,))
        execute(db, 'UPDATE control.source_landing_batch SET batch_ref=%s', (ref,))
    if problem == 'retry':
        retry = uid()
        execute(db, "INSERT INTO control.stitching_job(job_id,ingestion_batch_id,status,attempt) VALUES (%s,%s,'RUNNING',2)", (retry, b['batch']))
        execute(db, 'UPDATE identity.resolution_run SET stitching_job_id=%s WHERE run_id=%s', (retry, b['run']))
        b['job'] = retry
    finish(db, b)
    if problem == 'missing_resolution':
        execute(db, 'DELETE FROM identity.resolution_run')
    if problem == 'stale_issue':
        execute(db, "INSERT INTO control.customer_identity_capture_issue(stitching_job_id,error_code,created_at) VALUES (%s,'CAPTURE_SQL_ERROR','2099-01-01Z')", (b['job'],))
    if problem == 'stale_outbox':
        execute(db, "INSERT INTO control.customer_identity_transfer(transfer_id,resolution_run_id,stitching_job_id,ingestion_batch_id,status,updated_at) VALUES (%s,%s,%s,%s,'BLOCKED','2099-01-01Z')", (b['run'], b['run'], b['job'], b['batch']))
    before = status(db, b['batch'])
    db.commit()
    execute(db, (ROOT / 'database/migrations/025_customer_publication_status.sql').read_text())
    row = status(db, b['batch'])
    assert row['overall_status'] == before['overall_status'] == 'COMPLETED'
    assert row['updated_at'] == before['updated_at']
    assert all(value is None for key, value in row.items() if key.startswith('transfer_'))
    assert execute(db, 'SELECT * FROM control.customer_publication_status WHERE ingestion_batch_id=%s', (b['batch'],)) == []
    reconciliation = (ROOT / 'database/queries/verify_customer_identity_publication.sql').read_text()
    missing = reconciliation[reconciliation.index('SELECT j.job_id AS stitching_job_id'):].split(';')[0]
    assert execute(db, missing) == []


@pytest.mark.parametrize('ref', [
    '2026-08-26-083337-222-123456789abc ',
    ' 2026-08-26-083337-222-123456789abc',
    '2026-08-26-083337-222-123456789ABC',
    '2026-08-26-083337-222-123456789ab',
    '2026-08-26-083337-+222-123456789abc',
])
def test_bad_literal_upload_ref_has_clear_blocked_reason(db, ref):
    # Simulate historical/corrupt lineage beyond the landing ingress guard.
    execute(db, 'ALTER TABLE control.source_landing_batch DROP CONSTRAINT source_landing_batch_batch_ref_check')
    b = batch(db, ref=ref)
    member(db, b)
    finish(db, b)
    db.commit()
    assert transfer(db, b)['error_code'] == 'INVALID_UPLOAD_BATCH_REF'
    assert scalar(db, 'SELECT count(*) FROM control.customer_identity_upload_scope') == 0


@pytest.mark.parametrize('origin', ['other_app', 'connector', 'unknown', 'same_app'])
@pytest.mark.parametrize('relationship', ['entity', 'history', 'contributor'])
def test_complete_component_ownership_remains_fail_closed(db, origin, relationship):
    b = batch(db, app='000222')
    m = member(db, b, user='selected-original-id')
    if origin == 'connector':
        workspace = uid()
        execute(db, "INSERT INTO platform.workspace(workspace_id,slug,name) VALUES (%s,'connector','Connector')", (workspace,))
        other = connector_batch(db, workspace)
        # Even same app-looking connector provenance is not upload evidence.
        ref = '2026-08-26-083337-000222-123456789abc'
        execute(db, 'UPDATE control.ingestion_batch SET batch_ref=%s WHERE batch_id=%s', (ref, other['batch']))
        execute(db, 'UPDATE control.source_landing_batch SET batch_ref=%s WHERE batch_id=%s', (ref, other['landing']))
    else:
        other = batch(db, app='000222' if origin == 'same_app' else '222',
                      ref='unknown-provenance' if origin == 'unknown' else None)
    if relationship == 'entity':
        member(db, other, user='nonselected-canonical-contributor', entity=m['entity'], run=b['run'])
    else:
        execute(db, '''INSERT INTO raw.customer_batch_member(ingestion_batch_id,raw_customer_id,source_system_id,source_customer_id)
            VALUES (%s,%s,%s,%s)''', (other['batch'], m['raw'], b['source'], m['user']))
        if relationship == 'contributor':
            execute(db, 'UPDATE staging.customer_profile SET batch_id=%s WHERE raw_customer_id=%s', (other['batch'], m['raw']))
    unrelated = batch(db, app='777', source=3)
    member(db, unrelated, user='unrelated-not-selected')
    finish(db, b)
    db.commit()
    t = transfer(db, b)
    if origin == 'same_app':
        assert (t['status'], t['row_count']) == ('PENDING', 1)
        assert execute(db, 'SELECT user_id FROM control.customer_identity_export') == [('selected-original-id',)]
        assert execute(db, 'SELECT app_id FROM control.customer_identity_upload_scope') == [('000222',)]
    else:
        assert (t['status'], t['error_code'], t['row_count']) == ('BLOCKED', 'MIXED_OR_UNKNOWN_OWNER', 0)
        assert scalar(db, 'SELECT count(*) FROM control.customer_identity_export') == 0
        # Neither selected nor component registrations escape a rejected capture.
        assert scalar(db, 'SELECT count(*) FROM control.customer_identity_upload_scope') == 0


def test_registration_is_atomic_with_snapshot_and_outer_commit(db):
    b = batch(db)
    member(db, b)
    db.commit()
    observer = psycopg2.connect(db.dsn)
    try:
        finish(db, b)
        assert scalar(db, 'SELECT count(*) FROM control.customer_identity_upload_scope') == 1
        assert scalar(observer, 'SELECT count(*) FROM control.customer_identity_upload_scope') == 0
        db.rollback()
        observer.rollback()
        assert scalar(observer, 'SELECT count(*) FROM control.customer_identity_upload_scope') == 0
        assert scalar(observer, 'SELECT count(*) FROM control.customer_identity_transfer') == 0
        finish(db, b)
        db.commit()
        observer.rollback()
        scope = scalar(observer, 'SELECT workspace_id FROM control.customer_identity_upload_scope')
        finish(db, b)
        db.commit()
        assert transfer(db, b)['workspace_id'] == scope
    finally:
        observer.close()


def test_partial_export_failure_rolls_back_new_registry_rows(db):
    b = batch(db)
    member(db, b)
    execute(db, """CREATE FUNCTION control.fail_synthetic_export() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION 'private'; END $$;
        CREATE TRIGGER fail_synthetic_export AFTER INSERT ON control.customer_identity_export
        FOR EACH ROW EXECUTE FUNCTION control.fail_synthetic_export()""")
    finish(db, b)
    db.commit()
    assert transfer(db, b)['error_code'] == 'CAPTURE_SQL_ERROR'
    assert scalar(db, 'SELECT count(*) FROM control.customer_identity_upload_scope') == 0


def test_upload_missing_status_join_still_fails_closed(db):
    from test_customer_publication_status import status

    b = batch(db)
    finish(db, b)
    execute(db, '''DROP VIEW control.customer_pipeline_status;
        CREATE VIEW control.customer_pipeline_status AS
        SELECT ingestion_batch_id,job_id AS stitching_job_id,
            '11111111-1111-1111-1111-111111111111'::uuid AS resolution_run_id,
            'COMPLETED'::text AS overall_status,updated_at
        FROM control.stitching_job''')
    db.commit()
    execute(db, (ROOT / 'database/migrations/025_customer_publication_status.sql').read_text())
    assert status(db, b['batch'])['overall_status'] == 'TRANSFER_PENDING'
