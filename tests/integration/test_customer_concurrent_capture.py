"""Real PostgreSQL overlap-capture regressions in a disposable local database.

The fixture creates and drops its own UTF8 database on loopback port 55441.
Never run these tests against staging or a customer database.
"""
from pathlib import Path
import time
import pytest
import psycopg2

import test_customer_global_upload_capture as fixture


db = fixture.db
ROOT = Path(__file__).resolve().parents[2]
M041 = ROOT / 'database/migrations/041_global_member_bridge.sql'
M042 = ROOT / 'database/migrations/042_customer_capture_lock_retry.sql'
M044 = ROOT / 'database/migrations/044_customer_overlap_capture.sql'
CANDIDATE_SHA = '342a9d9de34f06f81252bc1487f47a36a4fef96fb4ee5c7e9136d989bd9ca1bc'


def install(db):
    for path in (M041, M042, M044):
        fixture.query(db, path.read_text())
    assert fixture.one(db, "SELECT encode(digest(prosrc,'sha256'),'hex') FROM pg_proc "
                           "WHERE oid='control.capture_customer_identity_snapshot(uuid)'::regprocedure") == CANDIDATE_SHA

def pin_matcher_snapshot(db):
    db.commit()
    db.set_session(isolation_level='REPEATABLE READ')

@pytest.mark.xfail(strict=True, reason='022 legacy source-only writer must remain fenced until coordinated app-scoped activation')
def test_new_upload_raw_uuid_overlap_uses_logical_source_identity(db):
    """Ordinary bulk writer must split same-source key by app, not reuse raw UUID.

    A reused raw UUID in historical observations must remain provenance; for
    NEW uploads the writer must produce distinct current rows per logical key.
    """
    first = fixture.batch(db, fixture.A, source=1)
    second = fixture.batch(db, fixture.B, source=1)
    fixture.query(db, '''CREATE TEMP TABLE customer_bulk_stage
        (LIKE raw.customer_current INCLUDING DEFAULTS INCLUDING CONSTRAINTS) ON COMMIT DROP''')
    for b in (first, second):
        fixture.query(db, 'TRUNCATE pg_temp.customer_bulk_stage')
        fixture.query(db, '''INSERT INTO pg_temp.customer_bulk_stage
            (batch_id,source_system_id,source_customer_id,first_name,email)
            VALUES (%s,%s,'shared-user',%s,'shared@example.test')''',
            (b['batch'], b['source'], b['app']))
        assert fixture.one(db, 'SELECT raw.apply_customer_bulk(%s::uuid,%s::bigint)',
                           (b['batch'], b['source'])) == 1
    assert fixture.one(db, '''SELECT count(*) FROM raw.customer_current
        WHERE source_system_id=1 AND source_customer_id='shared-user' ''') == 2


def test_overlap_migration_is_idempotent_and_preserves_private_catalog(db):
    install(db)
    before = fixture.query(db, """SELECT p.oid, pg_get_userbyid(p.proowner), p.proacl::text,
                  p.prosecdef, p.proconfig, encode(digest(p.prosrc,'sha256'),'hex')
               FROM pg_proc p WHERE p.oid IN (
                 'control.capture_customer_identity_snapshot(uuid)'::regprocedure,
                 'control.capture_customer_identity_after_stitch()'::regprocedure)
               ORDER BY p.oid""")
    fixture.query(db, M044.read_text())
    after = fixture.query(db, """SELECT p.oid, pg_get_userbyid(p.proowner), p.proacl::text,
                  p.prosecdef, p.proconfig, encode(digest(p.prosrc,'sha256'),'hex')
               FROM pg_proc p WHERE p.oid IN (
                 'control.capture_customer_identity_snapshot(uuid)'::regprocedure,
                 'control.capture_customer_identity_after_stitch()'::regprocedure)
               ORDER BY p.oid""")
    assert after == before


def test_read_committed_stitch_is_rejected_not_published(db):
    install(db)
    first = fixture.batch(db, fixture.A)
    original = fixture.member(db, first, 'old-image', first='Before')
    fixture.query(db, 'INSERT INTO staging.customer_profile_history '
                      'SELECT * FROM staging.customer_profile WHERE raw_customer_id=%s',
                  (original['raw'],))
    transfer = fixture.finish(db, first)
    assert (transfer['status'], transfer['error_code'], transfer['row_count']) == (
        'BLOCKED', 'SNAPSHOT_ISOLATION_REQUIRED', 0), transfer
    assert fixture.one(db, 'SELECT count(*) FROM control.customer_identity_export '
                           'WHERE transfer_id=%s', (first['run'],)) == 0


def test_serialization_failure_unwinds_whole_resolution_commit(db):
    install(db)
    first = fixture.batch(db, fixture.A)
    original = fixture.member(db, first, 'serialize-me', first='Before')
    fixture.query(db, 'INSERT INTO staging.customer_profile_history '
                      'SELECT * FROM staging.customer_profile WHERE raw_customer_id=%s',
                  (original['raw'],))
    db.commit()
    # Force the same SQLSTATE PostgreSQL raises for a concurrent RR UPDATE.
    # A trigger must not turn it into a durable, nonretryable BLOCKED transfer.
    fixture.query(db, """CREATE OR REPLACE FUNCTION control.capture_customer_identity_snapshot(job_id uuid)
        RETURNS void LANGUAGE plpgsql SET search_path=pg_catalog,pg_temp AS $body$
        BEGIN RAISE EXCEPTION USING ERRCODE='40001',MESSAGE='synthetic serialization race'; END $body$""")
    db.commit()
    db.set_session(isolation_level='REPEATABLE READ')
    with pytest.raises(psycopg2.errors.SerializationFailure):
        fixture.finish(db, first)
    db.rollback()
    assert fixture.one(db, 'SELECT status FROM control.stitching_job WHERE job_id=%s',
                       (first['job'],)) == 'RUNNING'
    assert fixture.one(db, 'SELECT count(*) FROM control.customer_identity_transfer '
                           'WHERE stitching_job_id=%s', (first['job'],)) == 0


def test_older_batch_publishes_current_identity_when_later_upload_overlaps(db):
    install(db)
    first = fixture.batch(db, fixture.A)
    old_member = fixture.member(db, first, 'shared-source-user', first='Before',
                                email='before@example.test')
    fixture.query(db, "INSERT INTO staging.customer_profile_history "
                      "SELECT * FROM staging.customer_profile WHERE raw_customer_id=%s",
                  (old_member['raw'],))
    later = fixture.batch(db, fixture.A)
    fixture.member(db, later, 'shared-source-user', raw=old_member['raw'],
                   entity=old_member['entity'], first='After',
                   email='after@example.test')
    fixture.query(db, "INSERT INTO staging.customer_profile_history "
                      "SELECT * FROM staging.customer_profile WHERE raw_customer_id=%s",
                  (old_member['raw'],))
    fixture.query(db, 'UPDATE identity.customer_entity SET member_count=1, '
                      'last_resolution_run_id=%s WHERE customer_entity_id=%s',
                  (first['run'], old_member['entity']))
    fixture.query(db, 'UPDATE identity.customer_entity_member SET resolution_run_id=%s '
                      'WHERE standardized_record_id=%s',
                  (first['run'], old_member['standard']))
    assert fixture.one(db, 'SELECT batch_id FROM staging.customer_profile WHERE raw_customer_id=%s',
                       (old_member['raw'],)) == later['batch']
    assert fixture.one(db, 'SELECT first_name_raw FROM staging.customer_profile_history '
                           'WHERE batch_id=%s AND raw_customer_id=%s',
                       (first['batch'], old_member['raw'])) == 'Before'

    pin_matcher_snapshot(db)
    transfer = fixture.finish(db, first)

    assert transfer['status'] == 'PENDING', transfer
    assert (transfer['row_count'], transfer['selected_row_count']) == (1, 1)
    payload = fixture.one(db, 'SELECT payload FROM control.customer_identity_export '
                              'WHERE transfer_id=%s', (first['run'],))
    # Global resolution uses the latest normalized source profile. The selected
    # batch stays provably present in immutable typed history; it must not force
    # a stale profile over a newer upload of that same source identity.
    assert payload['profile']['first_name'] == 'After'
    assert payload['profile']['email'] == 'after@example.test'
    assert payload['user_id'] == 'shared-source-user'


def test_completed_newer_resolution_cannot_be_published_as_old_run(db):
    install(db)
    first = fixture.batch(db, fixture.A)
    original = fixture.member(db, first, 'shared-user', first='Before')
    fixture.query(db, "INSERT INTO staging.customer_profile_history "
                      "SELECT * FROM staging.customer_profile WHERE raw_customer_id=%s",
                  (original['raw'],))
    later = fixture.batch(db, fixture.A)
    fixture.member(db, later, 'shared-user', raw=original['raw'],
                   entity=original['entity'], first='After')
    fixture.query(db, "INSERT INTO staging.customer_profile_history "
                      "SELECT * FROM staging.customer_profile WHERE raw_customer_id=%s",
                  (original['raw'],))
    # The subsequent run has already replaced global membership. Historical
    # evidence is complete, but it cannot be published under the earlier run.
    fixture.query(db, 'UPDATE identity.customer_entity SET last_resolution_run_id=%s '
                      'WHERE customer_entity_id=%s', (later['run'], original['entity']))
    fixture.query(db, 'UPDATE identity.customer_entity_member SET resolution_run_id=%s '
                      'WHERE standardized_record_id=%s', (later['run'], original['standard']))
    pin_matcher_snapshot(db)
    transfer = fixture.finish(db, first)
    assert (transfer['status'], transfer['error_code'], transfer['row_count']) == (
        'BLOCKED', 'STALE_RESOLUTION', 0), transfer
    assert fixture.one(db, 'SELECT count(*) FROM control.customer_identity_export '
                           'WHERE transfer_id=%s', (first['run'],)) == 0


def test_matched_input_snapshot_survives_overlapping_ingestion(db):
    install(db)
    first = fixture.batch(db, fixture.A)
    original = fixture.member(db, first, 'same-source', first='Before')
    fixture.query(db, 'INSERT INTO staging.customer_profile_history '
                      'SELECT * FROM staging.customer_profile WHERE raw_customer_id=%s',
                  (original['raw'],))
    db.commit()
    # The matcher begins before a concurrent new upload commits. Capture must
    # export that same matched snapshot, not a newer profile at commit time.
    db.set_session(isolation_level='REPEATABLE READ')
    assert fixture.one(db, 'SELECT first_name_raw FROM staging.customer_profile '
                           'WHERE raw_customer_id=%s', (original['raw'],)) == 'Before'
    with psycopg2.connect(db.dsn) as ingest:
        later = fixture.batch(ingest, fixture.A)
        fixture.member(ingest, later, 'same-source', raw=original['raw'],
                       entity=original['entity'], first='After')
        fixture.query(ingest, 'INSERT INTO staging.customer_profile_history '
                              'SELECT * FROM staging.customer_profile WHERE raw_customer_id=%s',
                      (original['raw'],))
        ingest.commit()
    assert fixture.one(db, 'SELECT first_name_raw FROM staging.customer_profile '
                           'WHERE raw_customer_id=%s', (original['raw'],)) == 'Before'
    transfer = fixture.finish(db, first)
    assert (transfer['status'], transfer['row_count']) == ('PENDING', 1), transfer
    payload = fixture.one(db, 'SELECT payload FROM control.customer_identity_export '
                              'WHERE transfer_id=%s', (first['run'],))
    assert payload['profile']['first_name'] == 'Before'
    assert fixture.one(db, 'SELECT first_name_raw FROM staging.customer_profile '
                           'WHERE raw_customer_id=%s', (original['raw'],)) == 'After'


def test_two_overlapping_uploads_get_immutable_separate_publication_intents(db):
    install(db)
    first = fixture.batch(db, fixture.A)
    person = fixture.member(db, first, 'reused-source-user', first='First')
    fixture.query(db, 'INSERT INTO staging.customer_profile_history '
                      'SELECT * FROM staging.customer_profile WHERE raw_customer_id=%s',
                  (person['raw'],))
    pin_matcher_snapshot(db)
    first_transfer = fixture.finish(db, first)
    assert first_transfer['status'] == 'PENDING', first_transfer
    first_payload = fixture.one(db, 'SELECT payload FROM control.customer_identity_export '
                                    'WHERE transfer_id=%s', (first['run'],))
    assert first_payload['profile']['first_name'] == 'First'
    db.commit()
    db.set_session(isolation_level='READ COMMITTED')

    later = fixture.batch(db, fixture.A)
    fixture.member(db, later, 'reused-source-user', raw=person['raw'],
                   entity=person['entity'], first='Later')
    fixture.query(db, 'INSERT INTO staging.customer_profile_history '
                      'SELECT * FROM staging.customer_profile WHERE raw_customer_id=%s',
                  (person['raw'],))
    fixture.query(db, 'UPDATE identity.customer_entity SET last_resolution_run_id=%s '
                      ', member_count=1 WHERE customer_entity_id=%s', (later['run'], person['entity']))
    fixture.query(db, 'UPDATE identity.customer_entity_member SET resolution_run_id=%s '
                      'WHERE standardized_record_id=%s', (later['run'], person['standard']))
    pin_matcher_snapshot(db)
    later_transfer = fixture.finish(db, later)
    assert (later_transfer['status'], later_transfer['row_count']) == ('PENDING', 1), later_transfer
    assert fixture.one(db, 'SELECT payload->\'profile\'->>\'first_name\' '
                           'FROM control.customer_identity_export WHERE transfer_id=%s',
                       (later['run'],)) == 'Later'
    assert fixture.one(db, 'SELECT payload FROM control.customer_identity_export '
                           'WHERE transfer_id=%s', (first['run'],)) == first_payload
    assert fixture.one(db, 'SELECT count(*) FROM control.customer_identity_transfer '
                           'WHERE status=\'PENDING\'') == 2


def test_ingestion_does_not_force_capture_lock_block_after_match_snapshot(db):
    install(db)
    first = fixture.batch(db, fixture.A)
    original = fixture.member(db, first, 'overlap-source', first='Before')
    fixture.query(db, 'INSERT INTO staging.customer_profile_history '
                      'SELECT * FROM staging.customer_profile WHERE raw_customer_id=%s',
                  (original['raw'],))
    db.commit()
    db.set_session(isolation_level='REPEATABLE READ')
    assert fixture.one(db, 'SELECT first_name_raw FROM staging.customer_profile '
                           'WHERE raw_customer_id=%s', (original['raw'],)) == 'Before'
    ingest = psycopg2.connect(db.dsn)
    try:
        # Hold the actual ingestion writer's ROW EXCLUSIVE lock while A's
        # successful stitch triggers immutable capture. No fake pg_locks mocks.
        second = fixture.batch(ingest, fixture.A)
        fixture.query(ingest, 'UPDATE raw.customer_current SET batch_id=%s,first_name=%s '
                              'WHERE raw_customer_id=%s',
                      (second['batch'], 'After', original['raw']))
        started = time.perf_counter()
        transfer = fixture.finish(db, first)
        elapsed = time.perf_counter() - started
        assert (transfer['status'], transfer['row_count']) == ('PENDING', 1), transfer
        assert elapsed < 5, f'capture waited on unrelated in-flight ingestion: {elapsed:.3f}s'
        payload = fixture.one(db, 'SELECT payload FROM control.customer_identity_export '
                                  'WHERE transfer_id=%s', (first['run'],))
        assert payload['profile']['first_name'] == 'Before'
    finally:
        ingest.rollback()
        ingest.close()


def test_missing_immutable_selected_history_blocks_capture(db):
    install(db)
    first = fixture.batch(db, fixture.A)
    fixture.member(db, first, 'missing-history-user', history=True)
    pin_matcher_snapshot(db)
    transfer = fixture.finish(db, first)
    assert (transfer['status'], transfer['error_code'], transfer['row_count']) == (
        'BLOCKED', 'SOURCE_IDENTITY_CONFLICT', 0), transfer
    assert fixture.one(db, 'SELECT count(*) FROM control.customer_identity_export '
                           'WHERE transfer_id=%s', (first['run'],)) == 0


def test_cross_app_reused_raw_id_still_blocks_without_leaking_profiles(db):
    install(db)
    first = fixture.batch(db, fixture.A)
    original = fixture.member(db, first, 'same-source-user', first='App A')
    fixture.query(db, "INSERT INTO staging.customer_profile_history "
                      "SELECT * FROM staging.customer_profile WHERE raw_customer_id=%s",
                  (original['raw'],))
    other = fixture.batch(db, fixture.B)
    fixture.member(db, other, 'same-source-user', raw=original['raw'],
                   entity=original['entity'], first='App B')
    fixture.query(db, 'UPDATE identity.customer_entity SET member_count=1, '
                      'last_resolution_run_id=%s WHERE customer_entity_id=%s',
                  (first['run'], original['entity']))
    fixture.query(db, 'UPDATE identity.customer_entity_member SET resolution_run_id=%s '
                      'WHERE standardized_record_id=%s',
                  (first['run'], original['standard']))
    pin_matcher_snapshot(db)
    transfer = fixture.finish(db, first)
    assert (transfer['status'], transfer['row_count']) == ('BLOCKED', 0), transfer
    assert transfer['error_code'] in ('SOURCE_IDENTITY_CONFLICT', 'MIXED_OR_UNKNOWN_OWNER')
    assert fixture.one(db, 'SELECT count(*) FROM control.customer_identity_export '
                           'WHERE transfer_id=%s', (first['run'],)) == 0


def test_same_app_same_user_from_two_sources_retains_both_provenance(db, monkeypatch):
    """Two source identities may share a literal customer ID within one app."""
    install(db)
    first = fixture.batch(db, fixture.A, source=1)
    a = fixture.member(db, first, 'overlapping-id', first='Source A')
    second = fixture.batch(db, fixture.A, source=2)
    b = fixture.member(db, second, 'overlapping-id', entity=a['entity'],
                       first='Source B')
    assert a['raw'] != b['raw']
    fixture.query(db, 'INSERT INTO staging.customer_profile_history '
                      'SELECT * FROM staging.customer_profile WHERE raw_customer_id IN (%s,%s)',
                  (a['raw'], b['raw']))
    fixture.query(db, 'UPDATE identity.customer_entity SET member_count=2, '
                      'last_resolution_run_id=%s WHERE customer_entity_id=%s',
                  (first['run'], a['entity']))
    fixture.query(db, 'UPDATE identity.customer_entity_member SET resolution_run_id=%s '
                      'WHERE customer_entity_id=%s', (first['run'], a['entity']))
    pin_matcher_snapshot(db)
    transfer = fixture.finish(db, first)
    assert (transfer['status'], transfer['row_count'], transfer['member_count']) == (
        'PENDING', 1, 2), transfer
    assert fixture.one(db, 'SELECT count(*) FROM control.customer_identity_member_export '
                           'WHERE transfer_id=%s', (first['run'],)) == 2
    monkeypatch.syspath_prepend(str(ROOT.parent / 'fargate-user-stitching/src'))
    from identity_stitcher.publication import PublicationWorker
    worker = PublicationWorker(db, None)
    proofs = list(worker._source_rows_with_proof(transfer))
    assert len(proofs) == 1
    payload = proofs[0][0][3]
    assert proofs[0][1] == (
        fixture.A, str(transfer['workspace_id']), payload['source_system_id'],
        'overlapping-id', str(a['entity']), payload['raw_customer_id'],
        payload['standardized_record_id'])
    assert len(list(worker._source(transfer))) == 1


def test_same_public_key_from_distinct_sources_keeps_distinct_entities(db):
    install(db)
    first = fixture.batch(db, fixture.A, source=1)
    a = fixture.member(db, first, 'ambiguous-user', first='Person A')
    second = fixture.batch(db, fixture.A, source=2)
    b = fixture.member(db, second, 'ambiguous-user', first='Person B')
    fixture.query(db, 'INSERT INTO staging.customer_profile_history '
                      'SELECT * FROM staging.customer_profile WHERE raw_customer_id IN (%s,%s)',
                  (a['raw'], b['raw']))
    fixture.query(db, 'UPDATE identity.customer_entity SET last_resolution_run_id=%s '
                      'WHERE customer_entity_id IN (%s,%s)', (first['run'], a['entity'], b['entity']))
    fixture.query(db, 'UPDATE identity.customer_entity_member SET resolution_run_id=%s '
                      'WHERE standardized_record_id IN (%s,%s)',
                  (first['run'], a['standard'], b['standard']))
    pin_matcher_snapshot(db)
    transfer = fixture.finish(db, first)
    assert (transfer['status'], transfer['row_count'], transfer['member_count']) == (
        'PENDING', 1, 1), transfer
    assert fixture.one(db, 'SELECT payload->>\'customer_entity_id\' '
                           'FROM control.customer_identity_export WHERE transfer_id=%s',
                       (first['run'],)) == a['entity']
    # Each source is independently identified; the public app/user projection
    # has one current winner, but does not merge two different people in PG.
    assert fixture.one(db, 'SELECT count(DISTINCT customer_entity_id) '
                           'FROM identity.customer_entity_member '
                           'WHERE source_customer_id=%s', ('ambiguous-user',)) == 2


@pytest.mark.parametrize('row_count', [10000, 100000])
@pytest.mark.parametrize('candidate', [False, True])
def test_overlap_capture_stays_set_based_at_scale(db, row_count, candidate):
    if candidate:
        install(db)
    else:
        fixture.query(db, M041.read_text())
        fixture.query(db, M042.read_text())
    first = fixture.batch(db, fixture.A)
    fixture.query(db, """INSERT INTO raw.customer_current(batch_id,source_system_id,source_customer_id)
                     SELECT %s,1,'synthetic-'||lpad(i::text,8,'0') FROM generate_series(1,%s) i""",
                  (first['batch'], row_count))
    fixture.query(db, """INSERT INTO raw.customer_batch_member(ingestion_batch_id,raw_customer_id,source_system_id,source_customer_id)
                     SELECT batch_id,raw_customer_id,source_system_id,source_customer_id FROM raw.customer_current
                     ON CONFLICT (ingestion_batch_id,raw_customer_id) DO NOTHING""")
    fixture.query(db, """INSERT INTO raw.customer(raw_customer_id,batch_id,source_system_id,source_customer_id)
                     SELECT raw_customer_id,batch_id,source_system_id,source_customer_id FROM raw.customer_current
                     ON CONFLICT (batch_id,raw_customer_id) DO NOTHING""")
    fixture.query(db, """INSERT INTO staging.customer_profile(raw_customer_id,batch_id,source_system_id,source_customer_id,standardized_at)
                     SELECT raw_customer_id,batch_id,source_system_id,source_customer_id,'2026-01-01' FROM raw.customer_current""")
    fixture.query(db, "INSERT INTO staging.customer_profile_history SELECT * FROM staging.customer_profile")
    fixture.query(db, """INSERT INTO identity.customer_entity(customer_entity_id,last_resolution_run_id)
                     SELECT standardized_record_id,%s FROM staging.customer_profile""", (first['run'],))
    fixture.query(db, """INSERT INTO identity.customer_entity_member(standardized_record_id,customer_entity_id,source_system_id,source_customer_id,match_method,resolution_run_id)
                     SELECT standardized_record_id,standardized_record_id,source_system_id,source_customer_id,'singleton',%s
                     FROM staging.customer_profile""", (first['run'],))
    fixture.query(db, "ANALYZE raw.customer_batch_member; ANALYZE staging.customer_profile;")
    fixture.query(db, "ANALYZE staging.customer_profile_history; ANALYZE identity.customer_entity_member;")
    db.commit()
    if candidate:
        db.set_session(isolation_level='REPEATABLE READ')
    started = time.perf_counter()
    transfer = fixture.finish(db, first)
    elapsed = time.perf_counter() - started
    print(f'capture_plus_commit_{"candidate" if candidate else "baseline"}_{row_count}_seconds={elapsed:.3f}')
    assert (transfer['status'], transfer['row_count']) == ('PENDING', row_count), transfer
    assert elapsed < (20 if row_count == 10000 else 40), 'overlap guard must remain set-based under indexed history'
