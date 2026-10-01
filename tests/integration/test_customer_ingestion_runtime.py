"""Real PostgreSQL control-plane tests; disposable local DB only."""
import importlib.util
import os
from pathlib import Path
import sys

import psycopg2
import pytest
from psycopg2.extensions import parse_dsn

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT/'_lambda/ingestion_data'))
import customer_ingestion_runtime as runtime

DSN = os.getenv('CUSTOMER_RUNTIME_TEST_DSN', 'host=127.0.0.1 port=59775 user=postgres dbname=customer_ingestion_runtime_test')


@pytest.fixture
def db():
    params = parse_dsn(DSN)
    assert params['host'] == '127.0.0.1' and params['dbname'] == 'customer_ingestion_runtime_test'
    c = psycopg2.connect(DSN)
    with c, c.cursor() as q:
        q.execute('DROP SCHEMA IF EXISTS control CASCADE; CREATE SCHEMA control; CREATE TABLE control.ingestion_batch(batch_id uuid primary key, status text)')
    yield c
    c.close()


def activate(db, mode='one_shot'):
    migration = ROOT/'database/migrations/023_customer_ingestion_runtime.sql'
    assert migration.exists(), 'runtime durable schema missing'
    with db, db.cursor() as q:
        q.execute(migration.read_text())
        q.execute("UPDATE control.customer_ingestion_runtime SET enabled=true, mode=%s", (mode,))


def event(key='customers.csv'):
    return {'eventTime': '2026-09-18T00:00:00Z', 's3': {'bucket': {'name': 'synthetic'}, 'object': {'key': 'raw/crm/'+key, 'eTag': 'etag', 'versionId': 'v1'}}}


def request(attempt):
    return {'clientToken': attempt, 'startedBy': attempt, 'taskDefinition': 'ingestion:1'}


def scalar(db, sql, args=None):
    with db, db.cursor() as q:
        q.execute(sql, args)
        return q.fetchone()[0]


def test_duplicate_event_persists_one_job_and_bounded_attempt_before_launch(db):
    activate(db)
    first = runtime.enqueue(db, event())
    assert runtime.enqueue(db, event())['job_id'] == first['job_id']
    runtime.enqueue(db, event('second.csv'))
    attempt = runtime.reserve(db, request)
    assert attempt['job_id'] == first['job_id']
    assert attempt['request']['clientToken'] == attempt['attempt_id']
    other = psycopg2.connect(DSN)
    try:
        assert scalar(other, 'SELECT count(*) FROM control.customer_ingestion_job') == 2
        assert scalar(other, 'SELECT count(*) FROM control.customer_ingestion_attempt') == 1
        assert runtime.reserve(other, request) is None
    finally:
        other.close()


def test_owner_fencing_heartbeat_stopped_retry_and_atomic_completion(db):
    activate(db)
    job = runtime.enqueue(db, event())
    attempt = runtime.reserve(db, request)
    assert hasattr(runtime, 'claim'), 'durable owner claim missing'
    owner = '11111111-1111-1111-1111-111111111111'
    assert runtime.claim(db, attempt['attempt_id'], owner, 'task/first')['event'] == event()
    assert runtime.claim(db, attempt['attempt_id'], owner, 'task/first') is None
    assert runtime.heartbeat(db, attempt['attempt_id'], owner)
    assert not runtime.heartbeat(db, attempt['attempt_id'], '22222222-2222-2222-2222-222222222222')
    with db, db.cursor() as q:
        q.execute("CREATE TABLE control.payload(value int)")
    # STOPPED only terminates this attempt; a delayed old event cannot kill retry.
    assert runtime.stopped(db, {'taskArn': 'task/first', 'startedBy': attempt['attempt_id'], 'lastStatus':'STOPPED'})['status'] == 'QUEUED'
    with db, db.cursor() as q:
        q.execute("UPDATE control.customer_ingestion_job SET available_at=now()")
    retry = runtime.reserve(db, request)
    assert retry['attempt'] == 2
    assert runtime.claim(db, retry['attempt_id'], owner, 'task/retry')
    with pytest.raises(runtime.LeaseLost):
        with db, db.cursor() as q:
            q.execute('INSERT INTO control.payload VALUES (1)')
            runtime.complete(q, attempt['attempt_id'], owner, '33333333-3333-3333-3333-333333333333')
    assert scalar(db, 'SELECT count(*) FROM control.payload') == 0
    runtime.stopped(db, {'taskArn': 'task/first', 'startedBy': attempt['attempt_id'], 'lastStatus':'STOPPED'})
    batch = '33333333-3333-3333-3333-333333333333'
    with db, db.cursor() as q:
        q.execute("INSERT INTO control.ingestion_batch VALUES (%s,'completed')", (batch,))
        q.execute('INSERT INTO control.payload VALUES (2)')
        runtime.complete(q, retry['attempt_id'], owner, batch)
    assert scalar(db, 'SELECT status FROM control.customer_ingestion_job') == 'SUCCEEDED'
    assert runtime.enqueue(db,event())['job_id'] == job['job_id']
    assert runtime.reserve(db,request) is None
    runtime.stopped(db, {'taskArn':'task/retry','lastStatus':'STOPPED'})
    assert scalar(db, 'SELECT status FROM control.customer_ingestion_job') == 'SUCCEEDED'


def test_ambiguous_dispatch_reuses_committed_intent_and_never_blind_retries(db):
    activate(db)
    runtime.enqueue(db, event())
    assert hasattr(runtime, 'tick'), 'external dispatch reconciler missing'
    class ECS:
        calls = []
        def run_task(self, **kwargs):
            self.calls.append(kwargs)
            if len(self.calls) == 1:
                raise TimeoutError('response lost')
            return {'tasks':[{'taskArn':'task/recovered'}]}
        def describe_tasks(self, **kwargs):
            return {'tasks':[{'taskArn':'task/recovered','lastStatus':'STOPPED','containers':[{'exitCode':137}]}]}
    ecs = ECS()
    runtime.tick(db,ecs,request,cluster='test')
    assert scalar(db, 'SELECT count(*) FROM control.customer_ingestion_attempt') == 1
    runtime.tick(db,ecs,request,cluster='test')
    assert len(ecs.calls) == 2 and ecs.calls[0] == ecs.calls[1]
    assert scalar(db, 'SELECT status FROM control.customer_ingestion_attempt') == 'SUBMITTED'
    runtime.tick(db,ecs,request,cluster='test')
    assert scalar(db, 'SELECT status FROM control.customer_ingestion_job') == 'QUEUED'
    with db, db.cursor() as q:
        q.execute('UPDATE control.customer_ingestion_job SET available_at=now()')
    next_attempt = runtime.reserve(db,request)
    with db, db.cursor() as q:
        q.execute("UPDATE control.customer_ingestion_attempt SET created_at=now()-interval '11 minutes' WHERE attempt_id=%s",(next_attempt['attempt_id'],))
    runtime.tick(db,ecs,request,cluster='test')
    assert scalar(db,"SELECT status FROM control.customer_ingestion_attempt WHERE attempt_id=%s",(next_attempt['attempt_id'],)) == 'UNKNOWN'
    assert len(ecs.calls) == 2
    assert runtime.reserve(db,request) is None


def test_process_record_completion_fence_rolls_back_whole_payload(db, monkeypatch):
    activate(db)
    spec = importlib.util.spec_from_file_location('atomic_ingestion', ROOT/'_lambda/ingestion_data/lambda_function.py')
    ingestion = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ingestion)
    import inspect
    assert 'before_commit' in inspect.signature(ingestion._process_record).parameters, 'atomic completion hook missing'
    configure_payload(db, ingestion, monkeypatch)
    runtime.enqueue(db,event())
    attempt = runtime.reserve(db,request)
    owner = '11111111-1111-1111-1111-111111111111'
    runtime.claim(db,attempt['attempt_id'],owner,'task/atomic')
    runtime.stopped(db,{'taskArn':'task/atomic','lastStatus':'STOPPED'})
    from types import SimpleNamespace
    customers,stats = ingestion.prepare_customers(b'source_customer_id,email\nx,a@example.test\n')
    prepared = (customers,stats,'checksum',{'landing-batch-id':'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'})
    with pytest.raises(runtime.LeaseLost):
        ingestion._process_record(event(),SimpleNamespace(),db,prepared=prepared,
            before_commit=lambda q,b: runtime.complete(q,attempt['attempt_id'],owner,b))
    assert scalar(db,'SELECT count(*) FROM control.payload') == 0
    assert scalar(db,'SELECT count(*) FROM control.ingestion_batch') == 0
    assert scalar(db,'SELECT count(*) FROM control.stitching_job') == 0


def configure_payload(db, ingestion, monkeypatch):
    with db, db.cursor() as q:
        q.execute('''
          ALTER TABLE control.ingestion_batch ADD source_system_id int, ADD object_name text,
            ADD file_name text, ADD checksum text, ADD metadata jsonb, ADD batch_ref text,
            ADD landing_batch_id uuid, ADD completed_at timestamptz, ADD updated_at timestamptz,
            ADD records_received int, ADD records_loaded int, ADD records_rejected int;
          ALTER TABLE control.ingestion_batch ADD UNIQUE(source_system_id,object_name,checksum);
          CREATE TABLE control.source_system(source_system_id serial PRIMARY KEY, source_code text UNIQUE,source_name text,source_type text,is_active boolean);
          CREATE TABLE control.stitching_job(job_id uuid DEFAULT gen_random_uuid(),ingestion_batch_id uuid,attempt int,status text);
          DROP SCHEMA IF EXISTS quality CASCADE; CREATE SCHEMA quality;
          CREATE TABLE quality.customer_validation_run(validation_run_id text,selected_record_count int,existing_source_record_count int,updated_at timestamptz,run_status text);
          CREATE TABLE quality.customer_validation_record(validation_run_id text,existing_source_record boolean,selected_for_canonical boolean);
          INSERT INTO quality.customer_validation_run VALUES ('validation',0,0,now(),'PENDING');
          CREATE TABLE control.payload(value text);
        ''')
    patch_payload(ingestion, monkeypatch)


def patch_payload(ingestion, monkeypatch):
    monkeypatch.setattr(ingestion,'_resolve_landing_lineage',lambda *a,**kw: ('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa','ref'))
    monkeypatch.setattr(ingestion,'_create_validation_run',lambda *a,**kw:'validation')
    for name in ('_create_existing_source_snapshot','_seed_validation_records','_record_batch_members'):
        monkeypatch.setattr(ingestion,name,lambda *a,**kw: None)
    def upsert(q,customers,*args):
        for customer in customers:
            q.execute('INSERT INTO control.payload VALUES (%s)',(customer['source_customer_id'],))
    monkeypatch.setattr(ingestion,'_upsert_customers',upsert)


def test_landing_stage_has_separate_idempotency_and_fenced_completion(db):
    activate(db)
    canonical=runtime.enqueue(db,event())
    original=event()
    original['s3']['object']['key']='data_sources/crm/batch/customers/source.csv'
    landing=runtime.enqueue(db,original,stage='landing')
    assert landing['job_id']!=canonical['job_id']
    assert runtime.enqueue(db,original,stage='landing')['job_id']==landing['job_id']
    attempt=runtime.reserve(db,request)
    owner='11111111-1111-1111-1111-111111111111'
    runtime.claim(db,attempt['attempt_id'],owner,'task/landing')
    with db,db.cursor() as q:
        runtime.fence(q,attempt['attempt_id'],owner)
    runtime.stopped(db,{'taskArn':'task/landing','lastStatus':'STOPPED'})
    with pytest.raises(runtime.LeaseLost):
        with db,db.cursor() as q:
            runtime.fence(q,attempt['attempt_id'],owner)


def test_expired_lease_revokes_owner_but_retains_slot_until_stopped(db):
    activate(db)
    runtime.enqueue(db,event())
    attempt=runtime.reserve(db,request)
    owner='11111111-1111-1111-1111-111111111111'
    runtime.claim(db,attempt['attempt_id'],owner,'task/expired')
    with db,db.cursor() as q:
        q.execute("UPDATE control.customer_ingestion_attempt SET lease_until=now()-interval '1 second'")
    calls=[]
    class ECS:
        def describe_tasks(self,**kw):
            return {'tasks':[{'taskArn':'task/expired','lastStatus':'RUNNING'}]}
        def stop_task(self,**kw):
            calls.append(kw)
    runtime.tick(db,ECS(),request,cluster='cluster')
    assert scalar(db,'SELECT status FROM control.customer_ingestion_attempt')=='STOPPING'
    assert not runtime.heartbeat(db,attempt['attempt_id'],owner)
    assert runtime.reserve(db,request) is None
    assert len(calls)==1
    runtime.stopped(db,{'taskArn':'task/expired','lastStatus':'STOPPED'})
    assert scalar(db,'SELECT status FROM control.customer_ingestion_job')=='QUEUED'


def test_launch_failure_is_persisted_without_sensitive_exception_text(db):
    activate(db)
    runtime.enqueue(db,event())
    class ECS:
        def run_task(self,**kw):
            raise TimeoutError('sensitive customer text')
    runtime.tick(db,ECS(),request,cluster='cluster')
    assert scalar(db,'SELECT error_code FROM control.customer_ingestion_attempt')=='ECS_LAUNCH_AMBIGUOUS'


def test_stopped_retry_budget_and_warm_mode(db):
    activate(db,mode='warm')
    with db,db.cursor() as q:
        q.execute('UPDATE control.customer_ingestion_runtime SET max_attempts=1')
    runtime.enqueue(db,event())
    assert runtime.reserve(db,request) is None
    attempt=runtime.reserve(db,None,mode='warm')
    assert attempt['request'] is None
    owner='11111111-1111-1111-1111-111111111111'
    runtime.claim(db,attempt['attempt_id'],owner,'task/warm')
    assert runtime.fail(db,attempt['attempt_id'],owner,'ValueError')['status']=='FAILED'
    assert runtime.reserve(db,None,mode='warm') is None


def test_lowering_capacity_does_not_admit_under_occupied_higher_slot(db):
    activate(db)
    with db,db.cursor() as q:
        q.execute('UPDATE control.customer_ingestion_runtime SET max_active=2')
    runtime.enqueue(db,event('one.csv')); runtime.enqueue(db,event('two.csv')); runtime.enqueue(db,event('three.csv'))
    first=runtime.reserve(db,request); second=runtime.reserve(db,request)
    runtime.stopped(db,{'startedBy':first['attempt_id'],'lastStatus':'STOPPED'})
    with db,db.cursor() as q:
        q.execute('UPDATE control.customer_ingestion_runtime SET max_active=1')
    assert second['slot']==2
    assert runtime.reserve(db,request) is None


def test_successful_owner_heartbeat_after_commit_is_not_lease_loss(db):
    activate(db)
    runtime.enqueue(db,event())
    attempt=runtime.reserve(db,request)
    owner='11111111-1111-1111-1111-111111111111'
    runtime.claim(db,attempt['attempt_id'],owner,'task/success')
    batch='33333333-3333-3333-3333-333333333333'
    with db,db.cursor() as q:
        q.execute("INSERT INTO control.ingestion_batch VALUES (%s,'completed')",(batch,))
        runtime.complete(q,attempt['attempt_id'],owner,batch)
    assert runtime.heartbeat(db,attempt['attempt_id'],owner), 'post-COMMIT cleanup must not be cancelled'
    assert not runtime.heartbeat(db,attempt['attempt_id'],'22222222-2222-2222-2222-222222222222')
    assert runtime.stopped(db,{'taskArn':'task/success','lastStatus':'STOPPED','containers':[{'exitCode':137}]})['status']=='unchanged'


@pytest.mark.parametrize('stage,key',[('landing','raw/crm/customer.csv'),('canonical','data_sources/crm/b/customers/source.csv')])
def test_enqueue_rejects_wrong_stage_identity(db,stage,key):
    activate(db)
    record=event()
    record['s3']['object']['key']=key
    with pytest.raises(ValueError):
        runtime.enqueue(db,record,stage=stage)
    assert scalar(db,'SELECT count(*) FROM control.customer_ingestion_job')==0


def test_status_action_reports_durable_job_not_pipeline_success(db,monkeypatch):
    activate(db)
    job=runtime.enqueue(db,event())
    spec=importlib.util.spec_from_file_location('runtime_status_ingestion',ROOT/'_lambda/ingestion_data/lambda_function.py')
    ingestion=importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ingestion)
    monkeypatch.setattr(ingestion,'_connect',lambda:psycopg2.connect(DSN))
    monkeypatch.setenv('CUSTOMER_INGESTION_ENABLED','true')
    result=ingestion.lambda_handler({'action':'customer_ingestion_job_status','job_id':job['job_id']},None)
    assert result['status']=='QUEUED' and result['stage']=='canonical'
    assert result['job_id']==job['job_id'] and result['ingestion_batch_id'] is None
    assert 'event' not in result and 'bucket' not in result
    with pytest.raises(ValueError,match='not found'):
        ingestion.lambda_handler({'action':'customer_ingestion_job_status','job_id':'11111111-1111-1111-1111-111111111111'},None)


@pytest.mark.parametrize('operation',['heartbeat','fence','complete'])
def test_expiry_is_checked_after_waiting_for_attempt_lock(db,operation):
    from concurrent.futures import ThreadPoolExecutor
    import time
    activate(db)
    runtime.enqueue(db,event())
    attempt=runtime.reserve(db,request)
    owner='11111111-1111-1111-1111-111111111111'
    runtime.claim(db,attempt['attempt_id'],owner,'task/expiry')
    with db,db.cursor() as q:
        q.execute("UPDATE control.customer_ingestion_attempt SET lease_until=clock_timestamp()+interval '0.3 second'")
    other=psycopg2.connect(DSN)
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            with db.cursor() as q:
                q.execute('SELECT * FROM control.customer_ingestion_attempt FOR UPDATE')
            def action():
                if operation=='heartbeat':
                    return runtime.heartbeat(other,attempt['attempt_id'],owner)
                try:
                    with other,other.cursor() as cursor:
                        if operation=='fence':
                            runtime.fence(cursor,attempt['attempt_id'],owner)
                        else:
                            runtime.complete(cursor,attempt['attempt_id'],owner,None)
                    return True
                except runtime.LeaseLost:
                    return False
            pending=pool.submit(action)
            time.sleep(.5)
            db.commit()
            assert pending.result(timeout=3) is False
    finally:
        other.close()


def test_long_payload_transaction_keeps_independent_heartbeat_live(db):
    import time
    activate(db)
    runtime.enqueue(db,event())
    attempt=runtime.reserve(db,request)
    owner='11111111-1111-1111-1111-111111111111'
    runtime.claim(db,attempt['attempt_id'],owner,'task/long')
    from test_customer_ingestion_worker import load_worker
    worker=load_worker()
    with db,db.cursor() as q:
        q.execute("UPDATE control.customer_ingestion_attempt SET lease_until=clock_timestamp()+interval '0.1 second'")
    before=scalar(db,'SELECT heartbeat_at FROM control.customer_ingestion_attempt')
    with worker.Heartbeat(lambda:psycopg2.connect(DSN),attempt['attempt_id'],owner,db,.02) as heartbeat:
        with db,db.cursor() as q:
            q.execute('SELECT pg_sleep(0.15)')
            heartbeat.check()
        assert scalar(db,'SELECT heartbeat_at FROM control.customer_ingestion_attempt')>before
    other=psycopg2.connect(DSN)
    try:
        with db,db.cursor() as q:
            q.execute("CREATE TABLE control.payload(value int)")
        with db,db.cursor() as q:
            q.execute('INSERT INTO control.payload VALUES (1)')
            q.execute('SELECT pg_sleep(0.05)')
            assert runtime.heartbeat(other,attempt['attempt_id'],owner)
            q.execute('SELECT pg_sleep(0.05)')
            assert runtime.heartbeat(other,attempt['attempt_id'],owner)
            runtime.fence(q,attempt['attempt_id'],owner)
        assert scalar(db,'SELECT count(*) FROM control.payload')==1
    finally:
        other.close()

