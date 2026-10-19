"""038 forward-only database contract; disposable UTF8 PostgreSQL on owned port 55441.

No remote endpoint, deployment, historical UPDATE, or 037 installation.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from contextlib import closing
from pathlib import Path
from threading import Event
from uuid import uuid4

import psycopg2
import pytest

import test_customer_global_upload_capture as base

ROOT = Path(__file__).resolve().parents[2]
MIGRATION = ROOT / 'database/migrations/038_customer_forward_source_namespace.sql'
A, B = '0043', '43'


@pytest.fixture
def db():
    # Keep the pre-040 fixture for 050-052 migration-chain consumers.
    yield from base.db.__wrapped__()


@pytest.fixture
def connector_db():
    # Installed connector-aware 040/045 catalog, never the live database.
    for conn in base.db.__wrapped__():
        # Reproduce the installed connector-aware 040/045 catalog before 038.
        sql(conn, 'ALTER TABLE platform.workspace ADD COLUMN app_id text')
        sql(conn, "SELECT set_config('cdp.connector_owner_runtime','verified-owner-only',false)")
        for name in ('040_connector_app_lineage.sql', '041_global_member_bridge.sql',
                     '042_customer_capture_lock_retry.sql', '044_customer_overlap_capture.sql',
                     '045_connector_identity_publication.sql'):
            sql(conn, (ROOT / 'database/migrations' / name).read_text())
        conn.commit()
        yield conn


def sql(db, statement, args=None):
    return base.query(db, statement, args)


def one(db, statement, args=None):
    return base.one(db, statement, args)


def upload(db, app, source_code='fixture-1', source_id=1):
    landing, ingestion = str(uuid4()), str(uuid4())
    ref = f'2026-09-27-120000-{app}-{uuid4().hex[:12]}'
    sql(db, "INSERT INTO control.source_landing_batch(batch_id,source_code,batch_ref,output_status) VALUES (%s,%s,%s,'PUBLISHED')",
        (landing, source_code, ref))
    sql(db, "INSERT INTO control.ingestion_batch(batch_id,source_system_id,landing_batch_id,batch_ref,status) VALUES (%s,%s,%s,%s,'completed')",
        (ingestion, source_id, landing, ref))
    return ingestion


def install(db):
    assert MIGRATION.is_file(), '038 migration must exist'
    sql(db, MIGRATION.read_text())
    db.commit()


def old_alias(db, *, historical_dates=False):
    # Legacy source-only key: historical snapshots from different literal apps
    # share a raw UUID; unchanged input is the whole purpose of 038.
    base.query(db, "INSERT INTO control.source_system(source_system_id,source_code,source_name) VALUES (1,'fixture-1','fixture')")
    sql(db, "SELECT setval('control.source_system_source_system_id_seq'::regclass,1,true)")
    a = upload(db, A)
    b = upload(db, B)
    if historical_dates:
        sql(db,"UPDATE control.ingestion_batch SET created_at='2026-09-01T00:00:00Z' WHERE batch_id=%s",(a,))
        sql(db,"UPDATE control.ingestion_batch SET created_at='2026-09-01T00:00:01Z' WHERE batch_id=%s",(b,))
    rid = str(uuid4())
    sql(db, '''INSERT INTO raw.customer_current(raw_customer_id,batch_id,source_system_id,source_customer_id,first_name)
        VALUES (%s,%s,1,'old-shared','Before')''', (rid, a))
    sql(db, "UPDATE control.ingestion_batch SET created_at=created_at + interval '1 second' WHERE batch_id=%s", (b,))
    sql(db, "UPDATE raw.customer_current SET batch_id=%s,first_name='After' WHERE raw_customer_id=%s", (b, rid))
    return rid, a, b


def test_historical_alias_is_unchanged_and_new_uploads_get_disjoint_physical_sources(db):
    old, _, _ = old_alias(db)
    before = sql(db, 'SELECT batch_id,raw_customer_id,source_system_id,source_customer_id,first_name,app_id,snapshot_id FROM raw.customer ORDER BY batch_id')
    db.commit()
    install(db)
    a, b = upload(db, A), upload(db, B)
    sa, sb = (one(db, 'SELECT source_system_id FROM control.ingestion_batch WHERE batch_id=%s', (x,)) for x in (a,b))
    assert len({1,sa,sb}) == 3
    assert one(db, 'SELECT max(length(source_code)) FROM control.source_system WHERE source_system_id IN (%s,%s)', (sa,sb)) <= 50
    assert [sql(db,'SELECT app_id,error_code FROM control.customer_app_scope_status(%s)', (x,))[0] for x in (a,b)] == [(A,None),(B,None)]
    assert [sql(db,'SELECT app_id,error_code FROM control.resolve_customer_identity_batches(ARRAY[%s]::uuid[])', (x,))[0] for x in (a,b)] == [(A,None),(B,None)]
    assert sql(db, 'SELECT batch_id,raw_customer_id,source_system_id,source_customer_id,first_name,app_id,snapshot_id FROM raw.customer ORDER BY batch_id') == before
    assert one(db, 'SELECT count(DISTINCT raw_customer_id) FROM raw.customer WHERE source_customer_id=%s', ('old-shared',)) == 1
    assert old in [r[1] for r in before]


def stage_and_merge(db, batch, user, first, *, legacy=False):
    source = one(db, 'SELECT source_system_id FROM control.ingestion_batch WHERE batch_id=%s', (batch,))
    sql(db, '''CREATE TEMP TABLE IF NOT EXISTS customer_bulk_stage
        (LIKE raw.customer_current INCLUDING DEFAULTS INCLUDING CONSTRAINTS) ON COMMIT DROP''')
    sql(db, 'TRUNCATE pg_temp.customer_bulk_stage')
    sql(db, '''INSERT INTO pg_temp.customer_bulk_stage
        (batch_id,source_system_id,source_customer_id,first_name)
        VALUES (%s,%s,%s,%s)''', (batch, source, user, first))
    fn = 'raw.apply_customer_bulk' if legacy else 'raw.apply_customer_forward_bulk'
    return one(db, f'SELECT {fn}(%s,%s)', (batch, source))


def setup_source(db):
    sql(db, "INSERT INTO control.source_system(source_system_id,source_code,source_name) VALUES (1,'fixture-1','fixture')")
    sql(db, "SELECT setval('control.source_system_source_system_id_seq'::regclass,1,true)")


def test_installed_045_connector_and_upload_routes_survive_038(connector_db):
    db = connector_db
    import test_connector_identity_publication as connector_fixture
    setup_source(db)
    install(db)
    connector = connector_fixture.connector_batch(db, app=A)
    batch = connector['batch']
    assert sql(db, 'SELECT app_id,error_code FROM control.connector_batch_app_scope(%s)', (batch,)) == [(A,None)]
    assert sql(db, 'SELECT app_id,error_code FROM control.resolve_customer_identity_batches(ARRAY[%s]::uuid[])', (batch,)) == [(A,None)]
    assert sql(db, 'SELECT app_id,error_code FROM control.customer_app_scope_status(%s)', (batch,)) == [(None,'CONNECTOR_EXCLUDED')]
    assert stage_and_merge(db,batch,'connector-key','Connector',legacy=True)==1
    assert one(db,'SELECT app_id FROM raw.customer_current WHERE batch_id=%s',(batch,))==A
    assert one(db, 'SELECT app_id FROM raw.customer WHERE batch_id=%s',(batch,))==A
    assert one(db,"SELECT encode(digest(pg_get_functiondef('control.connector_batch_app_scope(uuid)'::regprocedure),'sha256'),'hex')") == 'da30cb1b59e023479fe20a44f90c3988514bedabe57b643ea9c8cdc8612fed5a'
    assert one(db,"SELECT encode(digest(pg_get_functiondef('control.bind_connector_batch_app_id(uuid,uuid,text)'::regprocedure),'sha256'),'hex')") == '2ca65254b952050cb2919f5912eff2e56845187b33ac07f62feb358b95eaaf25'
    assert one(db,"SELECT pg_get_functiondef('control.resolve_customer_identity_batches(uuid[])'::regprocedure) LIKE '%%connector_batch_app_scope%%'")
    uploaded=upload(db,A)
    assert stage_and_merge(db,uploaded,'upload-key','Upload')==1
    assert sql(db, 'SELECT app_id,error_code FROM control.resolve_customer_identity_batches(ARRAY[%s]::uuid[])', (uploaded,)) == [(A,None)]
    assert one(db,'SELECT source_system_id FROM control.ingestion_batch WHERE batch_id=%s',(uploaded,)) != connector['source']


def test_connector_contract_drift_aborts_without_activation(connector_db):
    db = connector_db
    sql(db, '''CREATE OR REPLACE FUNCTION control.connector_batch_app_scope(p_batch uuid)
        RETURNS TABLE(app_id text,error_code text) LANGUAGE sql STABLE SECURITY DEFINER
        SET search_path=pg_catalog,pg_temp AS $$ SELECT NULL::text,NULL::text $$''')
    db.commit()
    with pytest.raises(psycopg2.Error, match='038 installed 040 connector contract drift'):
        sql(db,MIGRATION.read_text())
    db.rollback()
    assert one(db,"SELECT to_regclass('control.customer_forward_route')") is None


def test_resolver_contract_drift_aborts_without_activation(connector_db):
    db = connector_db
    sql(db, '''CREATE OR REPLACE FUNCTION control.resolve_customer_identity_batches(batch_ids uuid[])
        RETURNS TABLE(ingestion_batch_id uuid,workspace_id uuid,app_id text,error_code text)
        LANGUAGE sql VOLATILE SET search_path=pg_catalog,pg_temp AS $$
        SELECT NULL::uuid,NULL::uuid,NULL::text,NULL::text WHERE false $$''')
    db.commit()
    with pytest.raises(psycopg2.Error, match='038 installed 024/026 function contract drift'):
        sql(db,MIGRATION.read_text())
    db.rollback()
    assert one(db,"SELECT to_regclass('control.customer_forward_route')") is None


def test_connector_binding_rejects_wrong_app_after_038(connector_db):
    db = connector_db
    import test_connector_identity_publication as connector_fixture
    install(db)
    connector = connector_fixture.connector_batch(db, app=None)
    sql(db,'UPDATE platform.workspace SET app_id=%s WHERE workspace_id=%s',(A,connector['workspace']))
    expect_refused(db,'SELECT control.bind_connector_batch_app_id(%s,%s,%s)',
                   (connector['batch'],connector['workspace'],B),'workspace app_id mismatch')
    assert one(db,'SELECT app_id FROM control.ingestion_batch WHERE batch_id=%s',(connector['batch'],)) is None
    assert one(db,'SELECT control.bind_connector_batch_app_id(%s,%s,%s)',
               (connector['batch'],connector['workspace'],A)) == A
    expect_refused(db,"UPDATE control.ingestion_batch SET metadata='{}'::jsonb WHERE batch_id=%s",
                   (connector['batch'],),'connector')


def test_unbound_connector_and_connector_source_upload_fail_closed(connector_db):
    db = connector_db
    import test_connector_identity_publication as connector_fixture
    install(db)
    connector=connector_fixture.connector_batch(db,app=None)
    batch=connector['batch']
    expect_refused(db,'''INSERT INTO raw.customer_current(batch_id,source_system_id,source_customer_id)
        VALUES (%s,%s,'unbound')''',(batch,connector['source']),'connector customer app_id unbound')
    code=one(db,'SELECT source_code FROM control.source_system WHERE source_system_id=%s',(connector['source'],))
    landing=str(uuid4()); ref=f'2026-09-27-120000-{A}-{uuid4().hex[:12]}'
    sql(db,'INSERT INTO control.source_landing_batch(batch_id,source_code,batch_ref) VALUES (%s,%s,%s)',(landing,code,ref))
    expect_refused(db,'''INSERT INTO control.ingestion_batch(batch_id,source_system_id,landing_batch_id,batch_ref)
        VALUES (%s,%s,%s,%s)''',(str(uuid4()),connector['source'],landing,ref),'connector source cannot be used as upload lineage')


def expect_refused(db, statement, args, reason):
    sql(db,'SAVEPOINT expected_refusal')
    with pytest.raises(psycopg2.Error,match=reason):
        changed=sql(db,statement,args)
        assert False, f'Expected rejection; returned {changed!r}'
    sql(db,'ROLLBACK TO SAVEPOINT expected_refusal')


def test_old_bulk_and_direct_file_writes_fail_after_activation(db):
    setup_source(db)
    db.commit()
    install(db)
    b = upload(db,A)
    source=one(db,'SELECT source_system_id FROM control.ingestion_batch WHERE batch_id=%s',(b,))
    sql(db,'SAVEPOINT old_bulk')
    with pytest.raises(psycopg2.Error,match='038 old worker'):
        stage_and_merge(db,b,'fresh','legacy',legacy=True)
    sql(db,'ROLLBACK TO SAVEPOINT old_bulk')
    # Direct legacy source-only paths must also fail (no marker or app GUC).
    expect_refused(db,'''INSERT INTO raw.customer_current(batch_id,source_system_id,source_customer_id)
        VALUES (%s,1,'legacy-direct')''',(b,),'customer source conflicts with batch|038 unrouted or mismatched source write')
    # Simulate the distinct unprivileged application principal. The test owner
    # is superuser and cannot represent a worker at the route-owned guard.
    role='forward_worker_'+uuid4().hex
    sql(db,f'CREATE ROLE {role} NOLOGIN')
    for grant in ('GRANT USAGE ON SCHEMA raw,control TO {role}',
                  'GRANT SELECT ON control.ingestion_batch,control.customer_forward_route TO {role}',
                  'GRANT INSERT ON raw.customer_current,raw.customer TO {role}'):
        sql(db,grant.format(role=role))
    sql(db,f'SET LOCAL ROLE {role}')
    expect_refused(db,'''INSERT INTO raw.customer_current(batch_id,source_system_id,source_customer_id)
        VALUES (%s,%s,'direct')''',(b,source),'direct file write refused')
    expect_refused(db,'''INSERT INTO raw.customer(batch_id,source_system_id,source_customer_id)
        VALUES (%s,%s,'direct-history')''',(b,source),'038 direct file write refused')
    sql(db,'RESET ROLE')
    assert one(db,'SELECT count(*) FROM raw.customer_current')==0
    sql(db,f'SET LOCAL ROLE {role}')
    # Client-set GUCs cannot turn legacy or direct SQL into trusted writer SQL.
    sql(db,"SET LOCAL cdp.customer_source_namespace_writer='file-v1'")
    expect_refused(db,'''INSERT INTO raw.customer_current(batch_id,source_system_id,source_customer_id)
        VALUES (%s,%s,'guc')''',(b,source),'direct file write refused')
    sql(db,'RESET ROLE')
    sql(db,'SAVEPOINT old_bulk_guc')
    with pytest.raises(psycopg2.Error,match='038 old worker'):
        stage_and_merge(db,b,'guc','legacy',legacy=True)
    sql(db,'ROLLBACK TO SAVEPOINT old_bulk_guc')
    db.rollback()


def test_non_owner_with_forward_execute_cannot_directly_write_raw(db):
    setup_source(db)
    db.commit()
    install(db)
    batch_id=upload(db,A)
    source=one(db,'SELECT source_system_id FROM control.ingestion_batch WHERE batch_id=%s',(batch_id,))
    worker='forward_exec_'+uuid4().hex
    sql(db,f'CREATE ROLE {worker} NOLOGIN')
    sql(db,f'GRANT USAGE ON SCHEMA raw,control TO {worker}')
    sql(db,f'GRANT SELECT ON control.ingestion_batch,control.customer_forward_route TO {worker}')
    sql(db,f'GRANT INSERT ON raw.customer_current,raw.customer TO {worker}')
    sql(db,f'GRANT EXECUTE ON FUNCTION raw.apply_customer_forward_bulk(uuid,bigint) TO {worker}')
    sql(db,f'SET LOCAL ROLE {worker}')
    assert one(db,'SELECT has_function_privilege(current_user,%s,\'EXECUTE\')',
               ('raw.apply_customer_forward_bulk(uuid,bigint)',))
    expect_refused(db,'''INSERT INTO raw.customer_current(batch_id,source_system_id,source_customer_id)
        VALUES (%s,%s,'no-direct')''',(batch_id,source),'direct file write refused')
    sql(db,'RESET ROLE')
    assert one(db,'SELECT count(*) FROM raw.customer_current')==0
    assert one(db,'SELECT count(*) FROM raw.customer')==0


def test_non_owner_cannot_insert_routed_history_for_existing_current_id(db):
    setup_source(db)
    db.commit()
    install(db)
    first=upload(db,A)
    assert stage_and_merge(db,first,'same-source-user','first')==1
    second=upload(db,A)
    source=one(db,'SELECT source_system_id FROM control.ingestion_batch WHERE batch_id=%s',(first,))
    assert source==one(db,'SELECT source_system_id FROM control.ingestion_batch WHERE batch_id=%s',(second,))
    raw=one(db,'SELECT raw_customer_id FROM raw.customer_current WHERE source_system_id=%s',(source,))
    worker='forward_history_'+uuid4().hex
    sql(db,f'CREATE ROLE {worker} NOLOGIN')
    sql(db,f'GRANT USAGE ON SCHEMA raw,control TO {worker}')
    sql(db,f'GRANT SELECT ON control.ingestion_batch,control.customer_forward_route TO {worker}')
    sql(db,f'GRANT INSERT ON raw.customer,raw.customer_batch_member TO {worker}')
    sql(db,f'SET LOCAL ROLE {worker}')
    expect_refused(db,'''INSERT INTO raw.customer(raw_customer_id,batch_id,source_system_id,source_customer_id,first_name)
        VALUES (%s,%s,%s,'same-source-user','forged')''',(raw,second,source),'038 direct file write refused')
    expect_refused(db,'''INSERT INTO raw.customer_batch_member(ingestion_batch_id,raw_customer_id,source_system_id,source_customer_id)
        VALUES (%s,%s,%s,'same-source-user')''',(second,raw,source),'038 direct file write refused')
    sql(db,'RESET ROLE')
    assert one(db,'SELECT count(*) FROM raw.customer WHERE batch_id=%s',(second,))==0
    assert one(db,'SELECT count(*) FROM raw.customer_batch_member WHERE ingestion_batch_id=%s',(second,))==0


def test_unrouted_file_and_connector_reclassification_fail_closed(db):
    setup_source(db)
    db.commit()
    install(db)
    b = upload(db,A)
    source=one(db,'SELECT source_system_id FROM control.ingestion_batch WHERE batch_id=%s',(b,))
    expect_refused(db,"UPDATE control.ingestion_batch SET source_system_id=1 WHERE batch_id=%s",(b,),
                   'batch source/app lineage is immutable')
    expect_refused(db,"UPDATE control.source_system SET source_code='reclassified' WHERE source_system_id=%s",(source,),
                   'physical source cannot be reclassified')
    expect_refused(db,"UPDATE control.ingestion_batch SET metadata=metadata || '{\"connector_sync_run_id\":\"fake\"}'::jsonb WHERE batch_id=%s",(b,),
                   'batch source/app lineage is immutable')
    # No matching landing lineage cannot create a route.
    expect_refused(db,"INSERT INTO control.ingestion_batch(batch_id,source_system_id,batch_ref,status) VALUES (%s,1,%s,'completed')",
        (str(uuid4()),f'2026-09-27-120000-{A}-{uuid4().hex[:12]}'),'file landing lineage required')


def test_forward_merge_keeps_global_entity_with_app_local_profiles(db):
    setup_source(db)
    db.commit()
    install(db)
    a, b = upload(db,A), upload(db,B)
    assert stage_and_merge(db,a,'new-shared','A-local') == 1
    assert stage_and_merge(db,b,'new-shared','B-local') == 1
    assert stage_and_merge(db,a,'new-shared','A-local') == 1
    rows = sql(db, '''SELECT c.app_id,c.raw_customer_id,c.first_name,h.app_id,m.source_system_id
        FROM raw.customer_current c JOIN raw.customer h USING(raw_customer_id)
        JOIN raw.customer_batch_member m ON m.raw_customer_id=c.raw_customer_id
        ORDER BY c.app_id''')
    assert len(rows)==2 and rows[0][0]==rows[0][3]==A and rows[1][0]==rows[1][3]==B
    assert rows[0][1]!=rows[1][1] and rows[0][2:]!=rows[1][2:]
    assert one(db,'SELECT count(*) FROM raw.customer WHERE source_customer_id=%s',('new-shared',))==2
    sql(db, '''INSERT INTO staging.customer_profile(raw_customer_id,batch_id,source_system_id,
        source_customer_id,first_name_raw) SELECT raw_customer_id,batch_id,source_system_id,
        source_customer_id,first_name FROM raw.customer_current''')
    entity, job, resolution = (str(uuid4()) for _ in range(3))
    sql(db,'INSERT INTO identity.customer_entity(customer_entity_id) VALUES (%s)',(entity,))
    sql(db,"INSERT INTO control.stitching_job(job_id,ingestion_batch_id,status) VALUES (%s,%s,'RUNNING')",(job,a))
    sql(db,"INSERT INTO identity.resolution_run(run_id,stitching_job_id,status,normalization_version,match_threshold) VALUES (%s,%s,'RUNNING','fixture',0.9)",(resolution,job))
    sql(db,'''INSERT INTO identity.customer_entity_member(standardized_record_id,customer_entity_id,
        source_system_id,source_customer_id,match_method,resolution_run_id) SELECT standardized_record_id,%s,source_system_id,
        source_customer_id,'singleton',%s FROM staging.customer_profile''',(entity,resolution))
    assert one(db,'SELECT count(DISTINCT customer_entity_id) FROM identity.customer_entity_member')==1
    assert sql(db,'''SELECT app_id,first_name_raw FROM staging.customer_profile ORDER BY app_id''')==[(A,'A-local'),(B,'B-local')]


def test_039_logical_winner_after_real_038_upload_with_historical_alias(db):
    """No old/current rewrite: 039 picks the new app-local physical observation."""
    old, old_a, old_b = old_alias(db, historical_dates=True)
    old_rows=sql(db,'''SELECT batch_id,raw_customer_id,source_system_id,app_id,first_name
                       FROM raw.customer WHERE source_system_id=1 ORDER BY batch_id''')
    db.commit()
    install(db)
    # 039's real finalizer and extract_history run in the same task-owned 038 DB.
    import test_customer_app_backfill_logical_winner as logical
    import test_customer_app_backfill as historical
    for migration in ('025_customer_publication_status.sql',
                      '027_customer_app_backfill.sql',
                      '028_customer_app_backfill_access_paths.sql'):
        sql(db,(ROOT/'database/migrations'/migration).read_text())
    assert one(db,"SELECT md5(prosrc) FROM pg_proc WHERE oid='control.finalize_customer_app_backfill()'::regprocedure")==logical.PRIOR_MD5
    sql(db,logical.MIGRATION.read_text())
    db.commit()
    new_landing,new=str(uuid4()),str(uuid4())
    ref=f'2026-09-27-120000-{A}-{uuid4().hex[:12]}'
    sql(db,"INSERT INTO control.source_landing_batch(batch_id,source_code,batch_ref,output_status) VALUES (%s,'fixture-1',%s,'PUBLISHED')",(new_landing,ref))
    sql(db,"INSERT INTO control.ingestion_batch(batch_id,source_system_id,landing_batch_id,batch_ref,status,created_at) VALUES (%s,1,%s,%s,'completed','2026-09-02T00:00:00Z')",(new,new_landing,ref))
    assert stage_and_merge(db,new,'old-shared','new-local')==1
    assert sql(db,'''SELECT batch_id,raw_customer_id,source_system_id,app_id,first_name
                     FROM raw.customer WHERE source_system_id=1 ORDER BY batch_id''')==old_rows
    fresh=sql(db,'''SELECT raw_customer_id::text,source_system_id
                    FROM raw.customer_current WHERE batch_id=%s''',(new,))[0]
    assert fresh[0]!=old and fresh[1]!=1
    db.commit()
    # The fixed 039 SQL winner and real Python stream must agree on the later
    # source without resurrecting the historical aliased current UUID.
    from datetime import datetime,timezone
    from identity_stitcher import app_backfill
    manifest,rows=app_backfill.extract_history(db,A,datetime(2026,9,30,tzinfo=timezone.utc))
    assert len(rows)==1
    assert (rows[0]['batch_id'],rows[0]['raw_customer_id'],rows[0]['source_system_id'],rows[0]['first_name'])==(new,fresh[0],fresh[1],'new-local')
    assert {m['batch_id'] for m in manifest}=={old_a,new}
    db.rollback()
    run,workspace=historical.prepare(db,[old_a,new],app=A)
    historical.record(db,run,workspace,new,fresh[0],user='old-shared',app=A,source=fresh[1])
    historical.finish(db,run)
    assert sql(db,"SELECT status,row_count FROM control.customer_identity_transfer WHERE backfill_run_id=%s",(run,))==[('PENDING',1)]
    assert sql(db,"SELECT source_system_id,raw_customer_id::text FROM control.customer_app_backfill_record WHERE run_id=%s",(run,))==[(fresh[1],fresh[0])]
    assert one(db,'SELECT count(*) FROM identity.customer_entity_member')==0
    assert sql(db,'''SELECT batch_id,raw_customer_id,source_system_id,app_id,first_name
                     FROM raw.customer WHERE source_system_id=1 ORDER BY batch_id''')==old_rows


def test_historical_alias_keeps_new_stitching_fail_closed(db):
    old_alias(db)
    db.commit()
    install(db)
    new_batch=upload(db,A)
    job=str(uuid4())
    # QUEUED is a durable, inert placeholder. Promotion is blocked before the
    # global linker can read historically aliased staging/current attributes.
    sql(db,"INSERT INTO control.stitching_job(job_id,ingestion_batch_id,status) VALUES (%s,%s,'QUEUED')",(job,new_batch))
    expect_refused(db,"UPDATE control.stitching_job SET status='RUNNING' WHERE job_id=%s",(job,),
                   'GLOBAL_ALIAS_STITCH_BLOCKED')
    assert one(db,'SELECT status FROM control.stitching_job WHERE job_id=%s',(job,))=='QUEUED'


def test_new_forward_upload_with_old_raw_alias_stays_fenced(db):
    """An old aliased observation keeps the global matcher paused."""
    old_alias(db)
    db.commit()
    install(db)
    fresh = upload(db, A)
    assert stage_and_merge(db, fresh, 'old-shared', 'Fresh A') == 1
    assert one(db, 'SELECT count(DISTINCT raw_customer_id) FROM raw.customer '
                   'WHERE source_customer_id=%s', ('old-shared',)) == 2
    job = str(uuid4())
    sql(db, "INSERT INTO control.stitching_job(job_id,ingestion_batch_id,status) VALUES (%s,%s,'QUEUED')",
        (job, fresh))
    expect_refused(db,"UPDATE control.stitching_job SET status='RUNNING' WHERE job_id=%s",
                   (job,), 'GLOBAL_ALIAS_STITCH_BLOCKED')
    assert one(db, 'SELECT status FROM control.stitching_job WHERE job_id=%s', (job,)) == 'QUEUED'


def test_ordinary_matcher_loads_separate_app_origins_after_forward_upload(db):
    """Real current/staging read preserves literal app IDs despite old raw alias."""
    old_alias(db)
    db.commit()
    install(db)
    a, b = upload(db, A), upload(db, B)
    assert stage_and_merge(db, a, 'old-shared', 'New A') == 1
    assert stage_and_merge(db, b, 'old-shared', 'New B') == 1
    for batch in (a, b):
        raw = one(db, 'SELECT raw_customer_id FROM raw.customer_current WHERE batch_id=%s', (batch,))
        sql(db, '''INSERT INTO staging.customer_profile
            (standardized_record_id,raw_customer_id,batch_id,source_system_id,
             source_customer_id,standardization_status,standardized_at)
             SELECT gen_random_uuid(),c.raw_customer_id,c.batch_id,c.source_system_id,
                    c.source_customer_id,'READY',clock_timestamp()
             FROM raw.customer_current c WHERE c.raw_customer_id=%s''', (raw,))
    from identity_stitcher.linkage import load_profiles
    frame = load_profiles(db)
    assert {(r.app_id, r.source_system_id, r.source_customer_id)
            for r in frame.itertuples(index=False) if r.source_customer_id == 'old-shared'} >= {
                (A, one(db, 'SELECT source_system_id FROM control.ingestion_batch WHERE batch_id=%s', (a,)), 'old-shared'),
                (B, one(db, 'SELECT source_system_id FROM control.ingestion_batch WHERE batch_id=%s', (b,)), 'old-shared'),
            }


def test_source_codes_never_claim_connector_or_legacy_catalog_id(db):
    setup_source(db)
    code=one(db,"SELECT 'cf1-' || md5(length(%s)::text || ':' || %s || ':' || %s)",(A,A,'fixture-1'))
    sql(db,"INSERT INTO control.source_system(source_system_id,source_code,source_name) VALUES (2,%s,'reserved')",(code,))
    sql(db,"SELECT setval('control.source_system_source_system_id_seq'::regclass,2,true)")
    db.commit()
    install(db)
    expect_refused(db,"INSERT INTO control.ingestion_batch(batch_id,landing_batch_id,source_system_id,batch_ref,status) VALUES (%s,%s,1,%s,'completed')",
        (str(uuid4()),str(uuid4()),f'2026-09-27-120000-{A}-{uuid4().hex[:12]}'),'file landing lineage required')
    landing=str(uuid4());ref=f'2026-09-27-120000-{A}-{uuid4().hex[:12]}'
    sql(db,"INSERT INTO control.source_landing_batch(batch_id,source_code,batch_ref) VALUES (%s,'fixture-1',%s)",(landing,ref))
    expect_refused(db,"SELECT control.customer_forward_file_source(%s::uuid,%s::text,%s::text)",
        (landing,'fixture-1',ref),'physical source collision')
    assert one(db,'SELECT count(*) FROM control.customer_forward_route')==0
    assert one(db,'SELECT count(*) FROM control.source_system')==2


def test_cross_app_same_key_uses_distinct_raw_and_never_reuses_old_alias(db):
    old_alias(db)
    db.commit()
    install(db)
    first, second = upload(db,A), upload(db,B)
    assert stage_and_merge(db,first,'old-shared','A-new')==1
    assert stage_and_merge(db,second,'old-shared','B-new')==1
    rows=sql(db,'''SELECT c.app_id,c.raw_customer_id,c.source_system_id,c.first_name
        FROM raw.customer_current c WHERE c.source_customer_id='old-shared' ORDER BY c.source_system_id''')
    assert rows is not None and len(rows)==3 and len({r[1] for r in rows})==3
    assert {r[0] for r in rows if r[2]!=1}=={A,B}
    assert {r[3] for r in rows if r[2]!=1}=={'A-new','B-new'}
    assert one(db,'SELECT count(*) FROM raw.customer WHERE source_customer_id=%s',('old-shared',))==4


def test_same_app_old_and_new_same_user_retains_old_alias_and_new_physical_raw(db):
    old, _, _ = old_alias(db)
    before = sql(db, '''SELECT batch_id,raw_customer_id,snapshot_id,first_name,app_id
        FROM raw.customer WHERE source_customer_id='old-shared' ORDER BY batch_id''')
    db.commit()
    install(db)
    batch_id = upload(db, A)
    source = one(db,'SELECT source_system_id FROM control.ingestion_batch WHERE batch_id=%s',(batch_id,))
    assert stage_and_merge(db,batch_id,'old-shared','A-new') == 1
    assert sql(db, '''SELECT batch_id,raw_customer_id,snapshot_id,first_name,app_id
        FROM raw.customer WHERE source_customer_id='old-shared' AND source_system_id=1 ORDER BY batch_id''') == before
    assert one(db,'SELECT count(*) FROM raw.customer WHERE batch_id=%s',(batch_id,)) == 1
    current = sql(db, 'SELECT raw_customer_id,first_name,app_id FROM raw.customer_current WHERE source_system_id=%s',(source,))[0]
    assert current[0] != old and current[1:] == ('A-new',A)
    assert one(db,'SELECT count(DISTINCT raw_customer_id) FROM raw.customer WHERE source_customer_id=%s',('old-shared',)) == 2


def test_new_route_preceded_by_no_legacy_source_registration(db):
    install(db)
    landing = str(uuid4())
    ref=f'2026-09-27-120000-{A}-{uuid4().hex[:12]}'
    sql(db, "INSERT INTO control.source_landing_batch(batch_id,source_code,batch_ref) VALUES (%s,'new-file',%s)",(landing,ref))
    source=one(db,'SELECT control.customer_forward_file_source(%s::uuid,%s::text,%s::text)',(landing,'new-file',ref))
    assert one(db,'SELECT count(*) FROM control.source_system') == 1
    assert one(db,'SELECT source_code FROM control.source_system WHERE source_system_id=%s',(source,)).startswith('cf1-')
    ingestion=str(uuid4())
    sql(db,"INSERT INTO control.ingestion_batch(batch_id,landing_batch_id,source_system_id,batch_ref,status) VALUES (%s,%s,%s,%s,'completed')",(ingestion,landing,source,ref))
    assert one(db,'SELECT source_system_id FROM control.ingestion_batch WHERE batch_id=%s',(ingestion,)) == source
    assert sql(db,'SELECT app_id,error_code FROM control.customer_app_scope_status(%s)',(ingestion,))[0] == (A,None)
    assert stage_and_merge(db,ingestion,'first-new','local') == 1
