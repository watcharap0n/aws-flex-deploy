"""Admission regression against an isolated, disposable PostgreSQL database."""
import importlib.util
import os
from pathlib import Path
import sys
import time
import uuid

import psycopg2
import pytest

ROOT = Path(__file__).resolve().parents[2]


def load():
    sys.path.insert(0, str(ROOT / '_lambda/ingestion_data'))
    spec = importlib.util.spec_from_file_location('admission_ingestion', ROOT / '_lambda/ingestion_data/lambda_function.py')
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def db(monkeypatch):
    monkeypatch.setenv('STITCHING_ADMISSION_NOT_BEFORE','1970-01-01T00:00:00+00:00')
    dsn = os.environ['CDP_TEST_DSN']
    conn = psycopg2.connect(dsn)
    assert conn.info.host == '127.0.0.1' and conn.info.port == 55439
    with conn, conn.cursor() as q:
        q.execute('''DROP SCHEMA IF EXISTS control CASCADE; DROP SCHEMA IF EXISTS quality CASCADE;
            CREATE SCHEMA control; CREATE SCHEMA quality;
            CREATE TABLE control.ingestion_batch (batch_id uuid primary key, status text, metadata jsonb, updated_at timestamptz);
            CREATE TABLE control.stitching_job (job_id uuid primary key, ingestion_batch_id uuid, attempt int default 1, status text, ecs_task_arn text, submitted_at timestamptz, completed_at timestamptz, error_code text, error_message text, updated_at timestamptz default now(), created_at timestamptz default now());
            CREATE TABLE quality.customer_validation_run (stitching_job_id uuid, validation_run_id uuid, run_status text default 'PENDING', error_stage text, error_code text);''')
    yield conn
    conn.close()


def seed(conn):
    batch, job = str(uuid.uuid4()), str(uuid.uuid4())
    with conn, conn.cursor() as q:
        q.execute("INSERT INTO control.ingestion_batch VALUES (%s,'completed','{}',now())", (batch,))
        q.execute("INSERT INTO control.stitching_job (job_id,ingestion_batch_id,status) VALUES (%s,%s,'QUEUED')", (job,batch))
        q.execute('INSERT INTO quality.customer_validation_run (stitching_job_id,validation_run_id) VALUES (%s,%s)', (job,str(uuid.uuid4())))
    return batch,job


def test_ingestion_dispatch_leaves_durable_queue_instead_of_launching(monkeypatch, db):
    mod=load();batch,job=seed(db)
    monkeypatch.setattr(mod, '_ensure_stitching_job', lambda *_: {'job_id':job,'status':'QUEUED','task_arn':None})
    launches=[]
    monkeypatch.setattr(mod,'_start_stitching_task', lambda *args: launches.append(args) or 'task-1')
    result=mod._dispatch_stitching({'batch_id':batch},db)
    assert result['status']=='queued'
    assert launches==[]
    with db.cursor() as q:
        q.execute('SELECT status FROM control.stitching_job WHERE job_id=%s',(job,))
        assert q.fetchone()[0]=='QUEUED'


class Ecs:
    def __init__(self):
        self.tasks = {}
        self.calls = []
    def list_tasks(self, **kwargs):
        return {'taskArns': [arn for arn,t in self.tasks.items() if t['lastStatus'] != 'STOPPED']}
    def describe_tasks(self, **kwargs):
        return {'tasks': [self.tasks[a] for a in kwargs['tasks'] if a in self.tasks], 'failures': []}


def admission_module():
    import stitching_admission
    return stitching_admission


def test_real_lock_contention_survives_budget_restart_and_next_job(monkeypatch, db):
    # Unit-scaled legacy wait budget, real wall-clock and two actual PG sessions.
    # The production proof separately holds the real writer for >900 seconds.
    mod=load(); adm=admission_module(); ecs=Ecs()
    with db, db.cursor() as q:
        q.execute((ROOT/'database/migrations/018_stitching_admission.sql').read_text())
    first=seed(db); second=seed(db)
    holder=psycopg2.connect(os.environ['CDP_TEST_DSN'])
    monkeypatch.setattr(mod, 'boto3', type('Aws',(),{'client':staticmethod(lambda _:ecs)}))
    monkeypatch.setenv('ECS_CLUSTER','test')
    monkeypatch.setenv('ECS_TASK_DEFINITION','stitcher:9')
    monkeypatch.setattr(mod,'_stitching_task_request',lambda batch,job: {'clientToken':'stitch-'+job,'taskDefinition':'stitcher:9'})
    def launch(batch,job,**kwargs):
        arn='task-'+job;ecs.calls.append(job)
        ecs.tasks[arn]={'taskArn':arn,'lastStatus':'RUNNING','startedBy':job,'group':'family:cdp-stitcher-task','containers':[]}
        return arn
    monkeypatch.setattr(mod,'_start_stitching_task',launch)
    try:
        with holder.cursor() as q:q.execute('SELECT pg_advisory_lock(%s)',(7413220179,))
        started=time.monotonic()
        while time.monotonic()-started < float(os.getenv('CDP_CONTENTION_SECONDS', '0.3')):
            assert adm.tick(db,mod)['status']=='waiting_for_writer'
            time.sleep(.06)
        assert ecs.calls==[]
        with db.cursor() as q:
            q.execute('SELECT status FROM control.stitching_job ORDER BY created_at')
            assert q.fetchall()==[('QUEUED',),('QUEUED',)]
        # Simulate admission Lambda process restart with a fresh database session.
        with holder.cursor() as q:q.execute('SELECT pg_advisory_unlock(%s)',(7413220179,))
        replacement=psycopg2.connect(os.environ['CDP_TEST_DSN'])
        try:
            assert adm.tick(replacement,mod)['status']=='submitted'
            assert ecs.calls==[first[1]]
            assert adm.tick(db,mod)['status']=='active_task'
            # Writer commits success before ECS stops; next task must still wait.
            with db, db.cursor() as q:q.execute("UPDATE control.stitching_job SET status='SUCCEEDED' WHERE job_id=%s",(first[1],))
            assert adm.tick(db,mod)['status']=='active_task'
            ecs.tasks['task-'+first[1]]['lastStatus']='STOPPED'
            assert adm.tick(db,mod)['status']=='submitted'
            assert ecs.calls==[first[1],second[1]]
            assert adm.tick(replacement,mod)['status']=='active_task'
        finally:replacement.close()
    finally:holder.close()


def test_uncertain_dispatch_reuses_persisted_request_after_restart(monkeypatch, db):
    mod=load();adm=admission_module();ecs=Ecs()
    with db,db.cursor() as q:q.execute((ROOT/'database/migrations/018_stitching_admission.sql').read_text())
    batch,job=seed(db)
    monkeypatch.setattr(mod,'boto3',type('Aws',(),{'client':staticmethod(lambda _:ecs)}))
    monkeypatch.setenv('ECS_CLUSTER','test')
    original={'clientToken':'stitch-'+job,'taskDefinition':'pinned:9'}
    monkeypatch.setattr(mod,'_stitching_task_request',lambda *_:original)
    requests=[]
    def launch(*args,request):
        requests.append(request)
        if len(requests)==1:raise TimeoutError('uncertain response')
        return 'task-1'
    monkeypatch.setattr(mod,'_start_stitching_task',launch)
    with pytest.raises(TimeoutError):adm.tick(db,mod)
    with db,db.cursor() as q:
        q.execute('SELECT status FROM control.stitching_job WHERE job_id=%s',(job,));assert q.fetchone()[0]=='QUEUED'
        q.execute("UPDATE control.stitching_admission SET next_attempt_at=now()-interval '1 second'")
    monkeypatch.setattr(mod,'_stitching_task_request',lambda *_:pytest.fail('must retain immutable request'))
    replacement=psycopg2.connect(os.environ['CDP_TEST_DSN'])
    try:assert adm.tick(replacement,mod)['status']=='submitted'
    finally:replacement.close()
    assert requests==[original,original]


def test_duplicate_status_checks_never_report_queued_as_succeeded(db):
    load();adm=admission_module();batch,job=seed(db)
    with db,db.cursor() as q:q.execute((ROOT/'database/migrations/018_stitching_admission.sql').read_text())
    for _ in range(3):assert adm.job_status(db,batch,job)['status']=='QUEUED'
    with pytest.raises(ValueError,match='lineage'):adm.job_status(db,str(uuid.uuid4()),job)


def test_launched_task_is_adopted_after_uncertain_response(monkeypatch, db):
    mod=load();adm=admission_module();ecs=Ecs()
    with db,db.cursor() as q:q.execute((ROOT/'database/migrations/018_stitching_admission.sql').read_text())
    batch,job=seed(db)
    with db,db.cursor() as q:q.execute('INSERT INTO control.stitching_admission(job_id,request) VALUES (%s,%s)',(job,'{}'))
    ecs.tasks['task-1']={'taskArn':'task-1','lastStatus':'RUNNING','startedBy':job,'group':'family:cdp-stitcher-task','containers':[]}
    monkeypatch.setattr(mod,'boto3',type('Aws',(),{'client':staticmethod(lambda _:ecs)}))
    monkeypatch.setenv('ECS_CLUSTER','test')
    assert adm.tick(db,mod)['status']=='active_task'
    with db.cursor() as q:
        q.execute('SELECT ecs_task_arn,status FROM control.stitching_job WHERE job_id=%s',(job,))
        assert q.fetchone()==('task-1','SUBMITTED')


def test_ecs_failure_array_is_conclusive_not_uncertain(monkeypatch,db):
    mod=load();adm=admission_module();ecs=Ecs()
    with db,db.cursor() as q:q.execute((ROOT/'database/migrations/018_stitching_admission.sql').read_text())
    batch,job=seed(db)
    monkeypatch.setenv('ECS_CLUSTER','test')
    monkeypatch.setattr(mod,'boto3',type('Aws',(),{'client':staticmethod(lambda _:ecs)}))
    monkeypatch.setattr(mod,'_stitching_task_request',lambda *_:{'clientToken':'x'})
    ecs.run_task=lambda **kw:{'tasks':[],'failures':[{'arn':'x','reason':'RESOURCE:MEMORY'}]}
    def failed(conn,batch_id,job_id,error):
        with conn,conn.cursor() as q:q.execute("UPDATE control.stitching_job SET status='FAILED' WHERE job_id=%s",(job_id,))
    monkeypatch.setattr(mod,'_mark_stitching_dispatch_failed',failed)
    with pytest.raises(RuntimeError):adm.tick(db,mod)
    assert adm.job_status(db,batch,job)['status']=='FAILED'


def test_ambiguous_budget_expiry_fences_without_false_failure(monkeypatch,db):
    mod=load();adm=admission_module();ecs=Ecs()
    with db,db.cursor() as q:q.execute((ROOT/'database/migrations/018_stitching_admission.sql').read_text())
    batch,job=seed(db);seed(db)
    with db,db.cursor() as q:q.execute("INSERT INTO control.stitching_admission(job_id,request,dispatch_attempts) VALUES (%s,'{}',20)",(job,))
    monkeypatch.setenv('ECS_CLUSTER','test')
    monkeypatch.setattr(mod,'boto3',type('Aws',(),{'client':staticmethod(lambda _:ecs)}))
    monkeypatch.setattr(mod,'_start_stitching_task',lambda *a,**kw:pytest.fail('must not launch around uncertainty'))
    with pytest.raises(RuntimeError,match='budget'):adm.tick(db,mod)
    assert adm.job_status(db,batch,job)['status']=='BLOCKED'
    with pytest.raises(RuntimeError,match='blocked'):adm.tick(db,mod)
    with db.cursor() as q:
        q.execute('SELECT status FROM control.stitching_job')
        assert q.fetchall()==[('QUEUED',),('QUEUED',)]


def test_definitive_dispatch_error_is_terminal_without_blocking_next(monkeypatch,db):
    from botocore.exceptions import ClientError
    mod=load();adm=admission_module();ecs=Ecs()
    with db,db.cursor() as q:q.execute((ROOT/'database/migrations/018_stitching_admission.sql').read_text())
    batch,job=seed(db);second=seed(db)
    monkeypatch.setenv('ECS_CLUSTER','test')
    monkeypatch.setattr(mod,'boto3',type('Aws',(),{'client':staticmethod(lambda _:ecs)}))
    monkeypatch.setattr(mod,'_stitching_task_request',lambda *_:{'clientToken':'x'})
    def launch(*args,**kwargs):raise ClientError({'Error':{'Code':'AccessDeniedException','Message':'denied'}},'RunTask')
    monkeypatch.setattr(mod,'_start_stitching_task',launch)
    def failed(conn,batch_id,job_id,error):
        with conn,conn.cursor() as q:q.execute("UPDATE control.stitching_job SET status='FAILED',error_code='AccessDeniedException' WHERE job_id=%s",(job_id,))
    monkeypatch.setattr(mod,'_mark_stitching_dispatch_failed',failed)
    with pytest.raises(ClientError):adm.tick(db,mod)
    assert adm.job_status(db,batch,job)['status']=='FAILED'
    monkeypatch.setattr(mod,'_start_stitching_task',lambda *a,**kw:'next-task')
    assert adm.tick(db,mod)['job_id']==second[1]


def test_status_follows_retry_chain_and_hides_transient_lock_failure(db):
    load();adm=admission_module();batch,job=seed(db)
    with db,db.cursor() as q:
        q.execute((ROOT/'database/migrations/018_stitching_admission.sql').read_text())
        q.execute("UPDATE control.stitching_job SET status='FAILED',error_code='TimeoutError' WHERE job_id=%s",(job,))
        q.execute("UPDATE quality.customer_validation_run SET error_stage='LOCK' WHERE stitching_job_id=%s",(job,))
        q.execute("INSERT INTO control.stitching_admission(job_id,request) VALUES (%s,'{}')",(job,))
    assert adm.job_status(db,batch,job)['status']=='RETRY_PENDING'
    replacement=str(uuid.uuid4())
    with db,db.cursor() as q:
        q.execute("INSERT INTO control.stitching_job(job_id,ingestion_batch_id,status) VALUES (%s,%s,'SUCCEEDED')",(replacement,batch))
        q.execute('UPDATE control.stitching_admission SET retry_job_id=%s WHERE job_id=%s',(replacement,job))
    result=adm.job_status(db,batch,job)
    assert result['status']=='SUCCEEDED' and result['job_id']==replacement


def test_unmanaged_historical_jobs_do_not_block_current_admission(monkeypatch,db):
    mod=load();adm=admission_module();ecs=Ecs()
    with db,db.cursor() as q:q.execute((ROOT/'database/migrations/018_stitching_admission.sql').read_text())
    old_batch,old=seed(db);batch,job=seed(db)
    with db,db.cursor() as q:q.execute("UPDATE control.stitching_job SET status='RUNNING',created_at='2020-01-01' WHERE job_id=%s",(old,))
    monkeypatch.setenv('STITCHING_ADMISSION_NOT_BEFORE','2026-09-17T00:00:00+07:00')
    monkeypatch.setenv('ECS_CLUSTER','test')
    monkeypatch.setattr(mod,'boto3',type('Aws',(),{'client':staticmethod(lambda _:ecs)}))
    monkeypatch.setattr(mod,'_stitching_task_request',lambda *_:{'clientToken':'x'})
    monkeypatch.setattr(mod,'_start_stitching_task',lambda *args,**kw:'task-1')
    assert adm.tick(db,mod)['job_id']==job


def test_contention_failure_requeues_lineage_but_real_failure_stays_visible(monkeypatch,db):
    mod=load();adm=admission_module();ecs=Ecs()
    with db,db.cursor() as q:
        q.execute((ROOT/'database/migrations/018_stitching_admission.sql').read_text())
    batch,job=seed(db)
    with db,db.cursor() as q:
        q.execute("UPDATE control.stitching_job SET status='FAILED',error_code='TimeoutError',ecs_task_arn='old-task' WHERE job_id=%s",(job,))
        q.execute("UPDATE quality.customer_validation_run SET error_stage='LOCK',error_code='TimeoutError' WHERE stitching_job_id=%s",(job,))
        q.execute("INSERT INTO control.stitching_admission(job_id,request,state,task_arn) VALUES (%s,'{}','LAUNCHED','old-task')",(job,))
    ecs.tasks['old-task']={'taskArn':'old-task','lastStatus':'STOPPED','containers':[{'exitCode':1}]}
    monkeypatch.setattr(mod,'boto3',type('Aws',(),{'client':staticmethod(lambda _:ecs)}))
    monkeypatch.setenv('ECS_CLUSTER','test')
    calls=[]
    monkeypatch.setattr(mod,'_ensure_stitching_job',lambda _,b: calls.append(b) or {'job_id':str(uuid.uuid4())})
    adm.tick(db,mod)
    assert calls==[batch]
    calls.clear();adm.tick(db,mod);assert calls==[]
