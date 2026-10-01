"""049 candidate: ordinary app-scoped source projections on disposable PostgreSQL."""
from pathlib import Path
from uuid import uuid4

import psycopg2
import pytest
import test_customer_global_upload_capture as fixture

ROOT = Path(__file__).resolve().parents[2]
MIGRATION = ROOT / 'database/migrations/049_customer_logical_source_projection.sql'
db = fixture.db


def stage(conn, batch, raw, user, first):
    fixture.query(conn, '''CREATE TEMP TABLE IF NOT EXISTS customer_logical_source_stage (
        batch_id uuid NOT NULL, raw_customer_id uuid NOT NULL, source_system_id bigint NOT NULL,
        source_customer_id varchar(255) NOT NULL, first_name text,
        PRIMARY KEY(batch_id,raw_customer_id)) ON COMMIT PRESERVE ROWS''')
    fixture.query(conn, 'TRUNCATE pg_temp.customer_logical_source_stage')
    fixture.query(conn, '''INSERT INTO pg_temp.customer_logical_source_stage
        VALUES (%s,%s,%s,%s,%s)''', (batch['batch'],raw,batch['source'],user,first))


def install(conn):
    fixture.query(conn, MIGRATION.read_text())


def test_reused_provenance_uuid_has_two_stable_logical_currents_and_exact_observations(db):
    a = fixture.batch(db, fixture.A, source=1)
    b = fixture.batch(db, fixture.B, source=1)
    raw = str(uuid4())
    install(db)
    stage(db, a, raw, 'shared', 'Alice')
    assert fixture.one(db, 'SELECT raw.apply_customer_logical_source_bulk(%s,%s)', (a['batch'],1)) == 1
    stage(db, b, raw, 'shared', 'Bob')
    assert fixture.one(db, 'SELECT raw.apply_customer_logical_source_bulk(%s,%s)', (b['batch'],1)) == 1
    rows = fixture.query(db, '''SELECT c.app_id,c.logical_customer_id::text,c.first_name,
        h.first_name,h.raw_customer_id::text,h.snapshot_id::text
        FROM raw.customer_logical_current c JOIN raw.customer_logical_observation h
        USING(logical_customer_id) ORDER BY c.app_id''')
    assert [(r[0],r[2],r[3],r[4]) for r in rows] == [
        (fixture.A,'Alice','Alice',raw),(fixture.B,'Bob','Bob',raw)]
    assert len({r[1] for r in rows}) == len({r[5] for r in rows}) == 2
    assert fixture.one(db, 'SELECT count(*) FROM raw.customer_current') == 0
    assert fixture.one(db, 'SELECT count(*) FROM raw.customer_batch_member') == 0


def test_replay_and_later_observation_keep_identity_and_prior_snapshot(db):
    a = fixture.batch(db, fixture.A, source=1)
    later = fixture.batch(db, fixture.A, source=1)
    fixture.query(db, "UPDATE control.ingestion_batch SET created_at=created_at+interval '1 second' WHERE batch_id=%s", (later['batch'],))
    raw = str(uuid4())
    install(db)
    stage(db, a, raw, 'user', 'Before')
    fixture.one(db, 'SELECT raw.apply_customer_logical_source_bulk(%s,%s)', (a['batch'],1))
    before = fixture.query(db, 'SELECT logical_customer_id,snapshot_id,first_name FROM raw.customer_logical_observation')
    assert fixture.one(db, 'SELECT raw.apply_customer_logical_source_bulk(%s,%s)', (a['batch'],1)) == 1
    stage(db, later, raw, 'user', 'After')
    fixture.one(db, 'SELECT raw.apply_customer_logical_source_bulk(%s,%s)', (later['batch'],1))
    assert fixture.query(db, 'SELECT logical_customer_id,snapshot_id,first_name FROM raw.customer_logical_observation WHERE batch_id=%s', (a['batch'],)) == before
    assert fixture.one(db, 'SELECT first_name FROM raw.customer_logical_current') == 'After'
    assert fixture.one(db, 'SELECT count(*) FROM raw.customer_logical_observation') == 2
    assert fixture.one(db, 'SELECT count(DISTINCT standardized_record_id) FROM staging.customer_logical_profile_history') == 1
    assert fixture.one(db, 'SELECT first_name_raw FROM staging.customer_logical_profile_current') == 'After'


def test_same_logical_source_new_observation_uuid_preserves_identity(db):
    first = fixture.batch(db, fixture.A, source=1)
    second = fixture.batch(db, fixture.A, source=1)
    fixture.query(db, "UPDATE control.ingestion_batch SET created_at=created_at+interval '1 second' WHERE batch_id=%s", (second['batch'],))
    install(db)
    old_raw, new_raw = str(uuid4()), str(uuid4())
    stage(db, first, old_raw, 'same-user', 'Earlier')
    fixture.one(db, 'SELECT raw.apply_customer_logical_source_bulk(%s,%s)', (first['batch'],1))
    before = fixture.query(db, '''SELECT logical_customer_id,standardized_record_id
        FROM staging.customer_logical_profile_current''')
    stage(db, second, new_raw, 'same-user', 'Later')
    fixture.one(db, 'SELECT raw.apply_customer_logical_source_bulk(%s,%s)', (second['batch'],1))
    assert fixture.query(db, '''SELECT logical_customer_id,standardized_record_id
        FROM staging.customer_logical_profile_current''') == before
    assert fixture.query(db, '''SELECT raw_customer_id::text,first_name
        FROM raw.customer_logical_observation ORDER BY first_name''') == [
            (old_raw,'Earlier'),(new_raw,'Later')]
    assert fixture.one(db, 'SELECT raw_customer_id::text FROM raw.customer_logical_current') == new_raw


def test_same_batch_raw_uuid_cannot_claim_two_logical_sources(db):
    batch = fixture.batch(db, fixture.A, source=1)
    install(db)
    raw = str(uuid4())
    stage(db, batch, raw, 'alice', 'Alice')
    fixture.one(db, 'SELECT raw.apply_customer_logical_source_bulk(%s,%s)', (batch['batch'],1))
    before = fixture.query(db, '''SELECT logical_customer_id,source_customer_id,raw_customer_id
        FROM raw.customer_logical_current''')
    stage(db, batch, raw, 'bob', 'Bob')
    db.commit()
    db.autocommit = False
    fixture.query(db, 'SAVEPOINT colliding_source')
    with pytest.raises(psycopg2.errors.UniqueViolation):
        fixture.one(db, 'SELECT raw.apply_customer_logical_source_bulk(%s,%s)', (batch['batch'],1))
    fixture.query(db, 'ROLLBACK TO SAVEPOINT colliding_source')
    assert fixture.query(db, '''SELECT logical_customer_id,source_customer_id,raw_customer_id
        FROM raw.customer_logical_current''') == before
    assert fixture.one(db, 'SELECT count(*) FROM raw.customer_logical_observation') == 1


def test_older_arrival_is_history_only_and_observations_are_immutable(db):
    old = fixture.batch(db, fixture.A, source=1)
    new = fixture.batch(db, fixture.A, source=1)
    fixture.query(db, "UPDATE control.ingestion_batch SET created_at=created_at+interval '1 second' WHERE batch_id=%s", (new['batch'],))
    install(db)
    raw = str(uuid4())
    stage(db, new, raw, 'user', 'Newest')
    fixture.one(db, 'SELECT raw.apply_customer_logical_source_bulk(%s,%s)', (new['batch'],1))
    stage(db, old, raw, 'user', 'Older')
    fixture.one(db, 'SELECT raw.apply_customer_logical_source_bulk(%s,%s)', (old['batch'],1))
    assert fixture.one(db, 'SELECT first_name FROM raw.customer_logical_current') == 'Newest'
    assert fixture.one(db, 'SELECT first_name_raw FROM staging.customer_logical_profile_current') == 'Newest'
    assert set(fixture.query(db, 'SELECT first_name FROM raw.customer_logical_observation')) == {('Older',),('Newest',)}
    db.commit()
    db.autocommit = False
    fixture.query(db, 'SAVEPOINT immutable')
    with pytest.raises(psycopg2.Error, match='immutable'):
        fixture.query(db, "UPDATE raw.customer_logical_observation SET first_name='tampered'")
    fixture.query(db, 'ROLLBACK TO SAVEPOINT immutable')
    fixture.query(db, 'SAVEPOINT typed_immutable')
    with pytest.raises(psycopg2.Error, match='immutable'):
        fixture.query(db, 'DELETE FROM staging.customer_logical_profile_history')
    fixture.query(db, 'ROLLBACK TO SAVEPOINT typed_immutable')


def test_wrong_batch_or_changed_replay_fails_without_mutating_evidence(db):
    a = fixture.batch(db, fixture.A, source=1)
    install(db)
    db.commit()
    db.autocommit = False
    raw = str(uuid4())
    stage(db, a, raw, 'user', 'Before')
    fixture.one(db, 'SELECT raw.apply_customer_logical_source_bulk(%s,%s)', (a['batch'],1))
    stage(db, a, raw, 'user', 'Changed')
    fixture.query(db, 'SAVEPOINT bad_replay')
    with pytest.raises(psycopg2.Error, match='logical observation replay differs'):
        fixture.one(db, 'SELECT raw.apply_customer_logical_source_bulk(%s,%s)', (a['batch'],1))
    fixture.query(db, 'ROLLBACK TO SAVEPOINT bad_replay')
    assert fixture.one(db, 'SELECT first_name FROM raw.customer_logical_observation') == 'Before'
    stage(db, a, raw, 'user', 'Before')
    fixture.query(db, 'UPDATE pg_temp.customer_logical_source_stage SET source_system_id=2')
    fixture.query(db, 'SAVEPOINT bad_source')
    with pytest.raises(psycopg2.Error, match='logical stage lineage mismatch'):
        fixture.one(db, 'SELECT raw.apply_customer_logical_source_bulk(%s,%s)', (a['batch'],1))
    fixture.query(db, 'ROLLBACK TO SAVEPOINT bad_source')


def test_direct_profile_with_wrong_raw_uuid_is_rejected(db):
    a = fixture.batch(db, fixture.A, source=1)
    install(db)
    stage(db, a, str(uuid4()), 'one', 'Alice')
    fixture.one(db, 'SELECT raw.apply_customer_logical_source_bulk(%s,%s)', (a['batch'],1))
    logical = fixture.one(db, 'SELECT logical_customer_id FROM raw.customer_logical_current')
    db.commit()
    db.autocommit = False
    fixture.query(db, 'SAVEPOINT wrong_profile')
    with pytest.raises(psycopg2.errors.ForeignKeyViolation):
        fixture.query(db, '''UPDATE staging.customer_logical_profile_current
            SET raw_customer_id=%s WHERE logical_customer_id=%s''', (str(uuid4()),logical))
        fixture.query(db, 'SET CONSTRAINTS ALL IMMEDIATE')
    fixture.query(db, 'ROLLBACK TO SAVEPOINT wrong_profile')


def test_direct_logical_current_wrong_batch_app_fails_closed(db):
    a = fixture.batch(db, fixture.A, source=1)
    b = fixture.batch(db, fixture.B, source=2)
    install(db)
    with pytest.raises(psycopg2.Error, match='logical current batch lineage mismatch'):
        fixture.query(db, '''INSERT INTO raw.customer_logical_current
            (app_id,source_system_id,source_customer_id,batch_id,raw_customer_id)
            VALUES (%s,1,'spoofed',%s,%s)''', (a['app'],b['batch'],str(uuid4())))
    db.rollback()
    assert fixture.one(db, 'SELECT count(*) FROM raw.customer_logical_current') == 0


def test_direct_mismatched_batch_observation_is_rejected(db):
    a = fixture.batch(db, fixture.A, source=1)
    b = fixture.batch(db, fixture.B, source=2)
    install(db)
    raw = str(uuid4())
    stage(db, a, raw, 'one', 'Alice')
    fixture.one(db, 'SELECT raw.apply_customer_logical_source_bulk(%s,%s)', (a['batch'],1))
    logical = fixture.one(db, 'SELECT logical_customer_id FROM raw.customer_logical_current')
    db.commit()
    db.autocommit = False
    fixture.query(db, 'SAVEPOINT wrong_lineage')
    with pytest.raises(psycopg2.Error, match='logical observation batch lineage mismatch'):
        fixture.query(db, '''INSERT INTO raw.customer_logical_observation
            (logical_customer_id,app_id,source_system_id,source_customer_id,batch_id,raw_customer_id)
            VALUES (%s,%s,1,'one',%s,%s)''', (logical,fixture.A,b['batch'],raw))
    fixture.query(db, 'ROLLBACK TO SAVEPOINT wrong_lineage')


def test_legacy_alias_and_published_export_remain_byte_for_byte(db):
    a = fixture.batch(db, fixture.A, source=1)
    old = fixture.member(db,a,'old',first='Legacy')
    fixture.query(db, 'INSERT INTO staging.customer_profile_history SELECT * FROM staging.customer_profile WHERE raw_customer_id=%s', (old['raw'],))
    fixture.query(db, 'UPDATE identity.customer_entity SET member_count=1 WHERE customer_entity_id=%s', (old['entity'],))
    fixture.install(db)
    transfer = fixture.finish(db,a)
    assert transfer['status'] == 'PENDING', transfer
    fixture.query(db, "UPDATE control.customer_identity_transfer SET status='PUBLISHED' WHERE transfer_id=%s", (a['run'],))
    fixture.query(db, 'INSERT INTO staging.customer_profile_history SELECT * FROM staging.customer_profile WHERE 1=0')
    before = fixture.query(db, 'SELECT batch_id,raw_customer_id,snapshot_id,first_name FROM raw.customer')
    membership = fixture.query(db, 'SELECT * FROM raw.customer_batch_member')
    current = fixture.query(db, 'SELECT * FROM raw.customer_current')
    export = fixture.query(db, 'SELECT * FROM control.customer_identity_export')
    evidence = fixture.query(db, 'SELECT * FROM control.customer_identity_transfer')
    install(db)
    assert fixture.query(db, 'SELECT batch_id,raw_customer_id,snapshot_id,first_name FROM raw.customer') == before
    assert fixture.query(db, 'SELECT * FROM raw.customer_batch_member') == membership
    assert fixture.query(db, 'SELECT * FROM raw.customer_current') == current
    assert fixture.query(db, 'SELECT * FROM control.customer_identity_export') == export
    assert fixture.query(db, 'SELECT * FROM control.customer_identity_transfer') == evidence
    assert len(export) == 1
