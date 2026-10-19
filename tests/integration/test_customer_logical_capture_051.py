"""051 connector-aware capture and ordinary alias regressions in disposable PG.

The fixture uses only owned loopback port 55441; the staging tunnel is read-only.
"""
from pathlib import Path
import psycopg2
import pytest
import test_customer_forward_source_namespace as forward
import test_customer_concurrent_capture as overlap
import test_customer_global_upload_capture as fixture
import test_connector_identity_publication as connector
import test_customer_logical_member_cutover_050 as cutover

db = fixture.db

MIGRATION = Path(__file__).resolve().parents[2] / 'database/migrations/051_customer_logical_capture.sql'

def install(db):
    fixture.query(db, MIGRATION.read_text())
    db.commit()

def alias(db):
    connector.install(db)
    first, second, old = cutover.legacy_alias(db)
    cutover.install(db)
    return first, second, old

def finish_pinned(db, batch):
    overlap.pin_matcher_snapshot(db)
    return fixture.finish(db, batch)

def test_active_app_tuple_capture_accepts_old_cross_app_raw_alias_without_rewriting_history(db):
    first, second, old = alias(db)
    before = fixture.query(db, '''SELECT batch_id,raw_customer_id,app_id,source_system_id,source_customer_id
        FROM staging.customer_profile_history ORDER BY batch_id''')
    install(db)
    fixture.query(db, 'UPDATE identity.customer_entity_member SET resolution_run_id=%s WHERE standardized_record_id=%s',
                  (second['run'], old['standard']))
    fixture.query(db, 'UPDATE identity.customer_entity SET last_resolution_run_id=%s,member_count=1 WHERE customer_entity_id=%s',
                  (second['run'], old['entity']))
    transfer = finish_pinned(db, second)
    assert (transfer['status'], transfer['row_count'], transfer['member_count']) == ('PENDING',1,1), transfer
    assert fixture.one(db, 'SELECT app_id FROM control.customer_identity_export WHERE transfer_id=%s', (second['run'],)) == fixture.B
    assert fixture.query(db, '''SELECT batch_id,raw_customer_id,app_id,source_system_id,source_customer_id
        FROM staging.customer_profile_history ORDER BY batch_id''') == before
    assert fixture.one(db, 'SELECT count(*) FROM control.customer_identity_export WHERE app_id=%s', (fixture.A,)) == 0


def test_cross_app_global_entity_exports_both_exact_private_members_without_profile_leak(db):
    a = fixture.batch(db, fixture.A, source=1)
    own = fixture.member(db, a, 'shared', first='Alice', email='a@example.test')
    b = fixture.batch(db, fixture.B, source=2)
    other = fixture.member(db, b, 'shared', entity=own['entity'], first='Bob', email='b@example.test')
    fixture.query(db, 'INSERT INTO staging.customer_profile_history SELECT * FROM staging.customer_profile')
    fixture.query(db, 'UPDATE identity.customer_entity_member SET resolution_run_id=%s', (a['run'],))
    fixture.query(db, 'UPDATE identity.customer_entity SET last_resolution_run_id=%s,member_count=2', (a['run'],))
    connector.install(db)
    cutover.install(db)
    install(db)
    transfer = finish_pinned(db, a)
    assert (transfer['status'],transfer['row_count'],transfer['member_count']) == ('PENDING',2,2), transfer
    rows = fixture.query(db, '''SELECT app_id,source_customer_id,raw_customer_id,customer_entity_id
        FROM control.customer_identity_member_export WHERE transfer_id=%s ORDER BY app_id''', (a['run'],))
    assert rows == [(fixture.A,'shared',own['raw'],own['entity']), (fixture.B,'shared',other['raw'],own['entity'])]
    exports = fixture.query(db, '''SELECT app_id,payload->'profile'->>'first_name',payload->'profile'->>'email'
        FROM control.customer_identity_export WHERE transfer_id=%s ORDER BY app_id''', (a['run'],))
    assert exports == [(fixture.A,'Alice','a@example.test'),(fixture.B,'Bob','b@example.test')]


def test_051_same_app_older_selected_batch_uses_latest_exact_active_profile(db):
    first = fixture.batch(db, fixture.A)
    own = fixture.member(db, first, 'shared', first='Before')
    fixture.query(db, 'INSERT INTO staging.customer_profile_history SELECT * FROM staging.customer_profile')
    later = fixture.batch(db, fixture.A)
    fixture.query(db, "UPDATE control.ingestion_batch SET created_at=created_at+interval '1 second' WHERE batch_id=%s", (later['batch'],))
    fixture.member(db, later, 'shared', raw=own['raw'], entity=own['entity'], first='After')
    fixture.query(db, 'INSERT INTO staging.customer_profile_history SELECT * FROM staging.customer_profile')
    fixture.query(db, 'UPDATE identity.customer_entity SET member_count=1,last_resolution_run_id=%s WHERE customer_entity_id=%s',
                  (first['run'], own['entity']))
    fixture.query(db, 'UPDATE identity.customer_entity_member SET resolution_run_id=%s WHERE standardized_record_id=%s',
                  (first['run'],own['standard']))
    connector.install(db)
    cutover.install(db)
    install(db)
    transfer = finish_pinned(db, first)
    assert (transfer['status'],transfer['row_count']) == ('PENDING',1), transfer
    assert fixture.one(db, '''SELECT payload->'profile'->>'first_name' FROM control.customer_identity_export
        WHERE transfer_id=%s''', (first['run'],)) == 'After'
    assert fixture.one(db, 'SELECT first_name_raw FROM staging.customer_profile_history WHERE batch_id=%s',
                       (first['batch'],)) == 'Before'


@pytest.mark.parametrize('field,value', [
    ('batch_id', '00000000-0000-0000-0000-000000000001'),
    ('app_id', fixture.B),
    ('source_system_id', 999),
    ('source_customer_id', 'impostor'),
])
def test_selected_history_requires_exact_batch_app_source_user(db, field, value):
    b = fixture.batch(db, fixture.A)
    fixture.member(db, b, 'selected')
    fixture.query(db, 'INSERT INTO staging.customer_profile_history SELECT * FROM staging.customer_profile')
    connector.install(db)
    cutover.install(db)
    install(db)
    fixture.query(db, 'ALTER TABLE staging.customer_profile_history DISABLE TRIGGER ALL')
    fixture.query(db, f'UPDATE staging.customer_profile_history SET {field}=%s', (value,))
    fixture.query(db, 'ALTER TABLE staging.customer_profile_history ENABLE TRIGGER ALL')
    transfer = finish_pinned(db, b)
    assert (transfer['status'],transfer['row_count']) == ('BLOCKED',0), transfer
    assert fixture.one(db, 'SELECT count(*) FROM control.customer_identity_member_export WHERE transfer_id=%s', (b['run'],)) == 0
    assert fixture.one(db, 'SELECT count(*) FROM control.customer_identity_export WHERE transfer_id=%s', (b['run'],)) == 0


def test_selected_history_wrong_source_blocks_without_export(db):
    b = fixture.batch(db, fixture.A)
    fixture.member(db, b, 'member', first='Safe')
    fixture.query(db, 'INSERT INTO staging.customer_profile_history SELECT * FROM staging.customer_profile')
    connector.install(db)
    cutover.install(db)
    install(db)
    # A later forged immutable observation with a different source tuple must
    # never inherit the app's active member by raw UUID alone.
    fixture.query(db, 'ALTER TABLE raw.customer_batch_member DISABLE TRIGGER ALL')
    fixture.query(db, "UPDATE raw.customer_batch_member SET source_customer_id='forged' WHERE ingestion_batch_id=%s", (b['batch'],))
    fixture.query(db, 'ALTER TABLE raw.customer_batch_member ENABLE TRIGGER ALL')
    transfer = finish_pinned(db, b)
    assert (transfer['status'], transfer['row_count']) == ('BLOCKED',0), transfer
    assert fixture.one(db, 'SELECT count(*) FROM control.customer_identity_export WHERE transfer_id=%s', (b['run'],)) == 0


def test_051_does_not_replay_blocked_or_change_old_published_export(db):
    connector.install(db)
    published = fixture.batch(db, fixture.A)
    own = fixture.member(db, published, 'published', first='Before')
    fixture.query(db, 'INSERT INTO staging.customer_profile_history SELECT * FROM staging.customer_profile')
    finish_pinned(db, published)
    fixture.query(db, "UPDATE control.customer_identity_transfer SET status='PUBLISHED',published_at=clock_timestamp() WHERE transfer_id=%s", (published['run'],))
    old = fixture.query(db, 'SELECT transfer_id,row_number,payload::text FROM control.customer_identity_export')
    db.commit()
    db.set_session(isolation_level='READ COMMITTED')
    blocked = fixture.batch(db, fixture.B, source=2)
    fixture.member(db, blocked, 'blocked', first='Blocked')
    fixture.query(db, 'INSERT INTO staging.customer_profile_history SELECT * FROM staging.customer_profile WHERE batch_id=%s', (blocked['batch'],))
    transfer = fixture.finish(db, blocked)
    assert transfer['status'] == 'BLOCKED'
    cutover.install(db)
    install(db)
    assert fixture.query(db, 'SELECT transfer_id,row_number,payload::text FROM control.customer_identity_export') == old
    assert fixture.one(db, 'SELECT status FROM control.customer_identity_transfer WHERE transfer_id=%s', (published['run'],)) == 'PUBLISHED'
    assert fixture.one(db, 'SELECT status FROM control.customer_identity_transfer WHERE stitching_job_id=%s', (blocked['job'],)) == 'BLOCKED'
    assert fixture.one(db, 'SELECT standardized_record_id FROM staging.customer_profile WHERE app_id=%s', (fixture.A,)) == own['standard']


def test_051_keeps_capture_oid_acl_and_rejects_second_install(db):
    connector.install(db)
    cutover.install(db)
    before = fixture.query(db, '''SELECT oid,proowner,proacl,prosecdef,proconfig,provolatile,
        proparallel,prokind,pronargs,encode(digest(prosrc,'sha256'),'hex')
        FROM pg_proc WHERE oid='control.capture_customer_identity_snapshot(uuid)'::regprocedure''')
    assert before is not None and before[0][-1] == '678feae1d3d8dba0637b9e24b1fc018bbaf0e3b2a3fcb4c16dbf4bccc05ca4bd'
    install(db)
    after = fixture.query(db, '''SELECT oid,proowner,proacl,prosecdef,proconfig,provolatile,
        proparallel,prokind,pronargs,encode(digest(prosrc,'sha256'),'hex')
        FROM pg_proc WHERE oid='control.capture_customer_identity_snapshot(uuid)'::regprocedure''')
    assert after is not None and after[0][:-1] == before[0][:-1]
    assert after[0][-1] != before[0][-1]
    installed = fixture.one(db, "SELECT prosrc FROM pg_proc WHERE oid='control.capture_customer_identity_snapshot(uuid)'::regprocedure")
    with pytest.raises(psycopg2.Error, match='051.*drift'):
        fixture.query(db, MIGRATION.read_text())
    db.rollback()
    assert fixture.one(db, "SELECT prosrc FROM pg_proc WHERE oid='control.capture_customer_identity_snapshot(uuid)'::regprocedure") == installed


def test_051_rejects_capture_catalog_drift_atomically(db):
    connector.install(db)
    cutover.install(db)
    fixture.query(db, '''CREATE OR REPLACE FUNCTION control.capture_customer_identity_snapshot(job_id uuid)
        RETURNS void LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $body$
        BEGIN RAISE EXCEPTION 'changed'; END $body$''')
    modified = fixture.one(db, "SELECT prosrc FROM pg_proc WHERE oid='control.capture_customer_identity_snapshot(uuid)'::regprocedure")
    db.commit()
    with pytest.raises(psycopg2.Error, match='051.*drift'):
        fixture.query(db, MIGRATION.read_text())
    db.rollback()
    assert fixture.one(db, "SELECT prosrc FROM pg_proc WHERE oid='control.capture_customer_identity_snapshot(uuid)'::regprocedure") == modified


def test_connector_verified_capture_after_051_keeps_workspace_and_provenance(db):
    connector.install(db)
    b = connector.connector_batch(db)
    m = fixture.member(db, b, 'connector-user')
    fixture.query(db, 'INSERT INTO staging.customer_profile_history SELECT * FROM staging.customer_profile WHERE raw_customer_id=%s', (m['raw'],))
    cutover.install(db)
    install(db)
    t = finish_pinned(db, b)
    assert (t['status'], t['app_id'], str(t['workspace_id']), t['row_count']) == ('PENDING', '00042', b['workspace'], 1), t
    assert fixture.one(db, 'SELECT provenance_status FROM control.customer_identity_member_export WHERE transfer_id=%s', (b['run'],)) == 'VERIFIED_CONNECTOR'
    assert fixture.one(db, "SELECT payload->>'workspace_id' FROM control.customer_identity_export WHERE transfer_id=%s", (b['run'],)) == b['workspace']


def test_connector_unbound_cannot_be_attributed_as_upload_after_051(db):
    connector.install(db)
    b = connector.connector_batch(db, app=None)
    cutover.install(db)
    install(db)
    fixture.query(db, 'SAVEPOINT unbound')
    with pytest.raises(psycopg2.Error, match='connector customer app_id unbound'):
        fixture.query(db, '''INSERT INTO raw.customer_current(raw_customer_id,batch_id,source_system_id,source_customer_id)
            VALUES (gen_random_uuid(),%s,%s,'unbound')''', (b['batch'], b['source']))
    fixture.query(db, 'ROLLBACK TO SAVEPOINT unbound')
    assert fixture.one(db, 'SELECT count(*) FROM control.customer_identity_export') == 0




def test_ordinary_038_job_with_historical_cross_app_raw_alias_keeps_gate_until_052(db):
    # 038+050 is exercised only after the independent 050 author delivers a
    # syntactically valid candidate; do not mask a migration syntax failure.
    connector.install(db)
    raw, old_batch, other_batch = forward.old_alias(db)
    assert old_batch != other_batch
    assert fixture.one(db, '''SELECT count(DISTINCT b.app_id) FROM raw.customer h
        JOIN control.ingestion_batch b ON b.batch_id=h.batch_id
        WHERE h.raw_customer_id=%s''', (raw,)) == 2
    forward.install(db)
    cutover.install(db)
    install(db)
    fresh_batch = forward.upload(db, forward.A)
    forward.stage_and_merge(db, fresh_batch, 'new-user', 'New')
    db.commit()
    fixture.query(db, 'SAVEPOINT closed_gate')
    with pytest.raises(psycopg2.Error, match='038 GLOBAL_ALIAS_STITCH_BLOCKED'):
        fixture.query(db, '''INSERT INTO control.stitching_job
            (job_id,ingestion_batch_id,status) VALUES (gen_random_uuid(),%s,'RUNNING')''',
            (fresh_batch,))
    fixture.query(db, 'ROLLBACK TO SAVEPOINT closed_gate')
    assert fixture.one(db, 'SELECT count(*) FROM control.stitching_job WHERE ingestion_batch_id=%s', (fresh_batch,)) == 0

