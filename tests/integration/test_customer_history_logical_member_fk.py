"""Disposable-PG contract for immutable observation/current decoupling.

Candidate SQL is a prerequisite experiment, not an activation of 038.
"""
from pathlib import Path
from uuid import uuid4

import psycopg2
import pytest

import test_customer_global_upload_capture as fixture

# Candidate only: never place partial schema cutover in deployable migrations.
MIGRATION = Path(__file__).resolve().parent / 'fixtures/customer_history_logical_member_fk_candidate.sql'
db = fixture.db


def _batches(conn):
    a = fixture.batch(conn, fixture.A, source=1)
    b = fixture.batch(conn, fixture.B, source=2)
    fixture.query(conn, "UPDATE control.ingestion_batch SET created_at=created_at+interval '1 second' WHERE batch_id=%s", (b['batch'],))
    return a, b


def test_observation_tuple_can_differ_from_current_only_with_exact_immutable_member(db):
    a, b = _batches(db)
    raw = str(uuid4())
    fixture.query(db, '''INSERT INTO raw.customer_current
        (raw_customer_id,batch_id,source_system_id,source_customer_id)
        VALUES (%s,%s,1,'a-user')''', (raw,a['batch']))
    db.commit()
    before = fixture.query(db, '''SELECT batch_id,raw_customer_id,source_system_id,
        source_customer_id,snapshot_id FROM raw.customer ORDER BY batch_id''')
    fixture.query(db, MIGRATION.read_text())
    db.commit()
    # An observed UUID can be reused as provenance for a different logical
    # source even while the mutable current record still belongs to app A.
    fixture.query(db, '''INSERT INTO raw.customer
        (raw_customer_id,batch_id,source_system_id,source_customer_id)
        VALUES (%s,%s,2,'b-user')''', (raw,b['batch']))
    fixture.query(db, '''INSERT INTO raw.customer_batch_member
        (ingestion_batch_id,raw_customer_id,source_system_id,source_customer_id)
        VALUES (%s,%s,2,'b-user')''', (b['batch'],raw))
    db.commit()
    assert fixture.query(db, '''SELECT batch_id,raw_customer_id,source_system_id,
        source_customer_id,snapshot_id FROM raw.customer WHERE batch_id=%s''', (a['batch'],)) == before
    assert fixture.one(db, 'SELECT count(*) FROM raw.customer WHERE raw_customer_id=%s', (raw,)) == 2
    assert fixture.one(db, '''SELECT source_customer_id FROM raw.customer_current
        WHERE raw_customer_id=%s''', (raw,)) == 'a-user'


def test_observation_without_exact_batch_member_fails_at_commit(db):
    a, b = _batches(db)
    raw = str(uuid4())
    fixture.query(db, '''INSERT INTO raw.customer_current
        (raw_customer_id,batch_id,source_system_id,source_customer_id)
        VALUES (%s,%s,1,'a-user')''', (raw,a['batch']))
    db.commit()
    fixture.query(db, MIGRATION.read_text())
    db.commit()
    fixture.query(db, '''INSERT INTO raw.customer
        (raw_customer_id,batch_id,source_system_id,source_customer_id)
        VALUES (%s,%s,2,'b-user')''', (raw,b['batch']))
    with pytest.raises(psycopg2.errors.ForeignKeyViolation):
        db.commit()
    db.rollback()
    assert fixture.one(db, 'SELECT count(*) FROM raw.customer WHERE raw_customer_id=%s', (raw,)) == 1


def test_wrong_member_tuple_does_not_satisfy_observation_fk(db):
    a, b = _batches(db)
    raw = str(uuid4())
    fixture.query(db, '''INSERT INTO raw.customer_current
        (raw_customer_id,batch_id,source_system_id,source_customer_id)
        VALUES (%s,%s,1,'a-user')''', (raw,a['batch']))
    db.commit()
    fixture.query(db, MIGRATION.read_text())
    db.commit()
    fixture.query(db, '''INSERT INTO raw.customer
        (raw_customer_id,batch_id,source_system_id,source_customer_id)
        VALUES (%s,%s,2,'b-user')''', (raw,b['batch']))
    fixture.query(db, '''INSERT INTO raw.customer_batch_member
        (ingestion_batch_id,raw_customer_id,source_system_id,source_customer_id)
        VALUES (%s,%s,2,'different-user')''', (b['batch'],raw))
    with pytest.raises(psycopg2.errors.ForeignKeyViolation):
        db.commit()
    db.rollback()


def test_candidate_rejects_preexisting_history_without_exact_member(db):
    a, b = _batches(db)
    raw = str(uuid4())
    fixture.query(db, '''INSERT INTO raw.customer_current
        (raw_customer_id,batch_id,source_system_id,source_customer_id)
        VALUES (%s,%s,1,'a-user')''', (raw,a['batch']))
    db.commit()
    # Before cutover, the old FK permits a second historical observation of
    # that same current source without the 014 membership row. Candidate must
    # reject this drift; do not synthesize ownership to make installation pass.
    fixture.query(db, '''INSERT INTO raw.customer
        (raw_customer_id,batch_id,source_system_id,source_customer_id)
        VALUES (%s,%s,1,'a-user')''', (raw,b['batch']))
    db.commit()
    before = fixture.one(db, 'SELECT count(*) FROM raw.customer WHERE raw_customer_id=%s', (raw,))
    with pytest.raises(psycopg2.errors.RaiseException,
                       match='Missing exact immutable batch/source membership'):
        fixture.query(db, MIGRATION.read_text())
    db.rollback()
    assert fixture.one(db, 'SELECT count(*) FROM raw.customer WHERE raw_customer_id=%s', (raw,)) == before
    assert fixture.one(db, '''SELECT count(*) FROM pg_constraint
        WHERE conname='customer_history_current_fkey' ''') == 1


def test_existing_022_statement_writer_still_commits_new_history(db):
    a, _ = _batches(db)
    db.commit()
    fixture.query(db, MIGRATION.read_text())
    raw = str(uuid4())
    fixture.query(db, '''INSERT INTO raw.customer_current
        (raw_customer_id,batch_id,source_system_id,source_customer_id)
        VALUES (%s,%s,1,'fresh')''', (raw,a['batch']))
    db.commit()
    assert fixture.one(db, '''SELECT count(*) FROM raw.customer h
        JOIN raw.customer_batch_member m ON (m.ingestion_batch_id,m.raw_customer_id,
            m.source_system_id,m.source_customer_id)=(h.batch_id,h.raw_customer_id,
            h.source_system_id,h.source_customer_id)
        WHERE h.raw_customer_id=%s''', (raw,)) == 1
