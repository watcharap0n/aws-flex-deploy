"""Operator recovery of pre-export BLOCKED uploads (disposable PostgreSQL only)."""
import importlib.util
import os
from pathlib import Path
import sys
import uuid

import psycopg2
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / '_lambda' / 'ingestion_data'))
import lambda_function as ingestion

spec = importlib.util.spec_from_file_location('blocked_recovery', ROOT / 'database' / 'recover_customer_blocked.py')
recovery = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = recovery
spec.loader.exec_module(recovery)


@pytest.fixture
def db():
    dsn = os.environ['CDP_TEST_DSN']
    conn = psycopg2.connect(dsn)
    assert conn.info.host == '127.0.0.1' and conn.info.port == 55439
    with conn, conn.cursor() as q:
        q.execute('''DROP SCHEMA IF EXISTS control CASCADE;
            DROP SCHEMA IF EXISTS quality CASCADE;
            DROP SCHEMA IF EXISTS raw CASCADE;
            DROP SCHEMA IF EXISTS staging CASCADE;
            CREATE SCHEMA control; CREATE SCHEMA quality; CREATE SCHEMA raw; CREATE SCHEMA staging;
            CREATE FUNCTION control.customer_publication_is_connector(uuid) RETURNS boolean
                LANGUAGE sql AS 'SELECT false';
            CREATE TABLE control.source_system(source_system_id int PRIMARY KEY,source_code text,source_type text);
            CREATE TABLE control.source_landing_batch(batch_id uuid PRIMARY KEY,output_row_count bigint);
            CREATE TABLE control.ingestion_batch(batch_id uuid PRIMARY KEY,landing_batch_id uuid,
                source_system_id int,app_id text,batch_ref text,status text,records_loaded bigint);
            CREATE TABLE control.stitching_job(job_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),ingestion_batch_id uuid,
                attempt int,status text,selected_record_count bigint,
                UNIQUE(ingestion_batch_id,attempt));
            CREATE TABLE control.customer_identity_transfer(transfer_id uuid PRIMARY KEY,
                stitching_job_id uuid,ingestion_batch_id uuid,status text,error_code text,
                row_count bigint,attempts int);
            CREATE TABLE raw.customer_batch_member(ingestion_batch_id uuid,raw_customer_id uuid,
                source_system_id int,source_customer_id text);
            CREATE TABLE raw.customer_landing_source(batch_id uuid,source_customer_id text);
            CREATE TABLE staging.customer_profile_history(batch_id uuid,raw_customer_id uuid,
                app_id text,source_system_id int,source_customer_id text);
            CREATE TABLE staging.customer_profile(raw_customer_id uuid,app_id text,
                source_system_id int,source_customer_id text);
            CREATE TABLE quality.customer_validation_run(validation_run_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
                landing_batch_id uuid,ingestion_batch_id uuid,stitching_job_id uuid,attempt int,
                run_status text,ruleset_version text,source_row_count bigint DEFAULT 0,
                canonical_row_count bigint DEFAULT 0,blank_key_row_count bigint DEFAULT 0,
                duplicate_group_count bigint DEFAULT 0,duplicate_affected_row_count bigint DEFAULT 0,
                duplicate_excess_row_count bigint DEFAULT 0,selected_record_count bigint DEFAULT 0,
                existing_source_record_count bigint DEFAULT 0,updated_at timestamptz DEFAULT now(),
                UNIQUE(ingestion_batch_id,attempt));
            CREATE TABLE quality.customer_validation_record(validation_run_id uuid,source_record_id uuid,
                raw_customer_id uuid,source_row_number int,selected_for_canonical bool,
                disposition text,existing_source_record bool,preexisting_entity_id uuid,
                identity_classification text,entity_candidate_count int DEFAULT 0,
                matched_identifier_types text[] DEFAULT '{}',record_fingerprint text,
                issue_codes text[] DEFAULT '{}');
            CREATE VIEW control.customer_pipeline_status AS
                SELECT b.batch_id, j.job_id, j.attempt,
                  CASE WHEN j.status='QUEUED' THEN 'STITCHING_QUEUED'
                    WHEN t.status='BLOCKED' THEN 'TRANSFER_BLOCKED' END AS overall_status
                FROM control.ingestion_batch b
                LEFT JOIN LATERAL (SELECT * FROM control.stitching_job x
                    WHERE x.ingestion_batch_id=b.batch_id ORDER BY attempt DESC LIMIT 1) j ON true
                LEFT JOIN control.customer_identity_transfer t ON t.stitching_job_id=j.job_id;''')
    yield conn
    conn.close()


def seed(db, *, code='CAPTURE_LOCK_CONTENDED', app='1282009980020330', source='manual_upload',
         history=True, status='SUCCEEDED'):
    batch, landing, job, transfer, validation = (str(uuid.uuid4()) for _ in range(5))
    batch_ref = '2026-09-27-104114-1282009980020330-7f9067da99f1'
    members = [(str(uuid.uuid4()), str(i)) for i in range(2)]
    with db, db.cursor() as q:
        q.execute('INSERT INTO control.source_system VALUES (892,%s,\'s3_csv\')', (source,))
        q.execute('INSERT INTO control.source_landing_batch VALUES (%s,2)', (landing,))
        q.execute('INSERT INTO control.ingestion_batch VALUES (%s,%s,892,%s,%s,\'completed\',2)',
                  (batch, landing, app, batch_ref))
        q.execute('INSERT INTO control.stitching_job VALUES (%s,%s,1,%s,2)', (job,batch,status))
        q.execute('INSERT INTO control.customer_identity_transfer VALUES (%s,%s,%s,\'BLOCKED\',%s,0,0)',
                  (transfer,job,batch,code))
        q.execute('''INSERT INTO quality.customer_validation_run(validation_run_id,landing_batch_id,
            ingestion_batch_id,stitching_job_id,attempt,run_status,source_row_count,selected_record_count)
            VALUES (%s,%s,%s,%s,1,'SUCCEEDED',2,2)''', (validation,landing,batch,job))
        for i,(raw_id,key) in enumerate(members,1):
            q.execute('INSERT INTO raw.customer_batch_member VALUES (%s,%s,892,%s)',(batch,raw_id,key))
            q.execute('INSERT INTO raw.customer_landing_source VALUES (%s,%s)',(landing,key))
            q.execute('INSERT INTO staging.customer_profile VALUES (%s,%s,892,%s)',(raw_id,app,key))
            if history:
                q.execute('INSERT INTO staging.customer_profile_history VALUES (%s,%s,%s,892,%s)',
                          (batch,raw_id,app,key))
            q.execute('''INSERT INTO quality.customer_validation_record
                (validation_run_id,source_record_id,raw_customer_id,source_row_number,
                 selected_for_canonical,disposition,existing_source_record,identity_classification)
                 VALUES (%s,%s,%s,%s,true,'ACCEPTED',false,'NEW_TO_CDP')''',
                 (validation,str(uuid.uuid4()),raw_id,i))
    return batch, job, transfer


def test_blocked_recovery_preserves_old_attempt_and_moves_effective_status(db):
    batch,job,transfer=seed(db)
    result=recovery.recover_one(db,batch,job,transfer,ingestion)
    assert result['status']=='QUEUED' and result['attempt']==2
    with db.cursor() as q:
        q.execute('SELECT status,error_code,row_count FROM control.customer_identity_transfer WHERE transfer_id=%s',(transfer,))
        assert q.fetchone()==('BLOCKED','CAPTURE_LOCK_CONTENDED',0)
        q.execute('SELECT status FROM control.stitching_job WHERE job_id=%s',(job,))
        assert q.fetchone()==('SUCCEEDED',)
        q.execute('SELECT overall_status FROM control.customer_pipeline_status WHERE batch_id=%s',(batch,))
        assert q.fetchone()==('STITCHING_QUEUED',)
        q.execute('''SELECT count(*),count(*) FILTER(WHERE selected_for_canonical)
                    FROM quality.customer_validation_record r JOIN quality.customer_validation_run v
                    USING(validation_run_id) WHERE v.stitching_job_id=%s''',(result['job_id'],))
        assert q.fetchone()==(2,2)
    repeated=recovery.recover_one(db,batch,job,transfer,ingestion)
    assert repeated['status']=='ALREADY_ATTEMPTED' and repeated['job_id']==result['job_id']


@pytest.mark.parametrize('kwargs',[{'source':'tfg_sync'},{'app':None},{'history':False},
                                  {'code':'INVALID_UPLOAD_BATCH_REF'},{'status':'FAILED'}])
def test_blocked_recovery_rejects_unproven_lineage(db,kwargs):
    batch,job,transfer=seed(db,**kwargs)
    with pytest.raises(recovery.RecoveryIneligible):
        recovery.recover_one(db,batch,job,transfer,ingestion)
    with db.cursor() as q:
        q.execute('SELECT count(*) FROM control.stitching_job WHERE ingestion_batch_id=%s',(batch,))
        assert q.fetchone()[0]==1


def test_blocked_recovery_rejects_connector_attached_to_manual_source(db):
    batch,job,transfer=seed(db)
    with db,db.cursor() as q:
        q.execute('''CREATE OR REPLACE FUNCTION control.customer_publication_is_connector(uuid)
            RETURNS boolean LANGUAGE sql AS 'SELECT true' ''')
    with pytest.raises(recovery.RecoveryIneligible,match='CONNECTOR_LINEAGE_EXCLUDED'):
        recovery.recover_one(db,batch,job,transfer,ingestion)


def test_blocked_recovery_accepts_duplicate_landing_observations(db):
    batch,job,transfer=seed(db)
    with db,db.cursor() as q:
        q.execute('SELECT landing_batch_id FROM control.ingestion_batch WHERE batch_id=%s',(batch,))
        landing=q.fetchone()[0]
        # Landing output is two canonical customers; arrival/validation has three
        # rows including one excluded duplicate. Keep those grains distinct.
        q.execute('INSERT INTO raw.customer_landing_source VALUES (%s,%s)',(landing,'0'))
        q.execute('SELECT validation_run_id FROM quality.customer_validation_run WHERE stitching_job_id=%s',(job,))
        validation=q.fetchone()[0]
        q.execute('UPDATE quality.customer_validation_run SET source_row_count=3 WHERE validation_run_id=%s',(validation,))
        q.execute('''INSERT INTO quality.customer_validation_record
            (validation_run_id,source_record_id,raw_customer_id,source_row_number,
             selected_for_canonical,disposition,existing_source_record,identity_classification)
             VALUES (%s,%s,NULL,3,false,'EXCLUDED',false,'NOT_EVALUATED')''',
             (validation,str(uuid.uuid4())))
    result=recovery.recover_one(db,batch,job,transfer,ingestion)
    assert result['status']=='QUEUED'
    with db.cursor() as q:
        q.execute('''SELECT count(*),count(*) FILTER(WHERE selected_for_canonical)
                    FROM quality.customer_validation_record r JOIN quality.customer_validation_run v
                    USING(validation_run_id) WHERE v.stitching_job_id=%s''',(result['job_id'],))
        assert q.fetchone()==(3,2)


def test_blocked_recovery_rolls_back_failed_validation_copy(db,monkeypatch):
    batch,job,transfer=seed(db)
    def fail(*args,**kwargs):
        raise RuntimeError('copy failed')
    monkeypatch.setattr(ingestion,'_copy_validation_records',fail)
    with pytest.raises(RuntimeError,match='copy failed'):
        recovery.recover_one(db,batch,job,transfer,ingestion)
    with db.cursor() as q:
        q.execute('SELECT count(*) FROM control.stitching_job WHERE ingestion_batch_id=%s',(batch,))
        assert q.fetchone()[0]==1
