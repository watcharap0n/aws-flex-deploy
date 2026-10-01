"""Bounded full SQL path: originals -> landing -> durable canonical -> bulk.

Runs on the runtime fixture's isolated database; S3 transport alone is faked.
"""
import ast
import hashlib
import io
import re

import psycopg2

from test_customer_ingestion_runtime import ROOT, DSN, db, activate, request, scalar
from test_customer_ingestion_worker import load_worker
import customer_ingestion_runtime as runtime
import test_landing_bulk as lt


def setup(db):
    with db,db.cursor() as q:
        q.execute((ROOT/'tests/integration/fixtures/customer_bulk_live_base.sql').read_text())
        q.execute((ROOT/'database/migrations/019_customer_typed_history.sql').read_text())
        q.execute((ROOT/'database/migrations/022_customer_bulk_ingestion.sql').read_text())
        q.execute('DROP TABLE control.source_landing_batch CASCADE')
        tree=ast.parse((ROOT/'tests/integration/test_landing_bulk.py').read_text())
        fixture=next(n for n in tree.body if isinstance(n,ast.FunctionDef) and n.name=='db')
        ddl=next(n.args[0].value for n in ast.walk(fixture) if isinstance(n,ast.Call)
            and isinstance(n.func,ast.Attribute) and n.func.attr=='execute' and n.args
            and isinstance(n.args[0],ast.Constant) and 'CREATE TABLE control.source_landing_batch' in str(n.args[0].value))
        q.execute(ddl)
        q.execute('''CREATE EXTENSION IF NOT EXISTS pgcrypto;
          ALTER TABLE control.source_system ADD source_type text, ADD is_active boolean;
          CREATE SEQUENCE control.runtime_source_seq START 10;
          ALTER TABLE control.source_system ALTER source_system_id SET DEFAULT nextval('control.runtime_source_seq');
          ALTER TABLE control.ingestion_batch ADD status text, ADD object_name text, ADD file_name text,
            ADD checksum text, ADD metadata jsonb, ADD batch_ref text, ADD landing_batch_id uuid,
            ADD completed_at timestamptz, ADD updated_at timestamptz DEFAULT now(),
            ADD records_received bigint, ADD records_loaded bigint, ADD records_rejected bigint,
            ADD UNIQUE(source_system_id,object_name,checksum);
          CREATE TABLE control.stitching_job(job_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            ingestion_batch_id uuid REFERENCES control.ingestion_batch,attempt int,status text);
          DROP SCHEMA IF EXISTS identity CASCADE; CREATE SCHEMA identity;
          CREATE TABLE identity.customer_entity(customer_entity_id uuid PRIMARY KEY);
          CREATE TABLE identity.customer_entity_member(standardized_record_id uuid,customer_entity_id uuid);
          CREATE SCHEMA quality;''')
        quality=(ROOT/'database/migrations/015_customer_data_quality.sql').read_text()
        for table in ('customer_validation_run','customer_validation_record'):
            ddl=re.search(r'CREATE TABLE IF NOT EXISTS quality\.'+table+r' \(.*?\n\);',quality,re.S).group()
            # 019 already cut the customer table over; use its current-ID FK.
            q.execute(ddl.replace('REFERENCES raw.customer(raw_customer_id)','REFERENCES raw.customer_current(raw_customer_id)'))
    activate(db)


class Objects(lt.MemoryS3):
    def __init__(self):
        super().__init__()
        self.inputs={}
    def get_object(self,**kw):
        key=kw['Key']
        if key in self.inputs:
            content,etag,metadata=self.inputs[key]
        else:
            content,metadata=self.objects[(kw['Bucket'],key)]
            etag='canonical'
        assert kw['IfMatch'].strip('"')==etag
        return {'Body':io.BytesIO(content),'ContentLength':len(content),'ETag':etag,'VersionId':kw.get('VersionId','v1'),'Metadata':metadata}


def test_original_to_two_canonical_batches_exact_replay_stable_current(db,monkeypatch):
    setup(db)
    worker=load_worker()
    monkeypatch.setattr(worker,'load_landing',lambda:lt.landing)
    monkeypatch.setenv('DB_SECRET_ID','unused')
    monkeypatch.setenv('CUSTOMER_LANDING_ENABLED','true')
    monkeypatch.setenv('CUSTOMER_BULK_ENABLED','true')
    monkeypatch.setenv('LANDING_MAX_PARSE_BYTES',str(256*1024**2))
    snapshot=worker.ingestion._create_existing_source_snapshot
    def locked_snapshot(q,**kw):
        with psycopg2.connect(DSN) as other,other.cursor() as probe:
            probe.execute("SELECT pg_try_advisory_xact_lock(hashtextextended('customer-bulk:' || %s::text,0))",(kw['source_system_id'],))
            assert probe.fetchone()==(False,), 'source lock must precede existing snapshot'
        return snapshot(q,**kw)
    monkeypatch.setattr(worker.ingestion,'_create_existing_source_snapshot',locked_snapshot)
    s3=Objects()
    events=[]
    batches=[]
    for batch_ref,rows in [('first','001,old\n001,last\n,blank\n002,other\n'),('second','001,new\n003,third\n')]:
        key=f'data_sources/crm/{batch_ref}/customers/source.csv'
        content=('source_customer_id,first_name\n'+rows).encode()
        s3.inputs[key]=(content,'original',{})
        original={'s3':{'bucket':{'name':'fixture-bucket'},'object':{'key':key,'eTag':'original','versionId':'v1'}}}
        landing_job=runtime.enqueue(db,original,stage='landing')
        attempt=runtime.reserve(db,request)
        output=worker.run_attempt(attempt['attempt_id'],connect=lambda:psycopg2.connect(DSN),s3=s3,task_arn='task/'+attempt['attempt_id'])['output']
        canonical={'s3':{'bucket':{'name':output['output_bucket']},'object':{'key':output['output_key'],'eTag':'canonical','versionId':'v1'}}}
        events.append(canonical)
        runtime.enqueue(db,canonical)
        attempt=runtime.reserve(db,request)
        result=worker.run_attempt(attempt['attempt_id'],connect=lambda:psycopg2.connect(DSN),s3=s3,task_arn='task/'+attempt['attempt_id'])
        batches.append(result['batch_id'])
        assert result['status']=='completed' and result['records_loaded']==2
    with db,db.cursor() as q:
        q.execute("SELECT first_name FROM raw.customer_current WHERE source_customer_id='001'")
        assert q.fetchone()==('new',)
        q.execute('SELECT batch_id::text,first_name FROM raw.customer WHERE source_customer_id=%s',('001',))
        assert dict(q.fetchall())=={batches[0]:'last',batches[1]:'new'}
        q.execute('SELECT raw_customer_id,source_customer_id FROM raw.customer_current ORDER BY source_customer_id')
        identities=q.fetchall()
        q.execute('SELECT source_row_count,selected_record_count,existing_source_record_count FROM quality.customer_validation_run ORDER BY created_at')
        assert q.fetchall()==[(4,2,0),(2,2,1)]
        q.execute('SELECT count(*) FROM raw.customer_batch_member')
        assert q.fetchone()==(4,)
        q.execute("SELECT count(*) FROM control.stitching_job WHERE status='QUEUED'")
        assert q.fetchone()==(2,)
    # Same bytes delivered with a new transport identity: a new job must resolve
    # the existing completed batch, not rewrite old history/current or launch.
    replay=events[0]
    replay['s3']['object']['versionId']='v2'
    runtime.enqueue(db,replay)
    attempt=runtime.reserve(db,request)
    result=worker.run_attempt(attempt['attempt_id'],connect=lambda:psycopg2.connect(DSN),s3=s3,task_arn='task/replay')
    assert result['status']=='skipped' and result['batch_id']==batches[0]
    with db,db.cursor() as q:
        q.execute('SELECT raw_customer_id,source_customer_id FROM raw.customer_current ORDER BY source_customer_id')
        assert q.fetchall()==identities
        q.execute("SELECT first_name FROM raw.customer_current WHERE source_customer_id='001'")
        assert q.fetchone()==('new',)
        q.execute("SELECT count(*) FROM control.customer_ingestion_job WHERE status='SUCCEEDED'")
        assert q.fetchone()==(5,)


def test_real_ingress_ack_readback_and_duplicate_relay(db,monkeypatch):
    from types import SimpleNamespace
    import json
    setup(db)
    worker=load_worker()
    monkeypatch.setenv('CUSTOMER_INGESTION_ENABLED','true')
    monkeypatch.setenv('CUSTOMER_LANDING_ENABLED','true')
    monkeypatch.setenv('CUSTOMER_INGESTION_FUNCTION_ARN','local-test-ingestion')
    monkeypatch.setenv('DB_SECRET_ID','unused')
    monkeypatch.setattr(worker.ingestion,'_connect',lambda:psycopg2.connect(DSN))
    record={'s3':{'bucket':{'name':'fixture-bucket'},'object':{'key':'data_sources/crm/relay/customers/source.csv','eTag':'original','versionId':'v1'}}}
    def invoke(**kw):
        assert kw['InvocationType']=='RequestResponse'
        response=worker.ingestion.lambda_handler(json.loads(kw['Payload']),None)
        # A separate database read proves the relay cannot ACK an uncommitted job.
        assert scalar(db,'SELECT count(*) FROM control.customer_ingestion_job')==1
        return {'StatusCode':200,'Payload':io.BytesIO(json.dumps(response).encode())}
    monkeypatch.setattr(lt.landing.boto3,'client',lambda name:SimpleNamespace(invoke=invoke))
    first=lt.landing.lambda_handler({'Records':[record]},None)
    second=lt.landing.lambda_handler({'Records':[record]},None)
    assert first['results']==second['results']
    assert first['status']=='queued' and first['processed_records']==0
    assert scalar(db,'SELECT stage FROM control.customer_ingestion_job')=='landing'
    assert scalar(db,'SELECT count(*) FROM control.source_landing_file')==0
