"""Recovery outcome status must not falsify completed ingestion truth."""
from pathlib import Path
import sys
import uuid
import pytest
import psycopg2
sys.path.insert(0, str(Path(__file__).parent))
from test_history_recovery import db, seed, recovery

ROOT=Path(__file__).resolve().parents[2]


def test_stable_attempt_and_sanitized_evidence(db):
    batch,_=seed(db)
    attempt=str(uuid.uuid4())
    with db,db.cursor() as q:
        q.execute((ROOT/'database/migrations/021_customer_history_recovery_outcomes.sql').read_text())
        for _ in range(2):
            q.execute("INSERT INTO control.customer_history_recovery_outcome(attempt_id,ingestion_batch_id,status,reason_code,evidence) VALUES (%s,%s,'FAILED','MISSING_HISTORICAL_MEMBERSHIP','{\"membership_rows\":0}')",(attempt,batch))
        q.execute('SELECT count(*) FROM control.customer_history_recovery_outcome');assert q.fetchone()[0]==1
    for evidence in ['{\"email\":\"private@example.org\"}','{\"membership_rows\":-1}','{\"membership_rows\":\"payload\"}','{\"object_name\":\"arbitrary/customer/name.csv\"}']:
        with pytest.raises(psycopg2.Error):
            with db,db.cursor() as q:
                q.execute("INSERT INTO control.customer_history_recovery_outcome(attempt_id,ingestion_batch_id,status,reason_code,evidence) VALUES (%s,%s,'FAILED','MISSING_HISTORICAL_MEMBERSHIP',%s)",(str(uuid.uuid4()),batch,evidence))
    with pytest.raises(psycopg2.Error):
        with db,db.cursor() as q:
            q.execute("INSERT INTO control.customer_history_recovery_outcome(attempt_id,ingestion_batch_id,status,reason_code,evidence) VALUES (%s,%s,'DEFERRED','PARSER_REVIEW_REQUIRED','{}')",(attempt,batch))


def test_pipeline_additive_fields_keep_original_contract(db):
    batch,_=seed(db)
    with db,db.cursor() as q:
        q.execute("CREATE VIEW control.customer_pipeline_status AS SELECT batch_id AS ingestion_batch_id,status AS ingestion_status,'COMPLETED'::text AS overall_status FROM control.ingestion_batch")
        q.execute("SELECT oid FROM pg_class WHERE oid='control.customer_pipeline_status'::regclass");oid=q.fetchone()[0]
        q.execute((ROOT/'database/migrations/021_customer_history_recovery_outcomes.sql').read_text())
        q.execute("INSERT INTO control.customer_history_recovery_outcome(attempt_id,ingestion_batch_id,status,reason_code,evidence) VALUES (%s,%s,'FAILED','MISSING_HISTORICAL_MEMBERSHIP','{}')",(str(uuid.uuid4()),batch))
        q.execute('SELECT ingestion_status,overall_status,history_recovery_status,history_recovery_reason_code FROM control.customer_pipeline_status WHERE ingestion_batch_id=%s',(batch,))
        assert q.fetchone()==('completed','COMPLETED','FAILED','MISSING_HISTORICAL_MEMBERSHIP')
        q.execute("SELECT oid FROM pg_class WHERE oid='control.customer_pipeline_status'::regclass");assert q.fetchone()[0]==oid


def test_complete_native_history_cannot_receive_failure(db):
    batch,body=seed(db)
    with db,db.cursor() as q:
        q.execute((ROOT/'database/migrations/021_customer_history_recovery_outcomes.sql').read_text())
        q.execute("INSERT INTO raw.customer(raw_customer_id,batch_id,source_system_id,source_customer_id,email) SELECT m.raw_customer_id,m.ingestion_batch_id,m.source_system_id,m.source_customer_id,'original@example.org' FROM raw.customer_batch_member m WHERE m.ingestion_batch_id=%s",(batch,))
    with pytest.raises(psycopg2.Error,match='COMPLETE_HISTORY_CANNOT_BE_FAILED'):
        with db,db.cursor() as q:
            q.execute("INSERT INTO control.customer_history_recovery_outcome(attempt_id,ingestion_batch_id,status,reason_code,evidence) VALUES (%s,%s,'FAILED','TRANSPORT_TIMEOUT','{}')",(str(uuid.uuid4()),batch))
    with db,db.cursor() as q:
        q.execute('SELECT count(*) FROM raw.customer WHERE batch_id=%s',(batch,));assert q.fetchone()[0]==2
        q.execute('SELECT count(*) FROM control.customer_history_recovery_outcome');assert q.fetchone()[0]==0


def test_concurrent_duplicate_attempt_is_one_append(db):
    import concurrent.futures
    batch,_=seed(db);attempt=str(uuid.uuid4())
    with db,db.cursor() as q:q.execute((ROOT/'database/migrations/021_customer_history_recovery_outcomes.sql').read_text())
    def insert():
        c=psycopg2.connect(db.dsn)
        try:
            with c,c.cursor() as q:
                q.execute("SET LOCAL statement_timeout='5s'")
                q.execute("INSERT INTO control.customer_history_recovery_outcome(attempt_id,ingestion_batch_id,status,reason_code,evidence) VALUES (%s,%s,'FAILED','MISSING_HISTORICAL_MEMBERSHIP','{}')",(attempt,batch))
        finally:c.close()
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        list(ex.map(lambda _:insert(),range(2)))
    with db,db.cursor() as q:
        q.execute('SELECT count(*) FROM control.customer_history_recovery_outcome');assert q.fetchone()[0]==1


def test_failed_recovery_preserves_ingestion_and_allows_later_verified(db):
    batch,body=seed(db)
    migration=ROOT/'database/migrations/021_customer_history_recovery_outcomes.sql'
    assert migration.exists(), 'Additive recovery outcome contract is absent'
    with db,db.cursor() as q:
        q.execute(migration.read_text())
        q.execute("INSERT INTO control.customer_history_recovery_outcome(attempt_id,ingestion_batch_id,status,reason_code,evidence) VALUES (gen_random_uuid(),%s,'FAILED','MISSING_HISTORICAL_MEMBERSHIP','{\"membership_rows\":0}') RETURNING outcome_id",(batch,))
        outcome=q.fetchone()[0]
        q.execute('SELECT status FROM control.ingestion_batch WHERE batch_id=%s',(batch,));assert q.fetchone()[0]=='completed'
        q.execute('SELECT status,reason_code FROM control.customer_history_recovery_status WHERE ingestion_batch_id=%s',(batch,));assert q.fetchone()==('FAILED','MISSING_HISTORICAL_MEMBERSHIP')
    recovery.recover_batch(db,batch,body,'version-1',apply=True)
    with db,db.cursor() as q:
        q.execute('SELECT status,reason_code FROM control.customer_history_recovery_status WHERE ingestion_batch_id=%s',(batch,));assert q.fetchone()==('VERIFIED',None)
        q.execute('SELECT status FROM control.customer_history_recovery_outcome WHERE outcome_id=%s',(outcome,));assert q.fetchone()[0]=='FAILED'
    for sql in ["UPDATE control.customer_history_recovery_outcome SET reason_code='ALTERED'",'DELETE FROM control.customer_history_recovery_outcome','TRUNCATE control.customer_history_recovery_outcome']:
        with pytest.raises(psycopg2.Error):
            with db,db.cursor() as q:q.execute(sql)
    with pytest.raises(psycopg2.Error):
        with db,db.cursor() as q:
            q.execute("INSERT INTO control.customer_history_recovery_outcome(attempt_id,ingestion_batch_id,status,reason_code,evidence) VALUES (gen_random_uuid(),%s,'FAILED','AFTER_VERIFIED','{}')",(batch,))


def test_latest_outcome_and_invalid_reason_contract(db):
    batch,_=seed(db)
    with db,db.cursor() as q:
        q.execute((ROOT/'database/migrations/021_customer_history_recovery_outcomes.sql').read_text())
        for status,reason in [('DEFERRED','PARSER_REVIEW_REQUIRED'),('FAILED','MISSING_HISTORICAL_MEMBERSHIP')]:
            q.execute('INSERT INTO control.customer_history_recovery_outcome(attempt_id,ingestion_batch_id,status,reason_code,evidence) VALUES (gen_random_uuid(),%s,%s,%s,%s)',(batch,status,reason,'{}'))
        q.execute('SELECT status,reason_code FROM control.customer_history_recovery_status WHERE ingestion_batch_id=%s',(batch,));assert q.fetchone()==('FAILED','MISSING_HISTORICAL_MEMBERSHIP')
    for status,reason,evidence in [('FAILED','','{}'),('FAILED','contains payload','{}'),('VERIFIED','NO_LEDGER','{}'),('FAILED','FIXED_CODE','[]')]:
        with pytest.raises(psycopg2.Error):
            with db,db.cursor() as q:
                q.execute('INSERT INTO control.customer_history_recovery_outcome(attempt_id,ingestion_batch_id,status,reason_code,evidence) VALUES (gen_random_uuid(),%s,%s,%s,%s)',(batch,status,reason,evidence))
