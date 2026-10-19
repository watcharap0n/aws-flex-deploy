"""Execute the actual worker against PG; transport alone is faked."""
import importlib.util
import io
import multiprocessing
import logging
import os
from pathlib import Path
import signal
import sys
import time

import psycopg2
import pytest

from test_customer_ingestion_runtime import ROOT, DSN, db, activate, event, request, scalar, configure_payload, patch_payload

sys.path.insert(0,str(ROOT/'_lambda/ingestion_data'))
sys.path.insert(0,str(ROOT/'_workers/customer_ingestion'))
import customer_ingestion_runtime as runtime


def load_worker():
    # Both Lambda directories have lambda_function.py; collection order must
    # never choose landing for the worker's canonical ingestion dependency.
    spec_ingestion = importlib.util.spec_from_file_location('lambda_function',ROOT/'_lambda/ingestion_data/lambda_function.py')
    ingestion = importlib.util.module_from_spec(spec_ingestion)
    sys.modules['lambda_function'] = ingestion
    spec_ingestion.loader.exec_module(ingestion)
    path = ROOT/'_workers/customer_ingestion/worker.py'
    assert path.exists(), 'ECS worker executable missing'
    spec = importlib.util.spec_from_file_location('tested_customer_worker',path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_db_failure_logs_only_safe_constraint_metadata(caplog):
    worker = load_worker()
    class Diagnostic:
        constraint_name = 'customer_status_check'
        table_name = 'customer_current'
        schema_name = 'raw'
    class DatabaseError(Exception):
        diag = Diagnostic()
    with caplog.at_level(logging.ERROR):
        worker.log_failure(DatabaseError('sensitive customer row'))
    assert 'customer_status_check' in caplog.text
    assert 'sensitive customer row' not in caplog.text


class S3:
    def get_object(self, **kwargs):
        assert kwargs['VersionId']=='v1' and kwargs['IfMatch']=='etag'
        return {'Body': io.BytesIO(b'source_customer_id,email\nx,a@example.test\ny,b@example.test\nx,new@example.test\n'),
                'ETag':'etag','VersionId':'v1',
                'Metadata': {'landing-batch-id':'aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa','source-code':'crm','content-sha256':'unused','row-count':'2'}}


@pytest.mark.parametrize('changed',[{'ETag':'wrong'},{'VersionId':'wrong'},{'ContentLength':1}])
def test_canonical_download_rejects_changed_identity(tmp_path,changed):
    from types import SimpleNamespace
    worker=load_worker()
    response=S3().get_object(VersionId='v1',IfMatch='etag')
    response.update(changed)
    with pytest.raises(ValueError):
        worker.prepare(event(),SimpleNamespace(get_object=lambda **kw:response),tmp_path,SimpleNamespace(check=lambda:None))
    assert response['Body'].closed


def child_run(attempt_id, mode, ready):
    import worker
    patch_payload(worker.ingestion,pytest.MonkeyPatch())
    def connect():
        return psycopg2.connect(DSN)
    original = worker.runtime.complete
    def complete(q,*args):
        if mode=='before_commit':
            ready.set()
            time.sleep(60)
        result=original(q,*args)
        if mode=='during_commit':
            ready.set()
        return result
    worker.runtime.complete=complete
    original_process=worker.ingestion._process_record
    def process(*a,**kw):
        result=original_process(*a,**kw)
        if mode=='after_commit':
            ready.set()
            time.sleep(60)
        return result
    worker.ingestion._process_record=process
    worker.run_attempt(attempt_id,connect=connect,s3=S3(),task_arn='task/'+attempt_id,heartbeat_seconds=.1)


@pytest.mark.parametrize('kill_stage',['before_commit','during_commit','after_commit'])
def test_sigkill_payload_and_ack_boundaries(db,monkeypatch,kill_stage):
    worker=load_worker()
    # Spawn: macOS libpq/SSL cannot be safely inherited across a threaded fork.
    sys.modules['worker']=worker
    activate(db)
    configure_payload(db,worker.ingestion,monkeypatch)
    if kill_stage=='during_commit':
        with db,db.cursor() as q:
            q.execute("""CREATE FUNCTION control.slow_commit() RETURNS trigger LANGUAGE plpgsql AS $$
              BEGIN IF NEW.status='SUCCEEDED' THEN PERFORM pg_sleep(1); END IF; RETURN NEW; END $$;
              CREATE CONSTRAINT TRIGGER slow_commit AFTER UPDATE ON control.customer_ingestion_attempt
              DEFERRABLE INITIALLY DEFERRED FOR EACH ROW EXECUTE FUNCTION control.slow_commit()""")
    job=runtime.enqueue(db,event())
    attempt=runtime.reserve(db,request)
    ctx=multiprocessing.get_context('spawn')
    ready=ctx.Event()
    process=ctx.Process(target=child_run,args=(attempt['attempt_id'],kill_stage,ready))
    process.start()
    try:
        assert ready.wait(15), 'worker never reached requested commit boundary'
        assert scalar(db,'SELECT count(*) FROM control.customer_ingestion_attempt')==1
        if kill_stage=='during_commit':
            deadline=time.monotonic()+5
            while time.monotonic()<deadline:
                if scalar(db,"SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND query='COMMIT' AND wait_event='PgSleep'"):
                    break
                time.sleep(.01)
            else:
                pytest.fail('worker never entered deferred COMMIT')
        os.kill(process.pid,signal.SIGKILL)
        process.join(10)
        assert process.exitcode==-signal.SIGKILL
    finally:
        if process.is_alive():
            process.kill(); process.join(5)
    runtime.stopped(db,{'taskArn':'task/'+attempt['attempt_id'],'lastStatus':'STOPPED'})
    if kill_stage=='before_commit':
        assert scalar(db,'SELECT count(*) FROM control.payload')==0
        assert scalar(db,'SELECT count(*) FROM control.ingestion_batch')==0
        assert scalar(db,'SELECT status FROM control.customer_ingestion_job')=='QUEUED'
        with db,db.cursor() as q:
            q.execute('UPDATE control.customer_ingestion_job SET available_at=now()')
        retry=runtime.reserve(db,request)
        assert worker.run_attempt(retry['attempt_id'],connect=lambda:psycopg2.connect(DSN),s3=S3(),task_arn='task/retry')['status']=='completed'
    assert scalar(db,'SELECT count(*) FROM control.payload')==2
    assert scalar(db,'SELECT count(*) FROM control.ingestion_batch WHERE status=\'completed\'')==1
    assert scalar(db,'SELECT count(*) FROM control.stitching_job WHERE status=\'QUEUED\'')==1
    assert runtime.enqueue(db,event())['job_id']==job['job_id']
    assert runtime.reserve(db,request) is None
    assert scalar(db,'SELECT status FROM control.customer_ingestion_job')=='SUCCEEDED'


def test_landing_worker_completes_only_published_transaction(db,monkeypatch):
    from types import SimpleNamespace
    worker=load_worker()
    assert hasattr(worker,'run_landing'), 'landing worker route missing'
    activate(db)
    with db,db.cursor() as q:
        q.execute('CREATE TABLE control.source_landing_batch(batch_id uuid,source_code text,batch_ref text,output_status text)')
    record=event('input.csv')
    record['s3']['object']['key']='data_sources/crm/batch/customers/input.csv'
    runtime.enqueue(db,record,stage='landing')
    attempt=runtime.reserve(db,request)
    owner='11111111-1111-1111-1111-111111111111'
    runtime.claim(db,attempt['attempt_id'],owner,'task/landing')
    batch='aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa'
    source=SimpleNamespace(source_code='crm',batch_ref='batch')
    def process(source,downloaded,connection,s3,config,*,before_commit):
        with connection,connection.cursor() as q:
            q.execute("INSERT INTO control.source_landing_batch VALUES (%s,'crm','batch','PENDING')",(batch,))
            before_commit(q)
        assert scalar(connection,'SELECT status FROM control.customer_ingestion_job')=='ACTIVE'
        with connection,connection.cursor() as q:
            q.execute("UPDATE control.source_landing_batch SET output_status='PUBLISHED'")
            before_commit(q)
        return {'output':{'status':'published'}}
    from contextlib import contextmanager
    lifecycle=[]
    @contextmanager
    def download(*args):
        lifecycle.append('entered')
        try:
            yield SimpleNamespace(content=io.BytesIO())
        finally:
            lifecycle.append('closed')
    landing=SimpleNamespace(_load_config=lambda:None,_source_from_record=lambda r,c:source,
        _download_object=download,_process_source=process)
    monkeypatch.setattr(worker,'load_landing',lambda:landing)
    worker.run_landing(record,db,None,SimpleNamespace(check=lambda:None),attempt['attempt_id'],owner)
    assert scalar(db,'SELECT status FROM control.customer_ingestion_job')=='SUCCEEDED'
    assert lifecycle==['entered','closed']
    assert scalar(db,'SELECT landing_batch_id::text FROM control.customer_ingestion_job')==batch
    assert scalar(db,'SELECT ingestion_batch_id FROM control.customer_ingestion_job') is None


def test_real_landing_adapter_roundtrip_and_duplicate(db,monkeypatch):
    import ast
    import test_landing_bulk as landing_tests
    # Reuse the child's exact portable DDL contract in OUR isolated database;
    # never run its destructive fixture against their concurrently-used database.
    tree=ast.parse((ROOT/'tests/integration/test_landing_bulk.py').read_text())
    fixture=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='db')
    ddl=next(n.args[0].value for n in ast.walk(fixture) if isinstance(n,ast.Call)
             and isinstance(n.func,ast.Attribute) and n.func.attr=='execute'
             and n.args and isinstance(n.args[0],ast.Constant)
             and 'CREATE TABLE control.source_landing_batch' in str(n.args[0].value))
    with db,db.cursor() as q:
        q.execute('DROP SCHEMA IF EXISTS raw CASCADE; CREATE SCHEMA raw')
        q.execute(ddl)
    activate(db)
    worker=load_worker()
    landing=landing_tests.landing
    monkeypatch.setattr(worker,'load_landing',lambda:landing)
    monkeypatch.setenv('DB_SECRET_ID','unused')
    monkeypatch.setenv('CUSTOMER_LANDING_ENABLED','true')
    record=event()
    record['s3']['object']['key']='data_sources/crm/batch/customers/source.csv'
    content=b'source_customer_id,first_name\n001,old\n001,last\n,blank\n'
    class S3(landing_tests.MemoryS3):
        def get_object(self,**kwargs):
            return {'Body':io.BytesIO(content),'ContentLength':len(content),'ETag':'etag','VersionId':'v1'}
    s3=S3()
    job=runtime.enqueue(db,record,stage='landing')
    attempt=runtime.reserve(db,request)
    result=worker.run_attempt(attempt['attempt_id'],connect=lambda:psycopg2.connect(DSN),s3=s3,task_arn='task/landing-real')
    assert result['output']['status']=='published'
    assert scalar(db,'SELECT status FROM control.customer_ingestion_job')=='SUCCEEDED'
    assert scalar(db,'SELECT count(*) FROM raw.customer_landing_source')==3
    assert scalar(db,'SELECT output_row_count FROM control.source_landing_batch')==1
    assert runtime.enqueue(db,record,stage='landing')['job_id']==job['job_id']
    assert runtime.reserve(db,request) is None
    assert s3.puts==1


def test_sigterm_exits_warm_worker_instead_of_claiming_again(db,monkeypatch):
    worker=load_worker()
    activate(db)
    runtime.enqueue(db,event())
    attempt=runtime.reserve(db,request)
    def terminated(*args):
        raise SystemExit('SIGTERM')
    monkeypatch.setattr(worker,'prepare',terminated)
    with pytest.raises(SystemExit):
        worker.run_attempt(attempt['attempt_id'],connect=lambda:psycopg2.connect(DSN),s3=S3(),task_arn='task/stop')
    assert scalar(db,'SELECT status FROM control.customer_ingestion_job')=='QUEUED'


def test_failed_landing_commit_never_bypasses_later_fences(db,monkeypatch):
    from types import SimpleNamespace
    from contextlib import nullcontext
    worker=load_worker()
    activate(db)
    with db,db.cursor() as q:
        q.execute("CREATE TABLE control.source_landing_batch(batch_id uuid,source_code text,batch_ref text,output_status text)")
        q.execute("INSERT INTO control.source_landing_batch VALUES ('aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa','crm','batch','BUILDING')")
    record=event()
    record['s3']['object']['key']='data_sources/crm/batch/customers/source.csv'
    runtime.enqueue(db,record,stage='landing')
    attempt=runtime.reserve(db,request)
    owner='11111111-1111-1111-1111-111111111111'
    runtime.claim(db,attempt['attempt_id'],owner,'task/revoke')
    def process(source,downloaded,connection,s3,config,*,before_commit):
        with pytest.raises(RuntimeError,match='commit failed'):
            with connection,connection.cursor() as q:
                q.execute("UPDATE control.source_landing_batch SET output_status='PUBLISHED'")
                before_commit(q)
                raise RuntimeError('commit failed')
        runtime.stopped(connection,{'taskArn':'task/revoke','lastStatus':'STOPPED'})
        with pytest.raises(runtime.LeaseLost):
            with connection,connection.cursor() as q:
                q.execute("UPDATE control.source_landing_batch SET output_status='FAILED'")
                before_commit(q)
        raise RuntimeError('retry')
    landing=SimpleNamespace(_load_config=lambda:None,
        _source_from_record=lambda *a:SimpleNamespace(source_code='crm',batch_ref='batch'),
        _download_object=lambda *a:nullcontext(SimpleNamespace()),_process_source=process)
    monkeypatch.setattr(worker,'load_landing',lambda:landing)
    with pytest.raises(RuntimeError,match='retry'):
        worker.run_landing(record,db,None,SimpleNamespace(check=lambda:None),attempt['attempt_id'],owner)
    assert scalar(db,'SELECT output_status FROM control.source_landing_batch')=='BUILDING'

