"""Real PostgreSQL recovery tests, using only a disposable local database."""
import hashlib
import os
from pathlib import Path
import sys
import uuid

import psycopg2
from psycopg2.extras import Json
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))
import recover_customer_history as recovery

@pytest.fixture
def db():
    dsn = os.environ.get('CDP_HISTORY_TEST_DSN')
    if not dsn:
        pytest.skip('Set CDP_HISTORY_TEST_DSN to a disposable local regression database')
    params = psycopg2.extensions.parse_dsn(dsn)
    assert params.get('host') in ('127.0.0.1', 'localhost')
    assert params.get('dbname') == 'cdp_history_regression'
    conn = psycopg2.connect(dsn)
    with conn, conn.cursor() as q:
        q.execute((ROOT/'tests/integration/fixtures/customer_history_base.sql').read_text())
        q.execute((ROOT/'database/migrations/019_customer_typed_history.sql').read_text())
        q.execute((ROOT/'database/migrations/020_customer_history_recovery.sql').read_text())
    yield conn
    conn.close()


def seed(db):
    batch, landing, newer = [str(uuid.uuid4()) for _ in range(3)]
    body = b'source_customer_id,email,date_of_birth,attributes\nk1,old@example.test,2001-02-03,"{""v"":1}"\nk2,two@example.test,,{}\n'
    digest = hashlib.sha256(body).hexdigest()
    with db, db.cursor() as q:
        q.execute("INSERT INTO control.source_landing_batch VALUES (%s,'PUBLISHED','test-bucket','raw/test/file.csv',%s,2)", (landing,digest))
        q.execute("INSERT INTO control.ingestion_batch(batch_id,landing_batch_id,source_system_id,status,records_loaded,checksum,object_name,metadata) VALUES (%s,%s,1,'completed',2,%s,'raw/test/file.csv',%s)", (batch,landing,digest,Json({'customer_history_artifact': {'version_id':'version-1','format':'canonical-csv-v1'}})))
        q.execute('INSERT INTO control.ingestion_batch(batch_id) VALUES (%s)',(newer,))
        for key in ('k1','k2'):
            q.execute("INSERT INTO raw.customer_current(batch_id,source_system_id,source_customer_id,email) VALUES (%s,1,%s,'latest@example.test') RETURNING raw_customer_id", (newer,key))
            rid=q.fetchone()[0]
            q.execute('INSERT INTO raw.customer_batch_member VALUES (%s,%s,1,%s)', (batch,rid,key))
    return batch,body


def test_recovers_exact_history_and_idempotent_ledger(db):
    batch,body=seed(db)
    first=recovery.recover_batch(db,batch,body,'version-1',apply=True)
    second=recovery.recover_batch(db,batch,body,'version-1',apply=True)
    assert first['inserted_rows']==2
    assert second['inserted_rows']==0
    with db.cursor() as q:
        q.execute('SELECT count(*) FROM control.customer_history_recovery')
        assert q.fetchone()[0]==1
        q.execute('SELECT DISTINCT email FROM raw.customer_current')
        assert q.fetchall()==[('latest@example.test',)]
        q.execute('SELECT email,snapshot_origin FROM raw.customer WHERE batch_id=%s ORDER BY source_customer_id',(batch,))
        assert q.fetchall()==[('old@example.test','CANONICAL_RECONSTRUCTION'),('two@example.test','CANONICAL_RECONSTRUCTION')]


@pytest.mark.parametrize('field,value', [('email', 'different@example.test'),('date_of_birth','1999-01-01'),('attributes',Json({'v':2})),('raw_payload',Json({})),('status',None)])
def test_equal_count_typed_conflicts_rollback(db,field,value):
    batch,body=seed(db)
    from psycopg2 import sql
    # Seed contradictory preexisting evidence without ever changing immutable rows.
    with db, db.cursor() as q:
        for row in recovery.parse_artifact(body,hashlib.sha256(body).hexdigest(),2):
            key=row['source_customer_id']
            row[field]=value if field!='source_customer_id' else value+key
            q.execute(sql.SQL('INSERT INTO raw.customer(raw_customer_id,batch_id,source_system_id,{}) SELECT raw_customer_id,%s,source_system_id,{} FROM raw.customer_current WHERE source_customer_id=%s').format(sql.SQL(',').join(map(sql.Identifier,row)),sql.SQL(',').join(sql.Placeholder() for _ in row)),(batch,*[Json(v) if isinstance(v,dict) else v for v in row.values()],key))
    with pytest.raises(recovery.RecoveryError,match='TYPED_HISTORY_MISMATCH'):
        recovery.recover_batch(db,batch,body,'version-1',apply=True)
    with db,db.cursor() as q:
        q.execute('SELECT count(*) FROM control.customer_history_recovery')
        assert q.fetchone()[0]==0
        q.execute('SELECT count(*) FROM raw.customer WHERE batch_id=%s',(batch,))
        assert q.fetchone()[0]==2


@pytest.mark.parametrize('change,code',[
    ("UPDATE raw.customer_batch_member SET source_customer_id='other' WHERE source_customer_id='k1'",'MEMBERSHIP_MISMATCH'),
    ("DELETE FROM raw.customer_batch_member WHERE source_customer_id='k1'",'MEMBERSHIP_MISMATCH'),
    ("UPDATE raw.customer_batch_member SET source_system_id=2",'MEMBERSHIP_MISMATCH'),
    ("UPDATE control.ingestion_batch SET checksum='bad' WHERE status='completed'",'LINEAGE_MISMATCH'),
    ("UPDATE control.ingestion_batch SET metadata='{}' WHERE status='completed'",'UNSUPPORTED_ARTIFACT_FORMAT'),
    ("UPDATE control.ingestion_batch SET status='failed' WHERE status='completed'",'COMPLETED_LINEAGE_REQUIRED'),
    ("ALTER TABLE raw.customer ADD COLUMN new_typed_value text",'TYPED_SCHEMA_MISMATCH'),
])
def test_preflight_fails_closed(db,change,code):
    batch,body=seed(db)
    with db,db.cursor() as q: q.execute(change)
    with pytest.raises(recovery.RecoveryError,match=code):
        recovery.recover_batch(db,batch,body,'version-1',apply=True)
    with db,db.cursor() as q:
        q.execute('SELECT count(*) FROM raw.customer WHERE batch_id=%s',(batch,))
        assert q.fetchone()[0]==0
        q.execute('SELECT count(*) FROM control.customer_history_recovery')
        assert q.fetchone()[0]==0


def test_audit_rolls_back_and_ledger_failure_rolls_back_insertions(db):
    batch,body=seed(db)
    assert recovery.recover_batch(db,batch,body,'version-1')['status']=='AUDIT_PASS'
    with db,db.cursor() as q:
        q.execute('SELECT count(*) FROM raw.customer WHERE batch_id=%s',(batch,))
        assert q.fetchone()[0]==0
        q.execute("ALTER TABLE control.customer_history_recovery ADD CONSTRAINT injected_failure CHECK (expected_rows=0)")
    with pytest.raises(recovery.RecoveryError,match='RECOVERY_FAILED_CheckViolation'):
        recovery.recover_batch(db,batch,body,'version-1',apply=True)
    with db,db.cursor() as q:
        q.execute('SELECT count(*) FROM raw.customer WHERE batch_id=%s',(batch,))
        assert q.fetchone()[0]==0
        q.execute('ALTER TABLE control.customer_history_recovery DROP CONSTRAINT injected_failure')
    assert recovery.recover_batch(db,batch,body,'version-1',apply=True)['inserted_rows']==2


def test_changed_provenance_cannot_rewrite_ledger(db):
    batch,body=seed(db)
    recovery.recover_batch(db,batch,body,'version-1',apply=True)
    with db,db.cursor() as q:
        q.execute("UPDATE control.ingestion_batch SET metadata=jsonb_set(metadata,'{customer_history_artifact,version_id}','\"version-2\"') WHERE batch_id=%s",(batch,))
    with pytest.raises(recovery.RecoveryError,match='LEDGER_MISMATCH'):
        recovery.recover_batch(db,batch,body,'version-2',apply=True)


def test_identity_fk_rejects_contradictory_source_key(db):
    batch,body=seed(db)
    with pytest.raises(psycopg2.errors.ForeignKeyViolation):
        with db,db.cursor() as q:
            q.execute("INSERT INTO raw.customer(raw_customer_id,batch_id,source_system_id,source_customer_id) SELECT raw_customer_id,%s,source_system_id,'wrong-key' || source_customer_id FROM raw.customer_current",(batch,))
    with db,db.cursor() as q:
        q.execute('SELECT count(*) FROM raw.customer WHERE batch_id=%s',(batch,))
        assert q.fetchone()[0]==0


def test_checksum_bound_unversioned_reconstruction(db,tmp_path):
    batch,body=seed(db)
    with db,db.cursor() as q:
        q.execute("UPDATE control.ingestion_batch SET metadata='{}' WHERE batch_id=%s",(batch,))
    result=recovery.recover_batch(db,batch,body,None,apply=True,
        checksum_bound_unversioned=True,archive_dir=tmp_path)
    assert result['inserted_rows']==2
    assert (tmp_path/hashlib.sha256(body).hexdigest()).read_bytes()==body
    with db,db.cursor() as q:
        q.execute('SELECT provenance FROM control.customer_history_recovery WHERE ingestion_batch_id=%s',(batch,))
        provenance=q.fetchone()[0]
        assert provenance['version_id'] is None
        assert provenance['evidence_kind']=='CHECKSUM_BOUND_UNVERSIONED_RECONSTRUCTION'
        assert provenance['archive_sha256']==hashlib.sha256(body).hexdigest()
    assert recovery.recover_batch(db,batch,body,None,apply=True,
        checksum_bound_unversioned=True,archive_dir=tmp_path)['inserted_rows']==0


@pytest.mark.parametrize('case,code',[
    ('checksum','ARTIFACT_CHECKSUM'),('archive','ARCHIVE_REQUIRED'),
    ('corrupt_archive','ARCHIVE_CHECKSUM'),('version','UNVERSIONED_MODE_CONFLICT'),
    ('membership','MEMBERSHIP_MISMATCH'),('format','UNSUPPORTED_ARTIFACT_FORMAT')])
def test_unversioned_integrity_failures_are_atomic(db,tmp_path,case,code):
    batch,body=seed(db)
    with db,db.cursor() as q:
        if case!='version':
            q.execute("UPDATE control.ingestion_batch SET metadata='{}' WHERE batch_id=%s",(batch,))
        if case=='membership':
            q.execute("DELETE FROM raw.customer_batch_member WHERE ingestion_batch_id=%s AND source_customer_id='k1'",(batch,))
        if case=='format':
            q.execute("UPDATE control.source_landing_batch SET output_key='manifest.json'")
    if case=='corrupt_archive':
        (tmp_path/hashlib.sha256(body).hexdigest()).write_bytes(b'corrupt')
    with pytest.raises(recovery.RecoveryError,match=code):
        recovery.recover_batch(db,batch,body+b'x' if case=='checksum' else body,None,
            apply=True,checksum_bound_unversioned=True,
            archive_dir=None if case=='archive' else tmp_path)
    with db,db.cursor() as q:
        q.execute('SELECT count(*) FROM raw.customer WHERE batch_id=%s',(batch,))
        assert q.fetchone()[0]==0
        q.execute('SELECT count(*) FROM control.customer_history_recovery')
        assert q.fetchone()[0]==0


def test_wrong_version_rejected(db):
    batch,body=seed(db)
    with pytest.raises(recovery.RecoveryError,match='ARTIFACT_VERSION_REQUIRED'):
        recovery.recover_batch(db,batch,body,'other',apply=True)


def test_exact_partial_history_preserved_and_missing_rows_added(db):
    batch,body=seed(db)
    from psycopg2 import sql
    row=recovery.parse_artifact(body,hashlib.sha256(body).hexdigest(),2)[0]
    with db,db.cursor() as q:
        q.execute(sql.SQL('INSERT INTO raw.customer(raw_customer_id,batch_id,source_system_id,{}) SELECT raw_customer_id,%s,source_system_id,{} FROM raw.customer_current WHERE source_customer_id=%s RETURNING snapshot_id,created_at,snapshot_origin').format(sql.SQL(',').join(map(sql.Identifier,row)),sql.SQL(',').join(sql.Placeholder() for _ in row)),(batch,*[Json(v) if isinstance(v,dict) else v for v in row.values()],row['source_customer_id']))
        original=q.fetchone()
    assert recovery.recover_batch(db,batch,body,'version-1',apply=True)['inserted_rows']==1
    with db,db.cursor() as q:
        q.execute("SELECT snapshot_id,created_at,snapshot_origin FROM raw.customer WHERE batch_id=%s AND source_customer_id='k1'",(batch,))
        assert q.fetchone()==original


def test_ledger_is_immutable(db):
    batch,body=seed(db)
    recovery.recover_batch(db,batch,body,'version-1',apply=True)
    for command in ('UPDATE control.customer_history_recovery SET status=status','DELETE FROM control.customer_history_recovery','TRUNCATE control.customer_history_recovery'):
        with pytest.raises(psycopg2.Error,match='immutable'):
            with db,db.cursor() as q: q.execute(command)


def test_post_insert_typed_mismatch_rolls_back(db):
    batch,body=seed(db)
    with db,db.cursor() as q:
        q.execute("CREATE FUNCTION raw.inject_bad_value() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN NEW.email := 'changed@example.test'; RETURN NEW; END $$; CREATE TRIGGER inject_bad_value BEFORE INSERT ON raw.customer FOR EACH ROW EXECUTE FUNCTION raw.inject_bad_value()")
    with pytest.raises(recovery.RecoveryError,match='POST_INSERT_MISMATCH'):
        recovery.recover_batch(db,batch,body,'version-1',apply=True)
    with db,db.cursor() as q:
        q.execute('SELECT count(*) FROM raw.customer WHERE batch_id=%s',(batch,))
        assert q.fetchone()[0]==0
        q.execute('SELECT count(*) FROM control.customer_history_recovery')
        assert q.fetchone()[0]==0
