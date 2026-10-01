"""Forward-only capture contract on a fresh owned localhost PostgreSQL database.

No staging endpoint is accepted. Each test owns and drops only its unique database.
"""
from __future__ import annotations

import ast
import os
from pathlib import Path
from uuid import uuid4

import psycopg2
from psycopg2.extras import RealDictCursor
import pytest

ROOT = Path(__file__).resolve().parents[2]
STITCHER = Path(os.getenv('IDENTITY_STITCHER_ROOT', str(ROOT.parent / 'fargate-user-stitching')))
API = Path(os.getenv('CONNECTOR_API_ROOT', str(ROOT.parent / 'journey-builder-api')))
A, B = '1282009980020330', '43'
MIGRATION = ROOT / 'database/migrations/036_customer_global_upload_capture.sql'
BASE = 'host=127.0.0.1 port=55441 user=postgres dbname=postgres connect_timeout=3'


def query(db, sql, args=None):
    with db.cursor() as cur:
        cur.execute(sql, args)
        return cur.fetchall() if cur.description else None


def one(db, sql, args=None):
    return query(db, sql, args)[0][0]


@pytest.fixture
def db():
    admin = psycopg2.connect(BASE)
    name = 'poststitch_capture_036_' + uuid4().hex
    assert admin.info.host == '127.0.0.1' and admin.info.port == 55441
    assert admin.info.dbname == 'postgres' and admin.info.user == 'postgres'
    assert one(admin, 'SELECT pg_is_in_recovery()') is False
    assert one(admin, 'SELECT rolcreatedb OR rolsuper FROM pg_roles WHERE rolname=current_user')
    admin.rollback()
    admin.autocommit = True
    db_conn = None
    created = False
    try:
        assert one(admin, 'SELECT count(*) FROM pg_database WHERE datname=%s', (name,)) == 0
        query(admin, f'CREATE DATABASE {name} TEMPLATE template0 ENCODING %s', ('UTF8',))
        created = True
        db_conn = psycopg2.connect(BASE.replace('dbname=postgres', f'dbname={name}'))
        assert (db_conn.info.host, db_conn.info.port, db_conn.info.dbname) == ('127.0.0.1', 55441, name)
        assert one(db_conn, 'SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname=current_database()') == 'postgres'
        db_conn.rollback()
        db_conn.autocommit = True
        for path in (
            STITCHER / 'tests/fixtures/repository_catalog.sql',
            STITCHER / 'src/identity_stitcher/sql/001_identity_model.sql',
            ROOT / 'database/migrations/014_pipeline_tracking.sql',
            ROOT / 'database/migrations/024_customer_identity_publication.sql',
        ):
            query(db_conn, path.read_text())
        # Fixture catalog is already at the 019 table shape, but lacks its
        # triggers. Install the actual 019 capture trigger bodies before 022.
        typed = (ROOT / 'database/migrations/019_customer_typed_history.sql').read_text()
        query(db_conn, typed[typed.index('CREATE FUNCTION raw.capture_customer_history()'):
                             typed.index('-- Batch creation order')])
        query(db_conn, (ROOT / 'database/migrations/022_customer_bulk_ingestion.sql').read_text())
        query(db_conn, (ROOT / 'database/migrations/026_customer_app_scope.sql').read_text())
        tree = ast.parse((API / 'alembic/versions/001_connector_control_plane.py').read_text())
        ddl = next(ast.literal_eval(node.value) for node in tree.body if isinstance(node, ast.Assign)
                   and any(isinstance(t, ast.Name) and t.id == 'DDL_STATEMENTS' for t in node.targets))
        query(db_conn, ddl)
        assert db_conn is not None
        db_conn.autocommit = False
        yield db_conn
    finally:
        if db_conn:
            db_conn.close()
        # The generated name is never accepted from the environment or user input.
        assert name.startswith('poststitch_capture_036_') and len(name) == len('poststitch_capture_036_') + 32
        if created:
            query(admin, f'DROP DATABASE {name} WITH (FORCE)')
        admin.close()


def install(db):
    query(db, MIGRATION.read_text())
    db.commit()


def batch(db, app, *, source=1, ref=None):
    landing, ingestion, job, run = (str(uuid4()) for _ in range(4))
    ref = ref or f'2026-09-27-120000-{app}-{uuid4().hex[:12]}'
    query(db, "INSERT INTO control.source_system(source_system_id,source_code,source_name) VALUES (%s,%s,'fixture') ON CONFLICT DO NOTHING", (source, f'fixture-{source}'))
    query(db, "INSERT INTO control.source_landing_batch(batch_id,source_code,batch_ref,output_status) VALUES (%s,%s,%s,'PUBLISHED')", (landing, f'fixture-{source}', ref))
    query(db, "INSERT INTO control.ingestion_batch(batch_id,source_system_id,landing_batch_id,batch_ref,status) VALUES (%s,%s,%s,%s,'completed')", (ingestion, source, landing, ref))
    query(db, "INSERT INTO control.stitching_job(job_id,ingestion_batch_id,status) VALUES (%s,%s,'RUNNING')", (job, ingestion))
    query(db, "INSERT INTO identity.resolution_run(run_id,stitching_job_id,status,normalization_version,match_threshold) VALUES (%s,%s,'RUNNING','fixture',0.9)", (run, job))
    return dict(landing=landing, batch=ingestion, job=job, run=run, source=source, app=app)


def member(db, b, user, *, raw=None, entity=None, first='Local', email='local@example.test', history=True):
    raw = raw or str(uuid4())
    standard = str(uuid4())
    entity = entity or str(uuid4())
    if one(db, 'SELECT count(*) FROM raw.customer_current WHERE raw_customer_id=%s', (raw,)):
        query(db, '''UPDATE raw.customer_current SET batch_id=%s,first_name=%s,email=%s WHERE raw_customer_id=%s''',
              (b['batch'], first, email, raw))
    else:
        query(db, '''INSERT INTO raw.customer_current(raw_customer_id,batch_id,source_system_id,source_customer_id,first_name,email)
             VALUES (%s,%s,%s,%s,%s,%s)''', (raw,b['batch'],b['source'],user,first,email))
    # The real 022 statement trigger writes both immutable history and membership.
    if not one(db, 'SELECT count(*) FROM raw.customer_batch_member WHERE ingestion_batch_id=%s AND raw_customer_id=%s',
               (b['batch'],raw)):
        query(db, '''INSERT INTO raw.customer_batch_member(ingestion_batch_id,raw_customer_id,source_system_id,source_customer_id)
            VALUES (%s,%s,%s,%s)''', (b['batch'],raw,b['source'],user))
    if history:
        query(db, '''INSERT INTO raw.customer(raw_customer_id,batch_id,source_system_id,source_customer_id,first_name,email)
            VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (batch_id,raw_customer_id) DO NOTHING''',
            (raw,b['batch'],b['source'],user,first,email))
    if one(db, 'SELECT count(*) FROM staging.customer_profile WHERE raw_customer_id=%s', (raw,)):
        query(db, '''UPDATE staging.customer_profile SET batch_id=%s,first_name_raw=%s,email_raw=%s,
            standardized_at='2026-09-02T12:00:00+07' WHERE raw_customer_id=%s''',
            (b['batch'],first,email,raw))
        standard = one(db, 'SELECT standardized_record_id FROM staging.customer_profile WHERE raw_customer_id=%s', (raw,))
    else:
        query(db, '''INSERT INTO staging.customer_profile(standardized_record_id,raw_customer_id,batch_id,
            source_system_id,source_customer_id,first_name_raw,middle_name_raw,last_name_raw,full_name_raw,
            email_raw,phone_raw,address_raw,district_normalized,province_normalized,postal_code_normalized,
            country_code,date_of_birth,gender_normalized,source_updated_at,standardized_at)
            VALUES (%s,%s,%s,%s,%s,%s,'Mid','Last','Local Mid Last',%s,'+6612345678','App address',
            'Local district','Local province','10100','TH','1990-01-02','female',
            '2026-09-01T12:00:00+07','2026-09-02T12:00:00+07')''',
            (standard,raw,b['batch'],b['source'],user,first,email))
    query(db, '''INSERT INTO identity.customer_entity(customer_entity_id,canonical_first_name,canonical_email,
        last_resolution_run_id,first_seen_at,last_seen_at) VALUES (%s,'B GLOBAL','b-global@example.test',%s,
        '2026-09-01T12:00:00+07','2026-09-02T12:00:00+07')
        ON CONFLICT (customer_entity_id) DO UPDATE SET member_count=identity.customer_entity.member_count+1''',
        (entity,b['run']))
    if not one(db, 'SELECT count(*) FROM identity.customer_entity_member WHERE standardized_record_id=%s', (standard,)):
        query(db, '''INSERT INTO identity.customer_entity_member(standardized_record_id,customer_entity_id,
            source_system_id,source_customer_id,match_method,resolution_run_id)
            VALUES (%s,%s,%s,%s,'singleton',%s)''', (standard,entity,b['source'],user,b['run']))
    return dict(raw=raw, standard=standard, entity=entity)


def finish(db, b):
    query(db, "UPDATE identity.resolution_run SET status='SUCCEEDED',completed_at=clock_timestamp() WHERE run_id=%s", (b['run'],))
    query(db, '''UPDATE control.stitching_job SET status='SUCCEEDED',completed_at=clock_timestamp(),
        selected_record_count=(SELECT count(*) FROM raw.customer_batch_member WHERE ingestion_batch_id=%s),
        member_record_count=(SELECT count(*) FROM raw.customer_batch_member WHERE ingestion_batch_id=%s)
        WHERE job_id=%s''', (b['batch'],b['batch'],b['job']))
    db.commit()
    with db.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute('SELECT * FROM control.customer_identity_transfer WHERE stitching_job_id=%s', (b['job'],))
        return cur.fetchone()


def test_036_capture_after_037_keeps_same_source_key_app_local_payload(db):
    install(db)
    query(db, "SET cdp.customer_source_namespace_opt_in = 'reviewed-file-upload-only'")
    query(db, (ROOT / 'database/migrations/037_customer_app_source_namespace.sql').read_text())
    query(db, "SET cdp.customer_source_namespace_writer = 'file-v1'")
    a = batch(db, A, source=1)
    own = member(db, a, 'synthetic-shared-key', first='Alice A', email='alice-a@example.test')
    b = batch(db, B, source=1)
    other = member(db, b, 'synthetic-shared-key', entity=own['entity'],
                   first='Bob B', email='bob-b@example.test')
    assert own['raw'] != other['raw']
    query(db, 'UPDATE identity.customer_entity_member SET resolution_run_id=%s', (a['run'],))
    query(db, 'UPDATE identity.customer_entity SET last_resolution_run_id=%s', (a['run'],))
    transfer = finish(db, a)
    assert (transfer['status'], transfer['app_id'], transfer['row_count']) == ('PENDING', A, 1)
    payload = one(db, 'SELECT payload FROM control.customer_identity_export WHERE transfer_id=%s',
                  (a['run'],))
    assert payload['customer_entity_id'] == own['entity']
    assert payload['profile']['first_name'] == 'Alice A'
    assert payload['profile']['email'] == 'alice-a@example.test'
    assert 'bob-b@example.test' not in str(payload)
    assert one(db, 'SELECT count(*) FROM control.customer_identity_export') == 1


def test_cross_app_entity_preserves_original_global_id_and_app_local_profile(db):
    a = batch(db,A,source=1)
    own = member(db,a,'a-user',first='Alice A',email='alice-a@example.test')
    b = batch(db,B,source=2)
    member(db,b,'b-user',entity=own['entity'],first='Bob B',email='bob-b@example.test')
    # One completed global stitch resolves every member in the component.
    query(db, 'UPDATE identity.customer_entity_member SET resolution_run_id=%s',(a['run'],))
    query(db, 'UPDATE identity.customer_entity SET last_resolution_run_id=%s',(a['run'],))
    install(db)
    t = finish(db,a)
    assert (t['status'],t['app_id'],t['row_count']) == ('PENDING',A,1),t
    export = one(db,'SELECT payload FROM control.customer_identity_export WHERE transfer_id=%s',(a['run'],))
    assert export['customer_entity_id'] == own['entity']
    assert export['profile']['first_name'] == 'Alice A'
    assert export['profile']['email'] == 'alice-a@example.test'
    assert export['profile']['middle_name'] == 'Mid'
    assert export['profile']['address_line_1'] == 'App address'
    assert export['profile']['date_of_birth'] == '1990-01-02'
    assert 'b-global@example.test' not in str(export)
    assert 'bob-b@example.test' not in str(export)
    assert one(db,'SELECT count(*) FROM control.customer_identity_export') == 1
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        'capture_036_publication', STITCHER / 'src/identity_stitcher/publication.py')
    assert spec is not None and spec.loader is not None
    publication = importlib.util.module_from_spec(spec)
    import sys
    sys.modules[spec.name] = publication
    spec.loader.exec_module(publication)
    publication.validate_payload(export, t, A, 'a-user')


@pytest.mark.parametrize('other', ['unknown','connector'])
def test_unknown_or_connector_entity_contributor_stays_blocked(db,other):
    a = batch(db,A)
    own = member(db,a,'a-user')
    b = batch(db,B,source=2,ref='not-an-upload' if other == 'unknown' else None)
    member(db,b,'b-user',entity=own['entity'])
    query(db, 'UPDATE identity.customer_entity_member SET resolution_run_id=%s',(a['run'],))
    query(db, 'UPDATE identity.customer_entity SET last_resolution_run_id=%s',(a['run'],))
    if other == 'connector':
        # Connector evidence by ingestion_batch_id even for an upload-looking ref.
        workspace, connection, dataset, snapshot, mapping, operation, sync = (str(uuid4()) for _ in range(7))
        query(db,"INSERT INTO platform.workspace(workspace_id,slug,name) VALUES (%s,'connector','Connector')",(workspace,))
        query(db,"INSERT INTO connector.connection(connection_id,workspace_id,definition_key,name,source_code,source_system_id) VALUES (%s,%s,'postgresql','fixture','fixture-2',2)",(connection,workspace))
        query(db,"INSERT INTO connector.dataset(dataset_id,workspace_id,connection_id,object_name) VALUES (%s,%s,%s,'customers')",(dataset,workspace,connection))
        query(db,"INSERT INTO connector.schema_snapshot(schema_snapshot_id,workspace_id,dataset_id,schema_hash,schema_json) VALUES (%s,%s,%s,'fixture','{}')",(snapshot,workspace,dataset))
        query(db,"INSERT INTO connector.mapping_version(mapping_version_id,workspace_id,dataset_id,version,schema_snapshot_id,mapping_json,mapping_hash) VALUES (%s,%s,%s,1,%s,'{}','fixture')",(mapping,workspace,dataset,snapshot))
        query(db,"INSERT INTO connector.operation(operation_id,workspace_id,connection_id,dataset_id,operation_type,idempotency_key) VALUES (%s,%s,%s,%s,'DATASET_SYNC','fixture')",(operation,workspace,connection,dataset))
        query(db,"INSERT INTO connector.sync_run(sync_run_id,operation_id,workspace_id,dataset_id,landing_batch_id,ingestion_batch_id,stitching_job_id,mapping_version_id) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",(sync,operation,workspace,dataset,b['landing'],b['batch'],b['job'],mapping))
    install(db)
    t = finish(db,a)
    assert (t['status'],t['error_code'],t['row_count']) == ('BLOCKED','MIXED_OR_UNKNOWN_OWNER',0),t
    assert one(db,'SELECT count(*) FROM control.customer_identity_export') == 0


def test_source_key_collision_rejects_selected_app_even_with_matching_global_entity(db):
    a = batch(db,A,source=1)
    own = member(db,a,'shared-source-key',first='Alice A',email='alice-a@example.test')
    b = batch(db,B,source=1)
    query(db, "UPDATE control.ingestion_batch SET created_at=created_at+interval '1 second' WHERE batch_id=%s", (b['batch'],))
    member(db,b,'shared-source-key',raw=own['raw'],entity=own['entity'],first='Bob B',email='bob-b@example.test')
    install(db)
    t = finish(db,a)
    assert (t['status'],t['row_count']) == ('BLOCKED',0),t
    assert t['error_code'] in {'SOURCE_IDENTITY_CONFLICT','MIXED_OR_UNKNOWN_OWNER','STALE_RESOLUTION'}
    assert one(db,'SELECT count(*) FROM control.customer_identity_export') == 0
    assert one(db,'SELECT count(*) FROM control.customer_identity_upload_scope') == 0


def test_collision_stays_blocked_even_when_selected_app_owns_latest_profile(db):
    b = batch(db,B,source=1)
    prior = member(db,b,'reused-source-key',first='Bob B',email='bob-b@example.test')
    a = batch(db,A,source=1)
    query(db,"UPDATE control.ingestion_batch SET created_at=created_at+interval '1 second' WHERE batch_id=%s",(a['batch'],))
    member(db,a,'reused-source-key',raw=prior['raw'],entity=prior['entity'],
           first='Alice A',email='alice-a@example.test')
    query(db,'UPDATE identity.customer_entity SET member_count=1 WHERE customer_entity_id=%s',(prior['entity'],))
    query(db,'UPDATE identity.customer_entity_member SET resolution_run_id=%s',(a['run'],))
    query(db,'UPDATE identity.customer_entity SET last_resolution_run_id=%s',(a['run'],))
    query(db,"UPDATE staging.customer_profile SET standardized_at='2026-09-02T12:00:00+07' WHERE raw_customer_id=%s",(prior['raw'],))
    assert one(db,'SELECT app_id FROM staging.customer_profile WHERE raw_customer_id=%s',(prior['raw'],)) == A
    install(db)
    assert one(db,'SELECT count(*) FROM identity.customer_entity_member WHERE customer_entity_id=%s',(prior['entity'],)) == 1
    t=finish(db,a)
    assert (t['status'],t['error_code'],t['row_count']) == ('BLOCKED','SOURCE_IDENTITY_CONFLICT',0),t
    assert one(db,'SELECT count(*) FROM control.customer_identity_export') == 0


def test_new_capture_does_not_replay_old_blocked_or_stale_global_resolution(db):
    a=batch(db,A)
    own=member(db,a,'selected-user')
    b=batch(db,B,source=2)
    member(db,b,'external-user',entity=own['entity'])
    query(db,'UPDATE identity.customer_entity_member SET resolution_run_id=%s',(a['run'],))
    query(db,'UPDATE identity.customer_entity SET last_resolution_run_id=%s',(a['run'],))
    old=finish(db,a)
    assert old['status']=='BLOCKED'
    install(db)
    assert one(db,'SELECT count(*) FROM control.customer_identity_export')==0
    assert one(db,'SELECT error_code FROM control.customer_identity_transfer WHERE stitching_job_id=%s',(a['job'],)) == old['error_code']
    query(db,"UPDATE control.stitching_job SET status='RUNNING' WHERE job_id=%s",(a['job'],))
    query(db,"UPDATE control.stitching_job SET status='SUCCEEDED' WHERE job_id=%s",(a['job'],))
    db.commit()
    assert one(db,'SELECT count(*) FROM control.customer_identity_export')==0
    assert one(db,'SELECT status FROM control.customer_identity_transfer WHERE stitching_job_id=%s',(a['job'],))=='BLOCKED'


def test_migration_rejects_definition_drift_without_mutating_function(db):
    original = one(db,"SELECT pg_get_functiondef('control.capture_customer_identity_snapshot(uuid)'::regprocedure)")
    query(db,"ALTER FUNCTION control.capture_customer_identity_snapshot(uuid) COST 150")
    altered = one(db,"SELECT pg_get_functiondef('control.capture_customer_identity_snapshot(uuid)'::regprocedure)")
    assert altered != original
    db.commit()
    with pytest.raises(psycopg2.Error,match='036.*baseline|baseline.*036'):
        query(db,MIGRATION.read_text())
    db.rollback()
    assert one(db,"SELECT pg_get_functiondef('control.capture_customer_identity_snapshot(uuid)'::regprocedure)") == altered


def test_migration_preserves_oid_acl_and_security_contract(db):
    role = 'capture_036_acl_' + uuid4().hex
    query(db, f'CREATE ROLE {role} NOLOGIN')
    query(db, f'GRANT EXECUTE ON FUNCTION control.capture_customer_identity_snapshot(uuid) TO {role}')
    db.commit()
    try:
        before = query(db,"""SELECT oid,proacl,prosecdef,proconfig,provolatile,proowner
            FROM pg_proc WHERE oid='control.capture_customer_identity_snapshot(uuid)'::regprocedure""")[0]
        assert one(db,"SELECT has_function_privilege(%s,'control.capture_customer_identity_snapshot(uuid)','EXECUTE')",(role,))
        install(db)
        after = query(db,"""SELECT oid,proacl,prosecdef,proconfig,provolatile,proowner
            FROM pg_proc WHERE oid='control.capture_customer_identity_snapshot(uuid)'::regprocedure""")[0]
        assert after == before
        assert one(db,"SELECT has_function_privilege(%s,'control.capture_customer_identity_snapshot(uuid)','EXECUTE')",(role,))
        assert 'canonical_first_name' not in one(db,"SELECT pg_get_functiondef('control.capture_customer_identity_snapshot(uuid)'::regprocedure)")
        with pytest.raises(psycopg2.Error,match='036.*baseline|baseline.*036'):
            query(db,MIGRATION.read_text())
        db.rollback()
        assert query(db,"""SELECT oid,proacl,prosecdef,proconfig,provolatile,proowner
            FROM pg_proc WHERE oid='control.capture_customer_identity_snapshot(uuid)'::regprocedure""")[0] == after
    finally:
        db.rollback()
        query(db, f'DROP OWNED BY {role}; DROP ROLE {role}')
        db.commit()
