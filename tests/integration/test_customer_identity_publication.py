"""Real PostgreSQL capture tests. Destructive ONLY in poststitch_schema_test.

Create that isolated UTF8 database from template0, then set
CUSTOMER_IDENTITY_SCHEMA_TEST_DSN='host=127.0.0.1 port=61577 user=postgres dbname=poststitch_schema_test'.
Uses the actual pinned stitcher catalog and identity/tracking migrations.
Sibling repositories may be relocated with IDENTITY_STITCHER_ROOT / CONNECTOR_API_ROOT.
"""
from __future__ import annotations

import ast
import os
from pathlib import Path
import time
from uuid import uuid4

import psycopg2
from psycopg2.extensions import parse_dsn
from psycopg2.extras import RealDictCursor
import pytest

ROOT = Path(__file__).resolve().parents[2]
STITCHER = Path(os.getenv('IDENTITY_STITCHER_ROOT', str(ROOT.parent / 'fargate-user-stitching')))
API = Path(os.getenv('CONNECTOR_API_ROOT', str(ROOT.parent / 'journey-builder-api')))
MIGRATION = ROOT / 'database/migrations/024_customer_identity_publication.sql'
APP = '1282009980020330'


def uid():
    return str(uuid4())


def execute(db, sql, args=None):
    with db.cursor() as q:
        q.execute(sql, args)
        if q.description:
            return q.fetchall()


def scalar(db, sql, args=None):
    return execute(db, sql, args)[0][0]


def setup_database(c, *, with_connector=False):
    """Reusable destructive local bootstrap for schema + parent worker tests."""
    assert c.info.host == '127.0.0.1'
    assert c.info.dbname in {'poststitch_schema_test', 'poststitch_test'} or (
        c.info.dbname == 'poststitch_upload_scope_test' and c.info.port == 61577
    )
    c.rollback()
    c.autocommit = True
    execute(c, 'DROP SCHEMA IF EXISTS connector CASCADE; DROP SCHEMA IF EXISTS platform CASCADE')
    execute(c, (STITCHER / 'tests/fixtures/repository_catalog.sql').read_text())
    execute(c, (STITCHER / 'src/identity_stitcher/sql/001_identity_model.sql').read_text())
    execute(c, (ROOT / 'database/migrations/014_pipeline_tracking.sql').read_text())
    if with_connector:
        tree = ast.parse((API / 'alembic/versions/001_connector_control_plane.py').read_text())
        ddl = next(ast.literal_eval(node.value) for node in tree.body if isinstance(node,ast.Assign)
                   and any(isinstance(t,ast.Name) and t.id == 'DDL_STATEMENTS' for t in node.targets))
        execute(c,ddl)
    assert MIGRATION.exists(), 'durable customer identity publication migration missing'
    execute(c, MIGRATION.read_text())
    c.autocommit = False


@pytest.fixture
def db():
    dsn = os.getenv('CUSTOMER_IDENTITY_SCHEMA_TEST_DSN')
    if not dsn:
        pytest.skip('Set CUSTOMER_IDENTITY_SCHEMA_TEST_DSN for isolated local PostgreSQL')
    params = parse_dsn(dsn)
    assert params.get('host') == '127.0.0.1'
    assert params.get('dbname') == 'poststitch_schema_test' or (
        params.get('dbname') == 'poststitch_upload_scope_test' and params.get('port') == '61577')
    c = psycopg2.connect(dsn)
    assert c.info.dbname == params['dbname']
    try:
        setup_database(c)
        yield c
    finally:
        c.close()


def register(db, app=APP, workspace=None):
    """Optional explicit INTERNAL fixture scope; production capture auto-creates it."""
    workspace = workspace or uid()
    execute(db, 'INSERT INTO control.customer_identity_upload_scope(app_id,workspace_id) VALUES (%s,%s)', (app, workspace))
    return workspace


def batch(db, *, app=APP, source=1, ref=None):
    landing, ingestion, job, run = (uid() for _ in range(4))
    ref = ref or f'2026-08-26-083337-{app}-{uuid4().hex[:12]}'
    execute(db, "INSERT INTO control.source_system(source_system_id,source_code,source_name) VALUES (%s,%s,'synthetic') ON CONFLICT DO NOTHING", (source, f'synthetic-{source}'))
    execute(db, "INSERT INTO control.source_landing_batch(batch_id,source_code,batch_ref,output_status) VALUES (%s,%s,%s,'PUBLISHED')", (landing, f'synthetic-{source}', ref))
    execute(db, "INSERT INTO control.ingestion_batch(batch_id,source_system_id,landing_batch_id,batch_ref,status) VALUES (%s,%s,%s,%s,'completed')", (ingestion, source, landing, ref))
    execute(db, "INSERT INTO control.stitching_job(job_id,ingestion_batch_id,status) VALUES (%s,%s,'RUNNING')", (job, ingestion))
    execute(db, "INSERT INTO identity.resolution_run(run_id,stitching_job_id,status,normalization_version,match_threshold) VALUES (%s,%s,'RUNNING','synthetic',0.9)", (run, job))
    return dict(landing=landing, batch=ingestion, job=job, run=run, source=source)


def member(db, b, user='source-user', *, entity=None, run=None):
    raw, standardized = uid(), uid()
    entity = entity or uid()
    run = run or b['run']
    execute(db, "INSERT INTO raw.customer_current(raw_customer_id,batch_id,source_system_id,source_customer_id) VALUES (%s,%s,%s,%s)", (raw,b['batch'],b['source'],user))
    execute(db, "INSERT INTO raw.customer_batch_member(ingestion_batch_id,raw_customer_id,source_system_id,source_customer_id) VALUES (%s,%s,%s,%s)", (b['batch'],raw,b['source'],user))
    execute(db, """INSERT INTO staging.customer_profile(standardized_record_id,raw_customer_id,batch_id,source_system_id,source_customer_id,first_name_raw,source_updated_at,standardized_at)
        VALUES (%s,%s,%s,%s,%s,'Synthetic','2026-09-01T12:00:00+07','2026-09-02T12:00:00+07')""", (standardized,raw,b['batch'],b['source'],user))
    execute(db, """INSERT INTO identity.customer_entity(customer_entity_id,canonical_first_name,canonical_email,last_resolution_run_id,first_seen_at,last_seen_at)
        VALUES (%s,'Canonical','synthetic@example.test',%s,'2026-09-01T12:00:00+07','2026-09-02T12:00:00+07')
        ON CONFLICT (customer_entity_id) DO UPDATE SET member_count=identity.customer_entity.member_count+1""", (entity,run))
    execute(db, """INSERT INTO identity.customer_entity_member(standardized_record_id,customer_entity_id,source_system_id,source_customer_id,match_method,resolution_run_id)
        VALUES (%s,%s,%s,%s,'singleton',%s)""", (standardized,entity,b['source'],user,run))
    return dict(raw=raw, standardized=standardized, entity=entity, user=user)


def finish(db, b):
    execute(db, "UPDATE identity.resolution_run SET status='SUCCEEDED',completed_at=clock_timestamp() WHERE run_id=%s", (b['run'],))
    execute(db, """UPDATE control.stitching_job SET status='SUCCEEDED',completed_at=clock_timestamp(),
        selected_record_count=(SELECT count(*) FROM raw.customer_batch_member WHERE ingestion_batch_id=%s),
        member_record_count=(SELECT count(*) FROM raw.customer_batch_member WHERE ingestion_batch_id=%s)
        WHERE job_id=%s""", (b['batch'],b['batch'],b['job']))


def transfer(db, b):
    with db.cursor(cursor_factory=RealDictCursor) as q:
        q.execute('SELECT * FROM control.customer_identity_transfer WHERE stitching_job_id=%s', (b['job'],))
        return q.fetchone()


def test_successful_file_stitch_captures_both_outputs_at_source_user_grain(db):
    workspace = register(db)
    b = batch(db)
    m = member(db,b,'z-user')
    member(db,b,'a-user',entity=m['entity'])
    finish(db,b)
    db.commit()
    t = transfer(db,b)
    assert t['status'] == 'PENDING', t
    assert (t['resolution_run_id'],t['workspace_id'],t['app_id'],t['row_count']) == (b['run'],workspace,APP,2)
    assert t['source_sha256'] is None and t['attempts'] == 0
    rows = execute(db,'SELECT row_number,user_id,payload FROM control.customer_identity_export ORDER BY row_number')
    assert [r[:2] for r in rows] == [(1,'a-user'),(2,'z-user')]
    for _, user, payload in rows:
        assert payload['user_id'] == payload['profile']['customer_id'] == user
        assert payload['customer_entity_id'] == m['entity']
        assert payload['profile']['first_name'] == 'Canonical'
        assert payload['profile']['customer_key_id'] is None
        assert payload['profile']['profile_source_record_id'] is None
        assert payload['profile']['standardized_at'] == '2026-09-02T05:00:00.000000Z'


def test_malformed_upload_ref_blocks_export_without_failing_stitching(db):
    b = batch(db, ref='malformed-upload')
    member(db,b)
    finish(db,b)
    db.commit()
    assert scalar(db,'SELECT status FROM control.stitching_job') == 'SUCCEEDED'
    assert scalar(db,'SELECT status FROM identity.resolution_run') == 'SUCCEEDED'
    t = transfer(db,b)
    assert t['status'] == 'BLOCKED' and t['error_code'] == 'INVALID_UPLOAD_BATCH_REF'
    assert t['row_count'] == 0 and t['workspace_id'] is None
    assert scalar(db,'SELECT count(*) FROM control.customer_identity_export') == 0


@pytest.mark.parametrize('problem,reason', [
    ('external_owner','MIXED_OR_UNKNOWN_OWNER'),
    ('unknown_external','MIXED_OR_UNKNOWN_OWNER'),
    ('historical_collision','MIXED_OR_UNKNOWN_OWNER'),
    ('current_contributor','MIXED_OR_UNKNOWN_OWNER'),
    ('stale_member','STALE_RESOLUTION'),
    ('stale_entity','STALE_RESOLUTION'),
    ('missing_mapping','INCOMPLETE_MAPPING'),
    ('identity_mismatch','SOURCE_IDENTITY_CONFLICT'),
    ('source_batch_mismatch','SOURCE_IDENTITY_CONFLICT'),
    ('duplicate_user','DUPLICATE_APP_USER'),
    ('blank_user','BLANK_SOURCE_USER'),
])
def test_unsafe_global_components_are_blocked_without_stitch_failure(db,problem,reason):
    register(db)
    b = batch(db)
    m = member(db,b,user=' ' if problem == 'blank_user' else 'source-user')
    other = batch(db,app='222',source=1 if problem in ('historical_collision','current_contributor') else 2)
    if problem != 'unknown_external':
        register(db,'222')
    if problem in ('external_owner','unknown_external'):
        member(db,other,'outside-user',entity=m['entity'],run=b['run'])
    elif problem == 'historical_collision':
        execute(db,'INSERT INTO raw.customer_batch_member(ingestion_batch_id,raw_customer_id,source_system_id,source_customer_id) VALUES (%s,%s,%s,%s)',(other['batch'],m['raw'],b['source'],m['user']))
    elif problem == 'current_contributor':
        execute(db,'UPDATE staging.customer_profile SET batch_id=%s WHERE raw_customer_id=%s',(other['batch'],m['raw']))
    elif problem == 'stale_member':
        execute(db,'UPDATE identity.customer_entity_member SET resolution_run_id=%s',(other['run'],))
    elif problem == 'stale_entity':
        execute(db,'UPDATE identity.customer_entity SET last_resolution_run_id=%s',(other['run'],))
    elif problem == 'missing_mapping':
        execute(db,'DELETE FROM identity.customer_entity_member')
    elif problem == 'identity_mismatch':
        execute(db,"UPDATE staging.customer_profile SET source_customer_id='another-user'")
    elif problem == 'source_batch_mismatch':
        execute(db,'UPDATE control.ingestion_batch SET source_system_id=2 WHERE batch_id=%s',(b['batch'],))
        execute(db,"UPDATE control.source_landing_batch SET source_code='synthetic-2' WHERE batch_id=%s",(b['landing'],))
    elif problem == 'duplicate_user':
        duplicate = dict(b,source=2)
        member(db,duplicate,'source-user')
    finish(db,b)
    db.commit()
    t = transfer(db,b)
    assert (t['status'],t['error_code'],t['row_count']) == ('BLOCKED',reason,0), t
    assert scalar(db,'SELECT status FROM control.stitching_job WHERE job_id=%s',(b['job'],)) == 'SUCCEEDED'
    assert scalar(db,'SELECT count(*) FROM control.customer_identity_export') == 0


def connector_batch(db,workspace):
    b = batch(db,ref='connector-'+uuid4().hex[:12])
    connection,dataset,snapshot,mapping,operation,sync = (uid() for _ in range(6))
    execute(db,"INSERT INTO connector.connection(connection_id,workspace_id,definition_key,name,source_code,source_system_id) VALUES (%s,%s,'postgresql','synthetic','synthetic-1',1)",(connection,workspace))
    execute(db,"INSERT INTO connector.dataset(dataset_id,workspace_id,connection_id,object_name) VALUES (%s,%s,%s,'customers')",(dataset,workspace,connection))
    execute(db,"INSERT INTO connector.schema_snapshot(schema_snapshot_id,workspace_id,dataset_id,schema_hash,schema_json) VALUES (%s,%s,%s,'synthetic','{}')",(snapshot,workspace,dataset))
    execute(db,"INSERT INTO connector.mapping_version(mapping_version_id,workspace_id,dataset_id,version,schema_snapshot_id,mapping_json,mapping_hash) VALUES (%s,%s,%s,1,%s,'{}','synthetic')",(mapping,workspace,dataset,snapshot))
    execute(db,"INSERT INTO connector.operation(operation_id,workspace_id,connection_id,dataset_id,operation_type,idempotency_key) VALUES (%s,%s,%s,%s,'DATASET_SYNC','synthetic')",(operation,workspace,connection,dataset))
    execute(db,"INSERT INTO connector.sync_run(sync_run_id,operation_id,workspace_id,dataset_id,landing_batch_id,ingestion_batch_id,stitching_job_id,mapping_version_id) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",(sync,operation,workspace,dataset,b['landing'],b['batch'],b['job'],mapping))
    return dict(b,sync=sync,connection=connection,dataset=dataset,mapping=mapping)


@pytest.mark.parametrize('conflict',[None,'connection','dataset','mapping','operation','job','file_app'])
def test_connector_is_excluded_regardless_of_lineage_conflicts(db,conflict):
    setup_database(db,with_connector=True)
    workspace, other_workspace = uid(),uid()
    execute(db,"INSERT INTO platform.workspace(workspace_id,slug,name) VALUES (%s,'synthetic','Synthetic'),(%s,'other','Other')",(workspace,other_workspace))
    register(db,workspace=workspace)
    register(db,'222',workspace=other_workspace)
    b = connector_batch(db,workspace)
    member(db,b)
    if conflict in ('connection','dataset','mapping','operation'):
        table = 'mapping_version' if conflict == 'mapping' else conflict
        execute(db,f'UPDATE connector.{table} SET workspace_id=%s',(other_workspace,))
    elif conflict == 'job':
        other = batch(db,app='222',source=2)
        execute(db,'UPDATE connector.sync_run SET stitching_job_id=%s',(other['job'],))
    elif conflict == 'file_app':
        ref='2026-08-26-083337-222-123456789abc'
        execute(db,'UPDATE control.ingestion_batch SET batch_ref=%s',(ref,))
        execute(db,'UPDATE control.source_landing_batch SET batch_ref=%s',(ref,))
    finish(db,b)
    db.commit()
    t=transfer(db,b)
    assert t is None
    assert scalar(db, 'SELECT count(*) FROM control.customer_identity_capture_issue') == 0
    assert scalar(db, 'SELECT count(*) FROM control.customer_identity_export') == 0


@pytest.mark.parametrize('mutation',[
    'UPDATE control.customer_identity_export SET payload=payload',
    'DELETE FROM control.customer_identity_export',
    'TRUNCATE control.customer_identity_export',
    "UPDATE control.customer_identity_transfer SET app_id='222'",
    'UPDATE control.customer_identity_transfer SET row_count=row_count+1',
    'UPDATE control.customer_identity_transfer SET publication_seq=999',
    "INSERT INTO control.customer_identity_export SELECT transfer_id,99,app_id,'extra-user',payload FROM control.customer_identity_export",
    'DELETE FROM control.customer_identity_transfer',
])
def test_snapshot_and_transfer_scope_are_immutable(db,mutation):
    register(db)
    b=batch(db)
    member(db,b)
    finish(db,b)
    db.commit()
    with pytest.raises(psycopg2.Error):
        execute(db,mutation)
    db.rollback()
    assert transfer(db,b)['row_count'] == 1
    assert scalar(db,'SELECT count(*) FROM control.customer_identity_export') == 1


def test_frozen_snapshot_survives_later_run(db):
    workspace=register(db)
    b=batch(db)
    member(db,b)
    finish(db,b)
    db.commit()
    frozen=execute(db,'SELECT * FROM control.customer_identity_export')
    t=transfer(db,b)
    later=batch(db)
    execute(db,"UPDATE identity.customer_entity SET canonical_first_name='Later',last_resolution_run_id=%s",(later['run'],))
    execute(db,'UPDATE identity.customer_entity_member SET resolution_run_id=%s',(later['run'],))

    execute(db,"UPDATE control.customer_identity_transfer SET status='FAILED',error_code='CH_UNAVAILABLE',attempts=1")
    execute(db,"UPDATE control.customer_identity_transfer SET status='RUNNING',error_code=NULL,attempts=2")
    finish(db,b)  # Replay must never resolve or recapture.
    db.commit()
    assert execute(db,'SELECT * FROM control.customer_identity_export') == frozen
    assert transfer(db,b)['workspace_id'] == workspace
    assert transfer(db,b)['publication_seq'] == t['publication_seq']


@pytest.mark.parametrize('has_run',[False,True])
def test_success_without_successful_resolution_has_durable_block_reason(db,has_run):
    b=batch(db)
    if not has_run:
        execute(db,'DELETE FROM identity.resolution_run')
    execute(db,"UPDATE control.stitching_job SET status='SUCCEEDED'")
    db.commit()
    assert scalar(db,'SELECT status FROM control.stitching_job') == 'SUCCEEDED'
    assert scalar(db,'SELECT count(*) FROM control.customer_identity_export') == 0
    assert scalar(db,"SELECT to_regclass('control.customer_identity_capture_issue')") is not None
    assert scalar(db,'SELECT error_code FROM control.customer_identity_capture_issue WHERE stitching_job_id=%s',(b['job'],)) == 'NO_SUCCESSFUL_RESOLUTION'


def test_zero_rows_and_duplicate_success_replay_are_idempotent(db):
    register(db)
    b=batch(db)
    finish(db,b)
    db.commit()
    first=transfer(db,b)
    assert first['status']=='PENDING' and first['row_count']==0
    finish(db,b)
    db.commit()
    assert transfer(db,b)==first
    assert scalar(db,'SELECT count(*) FROM control.customer_identity_transfer') == 1


def test_outer_rollback_hides_outbox_and_export_from_other_sessions(db):
    register(db)
    b=batch(db)
    member(db,b)
    db.commit()
    observer=psycopg2.connect(db.dsn)
    try:
        finish(db,b)
        assert scalar(db,'SELECT count(*) FROM control.customer_identity_export')==1
        assert scalar(observer,'SELECT count(*) FROM control.customer_identity_export')==0
        assert scalar(observer,'SELECT count(*) FROM control.customer_identity_transfer')==0
        db.rollback()
        observer.commit()
        assert scalar(observer,'SELECT count(*) FROM control.customer_identity_transfer')==0
        assert scalar(observer,'SELECT status FROM control.stitching_job')=='RUNNING'
    finally:
        observer.close()


def test_sql_error_is_sanitized_and_partial_capture_rolled_back(db):
    register(db)
    b=batch(db)
    member(db,b)
    execute(db,"""CREATE FUNCTION control.synthetic_capture_failure() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION 'synthetic@example.test must never be persisted'; END $$;
        CREATE TRIGGER synthetic_failure AFTER INSERT ON control.customer_identity_export
        FOR EACH ROW EXECUTE FUNCTION control.synthetic_capture_failure()""")
    finish(db,b)
    db.commit()
    t=transfer(db,b)
    assert (t['status'],t['row_count'],t['error_code']) == ('BLOCKED',0,'CAPTURE_SQL_ERROR')
    assert scalar(db,'SELECT count(*) FROM control.customer_identity_export')==0
    assert scalar(db,'SELECT status FROM control.stitching_job')=='SUCCEEDED'


def test_outbox_storage_error_never_undoes_success_and_records_issue(db):
    register(db)
    b=batch(db)
    member(db,b)
    execute(db,"""CREATE FUNCTION control.synthetic_outbox_failure() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION 'synthetic private value'; END $$;
        CREATE TRIGGER synthetic_failure BEFORE INSERT ON control.customer_identity_transfer
        FOR EACH ROW EXECUTE FUNCTION control.synthetic_outbox_failure()""")
    finish(db,b)
    db.commit()
    assert scalar(db,'SELECT status FROM control.stitching_job')=='SUCCEEDED'
    assert scalar(db,'SELECT count(*) FROM control.customer_identity_transfer')==0
    assert scalar(db,'SELECT error_code FROM control.customer_identity_capture_issue')=='CAPTURE_SQL_ERROR'


@pytest.mark.parametrize('ref',[
    'prefix-2026-08-26-083337-1282009980020330-123456789abc',
    '2026-08-26-083337-1282009980020330-123456789abc-suffix',
    '2026-08-26-083337-app123-123456789abc',
])
def test_file_app_pattern_is_anchored_and_never_guessed(db,ref):
    register(db)
    b=batch(db,ref=ref)
    member(db,b)
    finish(db,b)
    db.commit()
    assert transfer(db,b)['error_code']=='INVALID_UPLOAD_BATCH_REF'


@pytest.mark.parametrize('table,sql',[
    ('raw.customer_batch_member','INSERT INTO raw.customer_batch_member SELECT %s,raw_customer_id,source_system_id,source_customer_id,now() FROM raw.customer_batch_member LIMIT 1'),
    ('staging.customer_profile','UPDATE staging.customer_profile SET first_name_raw=first_name_raw WHERE batch_id<>%s'),
])
def test_share_locks_cover_capture_through_outer_commit(db,table,sql):
    register(db)
    b=batch(db)
    member(db,b)
    other=batch(db)
    db.commit()
    observer=psycopg2.connect(db.dsn)
    try:
        finish(db,b)
        assert scalar(db,"SELECT count(*) FROM pg_locks WHERE pid=pg_backend_pid() AND relation=%s::regclass AND mode='ShareLock' AND granted",(table,)) == 1
        execute(observer,"SET LOCAL lock_timeout='100ms'")
        # A held capture lock permits ordinary readers, not ingestion writers.
        assert scalar(observer, f'SELECT count(*) FROM {table}') == 1
        with pytest.raises(psycopg2.errors.LockNotAvailable):
            execute(observer,sql,(other['batch'],))
        observer.rollback()
        db.commit()
        execute(observer,"SET LOCAL lock_timeout='100ms'")
        execute(observer,sql,(other['batch'],))
        observer.rollback()
    finally:
        observer.close()


def test_ingestion_overlap_never_deadlocks_or_rolls_back_writer(db):
    from concurrent.futures import ThreadPoolExecutor

    register(db)
    b, incoming = batch(db), batch(db)
    m = member(db, b)
    db.commit()
    writer = psycopg2.connect(db.dsn)
    observer = psycopg2.connect(db.dsn)
    observer.autocommit = True
    try:
        # Real ingestion order: first own ingestion_batch RowExclusive, then
        # append membership. The old capture order holds membership SHARE while
        # waiting for ingestion SHARE, making this writer the deadlock victim.
        execute(writer, "SET LOCAL deadlock_timeout='100ms'; SET LOCAL statement_timeout='5s'")
        execute(writer, "UPDATE control.ingestion_batch SET status='processing' WHERE batch_id=%s", (incoming['batch'],))
        execute(db, "SET LOCAL deadlock_timeout='10s'; SET LOCAL statement_timeout='5s'")
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(finish, db, b)
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                if future.done() or scalar(observer, """SELECT count(*) FROM pg_locks
                    WHERE pid=%s AND relation='control.ingestion_batch'::regclass
                      AND mode='ShareLock' AND NOT granted""", (db.get_backend_pid(),)):
                    break
                time.sleep(.01)
            else:
                db.cancel()
                pytest.fail('capture neither completed nor reached the ingestion lock')
            writer_error = None
            start = time.perf_counter()
            try:
                execute(writer, '''INSERT INTO raw.customer_batch_member
                    (ingestion_batch_id,raw_customer_id,source_system_id,source_customer_id)
                    VALUES (%s,%s,%s,%s)''', (incoming['batch'], m['raw'], b['source'], m['user']))
                writer.commit()
            except psycopg2.Error as exc:
                writer_error = exc.pgcode
                writer.rollback()
            elapsed = time.perf_counter() - start
            future.result(timeout=6)
        assert writer_error is None, f'ingestion was rolled back with SQLSTATE {writer_error}'
        assert elapsed < 2, 'writer waited behind rejected capture'
        # Capture's outer transaction is STILL open: failed ownership locks
        # must have been released by its savepoint, not by an early commit.
        assert scalar(observer, """SELECT count(*) FROM pg_locks WHERE pid=%s
            AND locktype='relation' AND mode='ShareLock' AND granted""", (db.get_backend_pid(),)) == 0
        db.commit()
        t = transfer(db, b)
        assert (t['status'], t['error_code'], t['row_count']) == ('BLOCKED', 'CAPTURE_LOCK_CONTENDED', 0)
        assert scalar(db, 'SELECT count(*) FROM control.customer_identity_export') == 0
        assert scalar(db, 'SELECT status FROM control.stitching_job WHERE job_id=%s', (b['job'],)) == 'SUCCEEDED'
        assert scalar(db, 'SELECT status FROM identity.resolution_run WHERE run_id=%s', (b['run'],)) == 'SUCCEEDED'
        assert scalar(db, 'SELECT status FROM control.ingestion_batch WHERE batch_id=%s', (incoming['batch'],)) == 'processing'
        assert scalar(db, 'SELECT count(*) FROM raw.customer_batch_member WHERE ingestion_batch_id=%s', (incoming['batch'],)) == 1
        # A later success replay must not capture mutable state from a stale run.
        finish(db, b)
        db.commit()
        assert transfer(db, b) == t
        assert scalar(db, 'SELECT count(*) FROM control.customer_identity_export') == 0
        print(f'ingestion_overlap_write_commit_seconds={elapsed:.3f}; capture=CAPTURE_LOCK_CONTENDED')
    finally:
        writer.close()
        observer.close()
        db.rollback()


@pytest.mark.parametrize('table', [
    'raw.customer_batch_member', 'staging.customer_profile',
    'control.customer_identity_upload_scope', 'control.ingestion_batch',
    'control.source_landing_batch', 'control.source_system',
    'connector.sync_run',
])
def test_every_ownership_lock_fails_closed_without_waiting(db, table):
    from concurrent.futures import ThreadPoolExecutor

    setup_database(db, with_connector=True)
    workspace = uid()
    execute(db, "INSERT INTO platform.workspace(workspace_id,slug,name) VALUES (%s,'synthetic','Synthetic')", (workspace,))
    register(db, workspace=workspace)
    b = batch(db)
    member(db, b)
    db.commit()
    writer = psycopg2.connect(db.dsn)
    try:
        execute(writer, f'LOCK TABLE {table} IN ROW EXCLUSIVE MODE')
        # This is a bound for test cleanup only, NOT the production strategy.
        execute(db, "SET LOCAL statement_timeout='5s'; SET LOCAL lock_timeout='4s'")
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(finish, db, b)
            future.result(timeout=2)
        t = transfer(db, b)
        assert (t['status'], t['error_code'], t['row_count']) == ('BLOCKED', 'CAPTURE_LOCK_CONTENDED', 0)
        assert scalar(db, "SELECT count(*) FROM pg_locks WHERE pid=pg_backend_pid() AND mode='ShareLock' AND granted") == 0
        # Even a late connector conflict releases the membership lock prefix.
        execute(writer, 'LOCK TABLE raw.customer_batch_member IN ROW EXCLUSIVE MODE NOWAIT')
        writer.commit()
        db.commit()
        assert scalar(db, 'SELECT status FROM control.stitching_job') == 'SUCCEEDED'
        assert scalar(db, 'SELECT status FROM identity.resolution_run') == 'SUCCEEDED'
        assert scalar(db, 'SELECT count(*) FROM control.customer_identity_export') == 0
    finally:
        writer.close()
        db.rollback()


def test_contended_global_lock_blocks_capture_without_stitch_failure(db):
    register(db)
    b=batch(db)
    member(db,b)
    db.commit()
    blocker=psycopg2.connect(db.dsn)
    try:
        execute(blocker,'SELECT pg_advisory_lock(7413220179::bigint)')
        execute(db,"SET LOCAL lock_timeout='100ms'")
        finish(db,b)
        db.commit()
        assert scalar(db,'SELECT status FROM control.stitching_job')=='SUCCEEDED'
        assert transfer(db,b) is None
        assert scalar(db,'SELECT status FROM control.customer_identity_capture_issue')=='BLOCKED'
    finally:
        blocker.close()


def test_reconciliation_sql_runs_read_only_and_has_no_payload_output(db):
    register(db)
    b=batch(db)
    member(db,b)
    finish(db,b)
    db.commit()
    path=ROOT/'database/queries/verify_customer_identity_publication.sql'
    assert path.exists(), 'read-only reconciliation query missing'
    with db.cursor() as q:
        q.execute(path.read_text())
        assert q.description is None or all(c.name not in {'payload','user_id','email','customer_id'} for c in q.description)


def test_real_stitcher_persist_resolution_triggers_atomic_snapshot(db):
    import sys
    sys.path.insert(0,str(STITCHER/'src'))
    from identity_stitcher.db import ADVISORY_LOCK_ID
    from identity_stitcher.linkage import load_profiles,ResolutionResult
    from identity_stitcher.repository import persist_resolution
    register(db)
    b=batch(db)
    m=member(db,b)
    execute(db,'DELETE FROM identity.customer_entity_member; DELETE FROM identity.customer_entity')
    execute(db,"UPDATE staging.customer_profile SET standardization_status='READY'")
    execute(db,'UPDATE control.stitching_job SET selected_record_count=1')
    db.commit()
    execute(db,'SELECT pg_advisory_lock(%s)',(ADVISORY_LOCK_ID,))
    try:
        profiles=load_profiles(db)
        result=ResolutionResult(records=profiles,pairs=[],memberships={m['standardized']:m['entity']},member_probabilities={m['standardized']:1.0},member_methods={m['standardized']:'singleton'},candidate_pair_count=0)
        metrics=persist_resolution(db,b['run'],result,1,stitching_job_id=b['job'])
        assert metrics['selected_member_records']==1
        assert transfer(db,b)['status']=='PENDING',transfer(db,b)
        assert scalar(db,"SELECT payload#>>'{profile,customer_id}' FROM control.customer_identity_export")==m['user']
    finally:
        execute(db,'SELECT pg_advisory_unlock(%s)',(ADVISORY_LOCK_ID,))
        db.commit()


@pytest.mark.parametrize('row_count',[10000,100000])
def test_capture_is_set_based_at_ten_thousand_users(db,row_count):
    register(db)
    b=batch(db)
    execute(db,"""INSERT INTO raw.customer_current(batch_id,source_system_id,source_customer_id)
        SELECT %s,1,'synthetic-'||lpad(i::text,8,'0') FROM generate_series(1,%s) i""",(b['batch'],row_count))
    execute(db,"""INSERT INTO raw.customer_batch_member(ingestion_batch_id,raw_customer_id,source_system_id,source_customer_id)
        SELECT batch_id,raw_customer_id,source_system_id,source_customer_id FROM raw.customer_current""")
    execute(db,"""INSERT INTO staging.customer_profile(raw_customer_id,batch_id,source_system_id,source_customer_id,standardized_at)
        SELECT raw_customer_id,batch_id,source_system_id,source_customer_id,'2026-01-01' FROM raw.customer_current""")
    execute(db,"""INSERT INTO identity.customer_entity(customer_entity_id,canonical_first_name,last_resolution_run_id)
        SELECT standardized_record_id,'Synthetic',%s FROM staging.customer_profile""",(b['run'],))
    execute(db,"""INSERT INTO identity.customer_entity_member(standardized_record_id,customer_entity_id,source_system_id,source_customer_id,match_method,resolution_run_id)
        SELECT standardized_record_id,standardized_record_id,source_system_id,source_customer_id,'singleton',%s FROM staging.customer_profile""",(b['run'],))
    execute(db,'ANALYZE raw.customer_batch_member; ANALYZE staging.customer_profile; ANALYZE identity.customer_entity_member; ANALYZE identity.customer_entity')
    db.commit()
    start=time.perf_counter()
    finish(db,b)
    db.commit()
    elapsed=time.perf_counter()-start
    t=transfer(db,b)
    assert t['status']=='PENDING',t
    assert t['row_count']==row_count
    assert scalar(db,'SELECT max(row_number) FROM control.customer_identity_export')==row_count
    print(f'capture_{row_count}_commit_seconds={elapsed:.3f}')
    assert elapsed < (5 if row_count==10000 else 30), 'capture must not perform quadratic materialized-CTE joins'


def test_session_stitcher_lock_survives_capture_failure_and_rollback(db):
    b=batch(db, ref='malformed-upload')
    member(db,b)
    db.commit()
    execute(db,'SELECT pg_advisory_lock(7413220179::bigint)')
    other=psycopg2.connect(db.dsn)
    try:
        finish(db,b)  # Invalid upload ref raises inside capture savepoint.
        assert transfer(db,b)['status']=='BLOCKED'
        db.rollback()
        assert scalar(other,'SELECT pg_try_advisory_lock(7413220179::bigint)') is False
        execute(db,'SELECT pg_advisory_unlock(7413220179::bigint)')
        assert scalar(other,'SELECT pg_try_advisory_lock(7413220179::bigint)') is True
    finally:
        other.close()
        execute(db,'SELECT pg_advisory_unlock_all()')
        db.commit()


def test_publication_sequence_serializes_concurrent_success_commits(db):
    from concurrent.futures import ThreadPoolExecutor
    register(db)
    first,second=batch(db),batch(db)
    member(db,first,'first')
    member(db,second,'second')
    db.commit()
    other=psycopg2.connect(db.dsn)
    try:
        finish(db,first)
        def commit_second():
            finish(other,second)
            other.commit()
        with ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(commit_second)
            # Observe the real wait rather than assuming thread scheduling.
            observer=psycopg2.connect(db.dsn)
            try:
                deadline=time.monotonic()+3
                while time.monotonic()<deadline:
                    observer.commit()
                    if scalar(observer,"SELECT count(*) FROM pg_locks WHERE pid=%s AND locktype='advisory' AND NOT granted",(other.get_backend_pid(),)):
                        break
                    time.sleep(.01)
                else:
                    pytest.fail('second capture did not wait on the global lock')
                assert not future.done()
                db.commit()
                future.result(timeout=5)
            finally:
                observer.close()
        assert transfer(db,first)['publication_seq'] < transfer(db,second)['publication_seq']
    finally:
        other.close()


@pytest.mark.parametrize('default_scope', ['', 'IN SCHEMA control'])
def test_new_capture_objects_ignore_inherited_default_acl(db, default_scope):
    reader = 'capture_reader_' + uuid4().hex
    db.commit()
    db.autocommit = True
    execute(db, f'CREATE ROLE {reader} NOLOGIN NOSUPERUSER NOINHERIT')
    try:
        # Bootstrap the same existing catalog without 024 so both global and
        # schema-specific defaults genuinely predate creation of new objects.
        execute(db, 'DROP SCHEMA IF EXISTS connector CASCADE; DROP SCHEMA IF EXISTS platform CASCADE')
        execute(db, (STITCHER / 'tests/fixtures/repository_catalog.sql').read_text())
        execute(db, (STITCHER / 'src/identity_stitcher/sql/001_identity_model.sql').read_text())
        execute(db, (ROOT / 'database/migrations/014_pipeline_tracking.sql').read_text())
        execute(db, f'''ALTER DEFAULT PRIVILEGES {default_scope} GRANT SELECT,INSERT,UPDATE
            ON TABLES TO {reader} WITH GRANT OPTION;
            ALTER DEFAULT PRIVILEGES {default_scope} GRANT EXECUTE ON FUNCTIONS TO {reader};
            ALTER DEFAULT PRIVILEGES {default_scope} GRANT USAGE,SELECT,UPDATE ON SEQUENCES TO {reader};
            ALTER DEFAULT PRIVILEGES {default_scope} GRANT SELECT ON TABLES TO PUBLIC;
            GRANT SELECT,INSERT,UPDATE ON control.ingestion_batch TO {reader}''')
        defaults = execute(db, 'SELECT defaclobjtype,defaclacl::text FROM pg_default_acl ORDER BY oid')
        old_acl = scalar(db, "SELECT relacl::text FROM pg_class WHERE oid='control.ingestion_batch'::regclass")
        execute(db, MIGRATION.read_text())
        db.autocommit = False
        assert execute(db, 'SELECT defaclobjtype,defaclacl::text FROM pg_default_acl ORDER BY oid') == defaults
        # Existing objects/default policy must not be changed by migration 024.
        assert scalar(db, "SELECT relacl::text FROM pg_class WHERE oid='control.ingestion_batch'::regclass") == old_acl
        assert scalar(db, "SELECT has_table_privilege(%s,'control.ingestion_batch','SELECT,INSERT,UPDATE')", (reader,))
        register(db)
        b = batch(db)
        member(db, b)
        finish(db, b)
        db.commit()
        execute(db, f'GRANT USAGE ON SCHEMA control TO {reader}')
        db.commit()
        # Check actual SET ROLE access, not just PUBLIC or catalog declarations.
        denied = [
            'SELECT payload FROM control.customer_identity_export',
            'SELECT * FROM control.customer_identity_transfer',
            'SELECT * FROM control.customer_identity_capture_issue',
            'SELECT * FROM control.customer_identity_upload_scope',
            "UPDATE control.customer_identity_upload_scope SET workspace_id=gen_random_uuid()",
            "INSERT INTO control.customer_identity_upload_scope(app_id,workspace_id) VALUES ('777',gen_random_uuid())",
            f"SELECT control.capture_customer_identity_snapshot('{b['job']}'::uuid)",
            f"SELECT * FROM control.resolve_customer_identity_batches(ARRAY['{b['batch']}'::uuid])",
            f"SELECT * FROM control.customer_identity_connector_lineage(ARRAY['{b['batch']}'::uuid])",
            "SELECT nextval(pg_get_serial_sequence('control.customer_identity_transfer','publication_seq'))",
        ]
        for statement in denied:
            execute(db, f'SET ROLE {reader}')
            try:
                with pytest.raises(psycopg2.errors.InsufficientPrivilege, match='permission denied'):
                    execute(db, statement)
            finally:
                db.rollback()
        # Include every new relation/sequence and every helper/trigger function.
        acl = execute(db, """SELECT c.relname,a.grantee,c.relowner
            FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
            CROSS JOIN LATERAL aclexplode(coalesce(c.relacl,
                acldefault(CASE WHEN c.relkind='S' THEN 'S'::\"char\" ELSE 'r'::\"char\" END,c.relowner))) a
            WHERE n.nspname='control' AND c.relname LIKE 'customer_identity_%'
                AND c.relkind IN ('r','S')
            UNION ALL
            SELECT p.proname,a.grantee,p.proowner
            FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
            CROSS JOIN LATERAL aclexplode(coalesce(p.proacl,acldefault('f',p.proowner))) a
            WHERE n.nspname='control' AND p.proname LIKE '%customer_identity%'""")
        assert acl and all(grantee == owner for _, grantee, owner in acl), acl
        assert transfer(db, b)['status'] == 'PENDING'
    finally:
        db.rollback()
        db.autocommit = True
        execute(db, f'ALTER DEFAULT PRIVILEGES {default_scope} REVOKE SELECT ON TABLES FROM PUBLIC')
        execute(db, f'DROP OWNED BY {reader}; DROP ROLE {reader}')
        db.autocommit = False


def test_least_privilege_stitcher_captures_without_private_object_grants(db):
    role = 'capture_stitcher_' + uuid4().hex
    register(db)
    b = batch(db)
    member(db, b)
    execute(db, "UPDATE identity.resolution_run SET status='SUCCEEDED',completed_at=clock_timestamp()")
    execute(db, 'UPDATE control.stitching_job SET selected_record_count=1,member_record_count=1')
    execute(db, f'''CREATE ROLE {role} NOLOGIN NOSUPERUSER NOINHERIT;
        GRANT USAGE ON SCHEMA control TO {role};
        GRANT SELECT(job_id),UPDATE(status,completed_at) ON control.stitching_job TO {role}''')
    db.commit()
    try:
        execute(db, f'SET ROLE {role}')
        # The fixed path and qualified names must ignore caller-created shadows.
        execute(db, '''CREATE TEMP TABLE customer_identity_upload_scope(app_id text);
            CREATE TEMP TABLE customer_identity_transfer(stitching_job_id uuid);
            SET LOCAL search_path=pg_temp,public''')
        execute(db, "UPDATE control.stitching_job SET status='SUCCEEDED',completed_at=clock_timestamp() WHERE job_id=%s", (b['job'],))
        execute(db, 'RESET ROLE')
        db.commit()
        assert transfer(db, b)['status'] == 'PENDING'
        assert scalar(db, 'SELECT count(*) FROM control.customer_identity_export') == 1
        assert scalar(db, "SELECT has_table_privilege(%s,'control.customer_identity_export','SELECT')", (role,)) is False
        assert scalar(db, "SELECT has_function_privilege(%s,'control.capture_customer_identity_snapshot(uuid)','EXECUTE')", (role,)) is False
    finally:
        db.rollback()
        execute(db, 'RESET ROLE')
        execute(db, f'DROP OWNED BY {role}; DROP ROLE {role}')
        db.commit()


def test_capture_trigger_security_is_fixed_and_worker_cannot_call_it(db):
    rows=execute(db,"""SELECT proname,prosecdef,proconfig,
        coalesce(has_function_privilege('public',p.oid,'EXECUTE'),false)
        FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
        WHERE n.nspname='control' AND proname IN ('capture_customer_identity_snapshot','capture_customer_identity_after_stitch')
        ORDER BY proname""")
    assert len(rows)==2
    assert all('search_path=pg_catalog, pg_temp' in row[2] for row in rows)
    assert all(not row[3] for row in rows)
    assert rows[0][1] is True and rows[1][1] is False
