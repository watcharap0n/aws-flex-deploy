"""Prior review blocker regressions; disposable PostgreSQL only."""
import sys
import uuid
from pathlib import Path
import pytest
import psycopg2
from psycopg2.extras import Json
sys.path.insert(0, str(Path(__file__).parent))
from test_history_recovery import db, seed, ROOT


def install(db):
    with db, db.cursor() as q:
        q.execute((ROOT/'database/migrations/021_customer_history_recovery_outcomes.sql').read_text())


def insert(db, batch, evidence, reason='MISSING_HISTORICAL_MEMBERSHIP', status='FAILED', attempt=None):
    with db, db.cursor() as q:
        q.execute('INSERT INTO control.customer_history_recovery_outcome(attempt_id,ingestion_batch_id,status,reason_code,evidence) VALUES (%s,%s,%s,%s,%s)',
                  (attempt or str(uuid.uuid4()),batch,status,reason,Json(evidence)))


def complete(db, batch):
    with db, db.cursor() as q:
        q.execute('INSERT INTO raw.customer(raw_customer_id,batch_id,source_system_id,source_customer_id) SELECT raw_customer_id,ingestion_batch_id,source_system_id,source_customer_id FROM raw.customer_batch_member WHERE ingestion_batch_id=%s',(batch,))


def test_false_membership_evidence_rejected(db):
    batch,_=seed(db);install(db)
    with pytest.raises(psycopg2.Error,match='STALE_RECOVERY_EVIDENCE'):
        insert(db,batch,{'membership_rows':0})


def test_future_native_completion_hides_old_failure(db):
    batch,_=seed(db);install(db)
    # Works against the pinned revision; repaired revision uses its full evidence contract.
    with db,db.cursor() as q:
        q.execute("SELECT to_regprocedure('control.customer_history_outcome_evidence(uuid)')")
        helper=q.fetchone()[0]
        if helper:
            q.execute('SELECT control.customer_history_outcome_evidence(%s)',(batch,));e=q.fetchone()[0]
        else:e={}
    insert(db,batch,e,reason='CSV_RECOVERY_RETRY_REQUIRED',status='DEFERRED')
    complete(db,batch)
    with db,db.cursor() as q:
        q.execute('SELECT count(*) FROM control.customer_history_recovery_status WHERE ingestion_batch_id=%s',(batch,))
        assert q.fetchone()[0]==0
        q.execute('SELECT count(*) FROM control.customer_history_recovery_outcome WHERE ingestion_batch_id=%s',(batch,))
        assert q.fetchone()[0]==1


def test_actual_nonowner_reader_schema_usage(db):
    role='history_review_reader_'+uuid.uuid4().hex
    with db,db.cursor() as q:
        q.execute('CREATE ROLE '+role)
        q.execute('GRANT USAGE ON SCHEMA raw TO '+role)
        q.execute('GRANT SELECT ON raw.customer TO '+role)
    try:
        install(db)
        with db,db.cursor() as q:
            q.execute('SET LOCAL ROLE '+role)
            q.execute('SELECT * FROM control.customer_history_recovery_status LIMIT 1')
            q.execute("SELECT has_table_privilege(current_user,'control.customer_history_recovery_outcome','INSERT,UPDATE,DELETE,TRUNCATE')")
            assert q.fetchone()[0] is False
    finally:
        with db,db.cursor() as q:
            q.execute('DROP OWNED BY '+role)
            q.execute('DROP ROLE '+role)
