"""Disposable PostgreSQL contract: a legacy UUID alias cannot form a new logical tuple.

049 must not be installed until a coordinated member/current/staging schema exists.
"""
from uuid import uuid4

import pytest

import test_customer_global_upload_capture as fixture
import test_customer_concurrent_capture as overlap

db = fixture.db


def make_selected_with_old_alias(conn):
    """The selected immutable/current/staging tuple is intact; old raw is another app."""
    overlap.install(conn)
    old = fixture.batch(conn, fixture.B, source=1)
    original = fixture.member(conn, old, 'old-owner', first='Old B')
    fixture.query(conn, 'INSERT INTO staging.customer_profile_history '
                        'SELECT * FROM staging.customer_profile WHERE raw_customer_id=%s',
                  (original['raw'],))
    selected = fixture.batch(conn, fixture.A, source=2)
    fixture.query(conn, "UPDATE control.ingestion_batch SET created_at=created_at+interval '1 second' WHERE batch_id=%s", (selected['batch'],))
    current = fixture.member(conn, selected, 'new-owner', raw=original['raw'],
                             entity=original['entity'], first='New A')
    fixture.query(conn, 'INSERT INTO staging.customer_profile_history '
                        'SELECT * FROM staging.customer_profile WHERE raw_customer_id=%s',
                  (current['raw'],))
    fixture.query(conn, 'UPDATE identity.customer_entity SET member_count=1,last_resolution_run_id=%s '
                        'WHERE customer_entity_id=%s', (selected['run'], current['entity']))
    fixture.query(conn, 'UPDATE identity.customer_entity_member SET resolution_run_id=%s '
                        'WHERE standardized_record_id=%s', (selected['run'], current['standard']))
    assert fixture.one(conn, '''SELECT count(*) FROM raw.customer_batch_member b
        JOIN raw.customer h ON (h.batch_id,h.raw_customer_id,h.source_system_id,h.source_customer_id)=
             (b.ingestion_batch_id,b.raw_customer_id,b.source_system_id,b.source_customer_id)
        JOIN staging.customer_profile_history p ON (p.batch_id,p.raw_customer_id,p.source_system_id,p.source_customer_id)=
             (b.ingestion_batch_id,b.raw_customer_id,b.source_system_id,b.source_customer_id)
        WHERE b.ingestion_batch_id=%s AND p.app_id=%s
          AND b.source_system_id=%s AND b.source_customer_id='new-owner' ''',
        (selected['batch'], fixture.A, selected['source'])) == 0
    # The selected batch has a raw observation, but it cannot be the new logical
    # source because current/history remain FK-bound to the older identity.
    return old, selected, current


def finish_rr(conn, selected):
    overlap.pin_matcher_snapshot(conn)
    return fixture.finish(conn, selected)


def test_selected_legacy_alias_two_logical_tuples_are_not_representable_in_current_schema(db):
    """RED for ordinary selected-source projection, not an unsafe guard bypass."""
    overlap.install(db)
    app_b = fixture.batch(db, fixture.B, source=1)
    old = fixture.member(db, app_b, 'shared-user')
    raw = old['raw']
    app_a = fixture.batch(db, fixture.A, source=2)
    fixture.query(db, "UPDATE control.ingestion_batch SET created_at=created_at+interval '1 second' WHERE batch_id=%s", (app_a['batch'],))
    # These are distinct logical source keys. A provenance alias must not
    # overwrite B's current source or entity membership.
    fixture.query(db, '''INSERT INTO raw.customer_current
        (raw_customer_id,batch_id,source_system_id,source_customer_id)
        VALUES (%s,%s,%s,'shared-user')''', (raw, app_a['batch'], app_a['source']))
    assert fixture.one(db, '''SELECT count(*) FROM raw.customer_current
        WHERE raw_customer_id=%s''', (raw,)) == 2
    assert fixture.one(db, '''SELECT count(*) FROM raw.customer
        WHERE raw_customer_id=%s''', (raw,)) == 2


def test_legacy_raw_alias_cannot_satisfy_new_exact_selected_tuple(db):
    old, selected, current = make_selected_with_old_alias(db)
    assert old['batch'] != selected['batch']
    transfer = finish_rr(db, selected)
    assert (transfer['status'], transfer['error_code'], transfer['row_count']) == (
        'BLOCKED', 'SOURCE_IDENTITY_CONFLICT', 0), transfer
    assert fixture.one(db, 'SELECT count(*) FROM control.customer_identity_export WHERE transfer_id=%s',
                       (selected['run'],)) == 0


def test_legacy_alias_current_source_cannot_be_changed_while_history_exists(db):
    import psycopg2
    _, selected, current = make_selected_with_old_alias(db)
    db.commit()
    db.autocommit = False
    fixture.query(db, 'SAVEPOINT retain_history')
    with pytest.raises(psycopg2.errors.ForeignKeyViolation, match='customer_history_current_fkey'):
        fixture.query(db, '''UPDATE raw.customer_current SET source_system_id=%s,source_customer_id=%s
            WHERE raw_customer_id=%s''', (selected['source'], 'new-owner', current['raw']))
    fixture.query(db, 'ROLLBACK TO SAVEPOINT retain_history')
    assert fixture.one(db, 'SELECT count(*) FROM raw.customer WHERE raw_customer_id=%s',
                       (current['raw'],)) == 2


def test_mismatched_selected_history_still_blocks(db):
    _, selected, current = make_selected_with_old_alias(db)
    fixture.query(db, 'DELETE FROM staging.customer_profile_history WHERE batch_id=%s AND raw_customer_id=%s',
                  (selected['batch'], current['raw']))
    transfer = finish_rr(db, selected)
    assert (transfer['status'], transfer['error_code'], transfer['row_count']) == ('BLOCKED', 'SOURCE_IDENTITY_CONFLICT', 0), transfer
    assert fixture.one(db, 'SELECT count(*) FROM control.customer_identity_export WHERE transfer_id=%s', (selected['run'],)) == 0


def test_selected_owner_conflict_still_blocks(db):
    _, selected, current = make_selected_with_old_alias(db)
    fixture.query(db, 'UPDATE staging.customer_profile SET app_id=%s WHERE raw_customer_id=%s',
                  (fixture.B, current['raw']))
    transfer = finish_rr(db, selected)
    assert (transfer['status'], transfer['row_count']) == ('BLOCKED', 0), transfer
    assert transfer['error_code'] in {'SOURCE_IDENTITY_CONFLICT', 'MIXED_OR_UNKNOWN_OWNER'}
    assert fixture.one(db, 'SELECT count(*) FROM control.customer_identity_export WHERE transfer_id=%s', (selected['run'],)) == 0
