"""Real PostgreSQL history contract. Only disposable local database allowed."""
import os
from pathlib import Path
import uuid

import psycopg2
import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def db():
    conn = psycopg2.connect(os.environ['CDP_HISTORY_TEST_DSN'])
    assert conn.info.host == '127.0.0.1' and conn.info.dbname == 'cdp_history_regression'
    with conn, conn.cursor() as q:
        q.execute('DROP SCHEMA IF EXISTS raw CASCADE; DROP SCHEMA IF EXISTS control CASCADE; DROP SCHEMA IF EXISTS staging CASCADE; CREATE SCHEMA raw; CREATE SCHEMA control; CREATE SCHEMA staging;')
        q.execute('CREATE TABLE control.ingestion_batch(batch_id uuid PRIMARY KEY, created_at timestamptz NOT NULL DEFAULT clock_timestamp())')
        q.execute('''CREATE TABLE raw.customer(raw_customer_id uuid PRIMARY KEY, batch_id uuid NOT NULL REFERENCES control.ingestion_batch, source_system_id bigint NOT NULL, source_customer_id text NOT NULL, date_of_birth date, status text, attributes jsonb, created_at timestamptz DEFAULT now(), updated_at timestamptz DEFAULT now(), ingested_at timestamptz DEFAULT now(), UNIQUE(source_system_id,source_customer_id));
        CREATE TABLE staging.customer_profile(standardized_record_id uuid DEFAULT gen_random_uuid() PRIMARY KEY,raw_customer_id uuid UNIQUE REFERENCES raw.customer, batch_id uuid, normalization_version text);
        CREATE TABLE raw.customer_batch_member(ingestion_batch_id uuid,raw_customer_id uuid REFERENCES raw.customer);''')
        q.execute((ROOT/'database/migrations/019_customer_typed_history.sql').read_text())
    yield conn
    conn.close()


@pytest.mark.parametrize('late_first_arrival', [False, True])
@pytest.mark.parametrize('same_timestamp', [False, True])
def test_current_never_rewinds_but_captures_old_batch(db, late_first_arrival, same_timestamp):
    raw = str(uuid.uuid4())
    first, second = '00000000-0000-0000-0000-000000000001', '00000000-0000-0000-0000-000000000002'
    with db, db.cursor() as q:
        q.execute("INSERT INTO control.ingestion_batch VALUES (%s,'2025-01-01'),(%s,%s)", (first, second, '2025-01-01' if same_timestamp else '2025-01-02'))
        batches = [(second, 'NEW')] if late_first_arrival else [(first, 'OLD'), (second, 'NEW')]
        for batch, status in batches + [(first, 'OLD')]:
            q.execute("INSERT INTO raw.customer_current(raw_customer_id,batch_id,source_system_id,source_customer_id,status) VALUES (%s,%s,1,'key',%s) ON CONFLICT(source_system_id,source_customer_id) DO UPDATE SET batch_id=EXCLUDED.batch_id,status=EXCLUDED.status", (raw,batch,status))
        q.execute('SELECT batch_id::text,status FROM raw.customer_current')
        assert q.fetchall() == [(second,'NEW')]
        q.execute('SELECT batch_id::text,status FROM raw.customer ORDER BY batch_id')
        assert q.fetchall() == [(first,'OLD'),(second,'NEW')]


def test_history_rejects_wrong_source_identity(db):
    raw, batch = str(uuid.uuid4()), str(uuid.uuid4())
    with db, db.cursor() as q:
        q.execute('INSERT INTO control.ingestion_batch(batch_id) VALUES (%s)', (batch,))
        q.execute("INSERT INTO raw.customer_current(raw_customer_id,batch_id,source_system_id,source_customer_id) VALUES (%s,%s,1,'key')", (raw,batch))
    with pytest.raises(psycopg2.IntegrityError):
        with db, db.cursor() as q:
            other = str(uuid.uuid4())
            q.execute('INSERT INTO control.ingestion_batch(batch_id) VALUES (%s)', (other,))
            q.execute("INSERT INTO raw.customer(raw_customer_id,batch_id,source_system_id,source_customer_id) VALUES (%s,%s,2,'wrong')", (raw,other))
    with pytest.raises(psycopg2.Error):
        with db, db.cursor() as q:
            q.execute('UPDATE raw.customer_current SET source_system_id=2 WHERE raw_customer_id=%s', (raw,))


def test_history_preserves_typed_uploads_and_identity_foreign_keys(db):
    raw, first, second = [str(uuid.uuid4()) for _ in range(3)]
    with db, db.cursor() as q:
        q.execute('INSERT INTO control.ingestion_batch VALUES (%s),(%s)',(first,second))
        q.execute("INSERT INTO raw.customer_current(raw_customer_id,batch_id,source_system_id,source_customer_id,date_of_birth,status,attributes) VALUES (%s,%s,1,'key','2001-02-03','ACTIVE','{\"v\":1}')",(raw,first))
        q.execute('INSERT INTO staging.customer_profile(raw_customer_id,batch_id) VALUES (%s,%s)',(raw,first))
        q.execute("UPDATE raw.customer_current SET batch_id=%s,status='SUSPENDED',attributes='{\"v\":2}' WHERE raw_customer_id=%s",(second,raw))
        q.execute('SELECT batch_id::text,status,attributes,pg_typeof(date_of_birth)::text FROM raw.customer ORDER BY attributes::text')
        assert q.fetchall()==[(first,'ACTIVE',{'v':1},'date'),(second,'SUSPENDED',{'v':2},'date')]
        q.execute('SELECT count(*) FROM staging.customer_profile JOIN raw.customer_current USING(raw_customer_id)')
        assert q.fetchone()[0]==1
        q.execute('UPDATE raw.customer_current SET updated_at=now() WHERE raw_customer_id=%s',(raw,))
        q.execute('SELECT count(*) FROM raw.customer'); assert q.fetchone()[0]==2
    for sql in ['UPDATE raw.customer SET status=status','DELETE FROM raw.customer','TRUNCATE raw.customer CASCADE']:
        with pytest.raises(psycopg2.Error,match='immutable'):
            with db, db.cursor() as q: q.execute(sql)
    with pytest.raises(psycopg2.Error,match='cannot change'):
        with db, db.cursor() as q:
            q.execute("UPDATE raw.customer_current SET status='ACTIVE' WHERE raw_customer_id=%s",(raw,))
    with db, db.cursor() as q:
        q.execute('SELECT status FROM raw.customer_current'); assert q.fetchone()[0]=='SUSPENDED'
