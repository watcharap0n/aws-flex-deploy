"""050 active staging/member cutover on a fresh, owned PostgreSQL 55441 database."""
from pathlib import Path
from uuid import uuid4

import psycopg2
import pytest
import test_customer_global_upload_capture as base
import re

ROOT = Path(__file__).resolve().parents[2]
MIGRATION = ROOT / 'database/migrations/050_customer_logical_member_cutover.sql'
db = base.db


def test_exact_lineage_checks_use_indexable_nonnull_keys():
    sql = MIGRATION.read_text()
    assert re.search(r'NOT EXISTS\s*\(SELECT FROM raw\.customer r WHERE\s*'
                     r'r\.batch_id=h\.batch_id AND r\.raw_customer_id=h\.raw_customer_id\s*'
                     r'AND \(r\.source_system_id,r\.source_customer_id,r\.app_id\)\s*'
                     r'IS NOT DISTINCT FROM\s*'
                     r'\(h\.source_system_id,h\.source_customer_id,h\.app_id\)', sql)
    assert re.search(r'NOT EXISTS\s*\(SELECT FROM staging\.customer_profile_history h\s*'
                     r'WHERE h\.batch_id=me\.batch_id AND h\.raw_customer_id=me\.raw_customer_id\s*'
                     r'AND \(h\.source_system_id,h\.source_customer_id,h\.app_id\)\s*'
                     r'IS NOT DISTINCT FROM\s*'
                     r'\(me\.source_system_id,me\.source_customer_id,me\.app_id\)', sql)


def test_published_history_ownership_uses_indexed_bridge_when_present():
    sql = MIGRATION.read_text()
    block = sql[sql.index("-- A released ID may never"):sql.index("-- Cross-app historical reuse")]
    assert "IF to_regclass('control.customer_identity_member_export') IS NOT NULL THEN" in block
    assert re.search(r'WITH bridge_owners AS MATERIALIZED\s*\(SELECT DISTINCT ON\s*\(standardized_record_id\)\s*standardized_record_id,app_id,source_system_id,source_customer_id\s+FROM control\.customer_identity_member_export\s+ORDER BY standardized_record_id\)\s*SELECT FROM staging\.customer_profile_history h\s+JOIN bridge_owners me USING\(standardized_record_id\)', block)
    assert not re.search(r'JOIN control\.customer_identity_member_export me\s+USING\(standardized_record_id\)', block)
    assert 'JOIN control.customer_identity_export e\n' in block  # optional-bridge fallback
    assert "ELSE\n        -- 041 bridge is optional" in block
    ownership = sql[sql.index("IF to_regclass('control.customer_identity_member_export') IS NOT NULL THEN", sql.index('050 published standardized ID ownership collision')):sql.index('-- A released ID may never')]
    assert not re.search(r"e\.payload->>'standardized_record_id'=me\.standardized_record_id::text", ownership)
    assert 'me.standardized_record_id=CASE WHEN' in ownership


def test_allocated_guard_probes_indexed_materialized_released_owners():
    sql = MIGRATION.read_text()
    allocated = sql[sql.index('DO $allocated_identity$'):sql.index('END $allocated_identity$')]
    assert 'FROM customer_050_released_owner owner' in allocated
    assert 'CREATE INDEX ON customer_050_released_owner(standardized_record_id)' in sql
    assert 'FROM control.customer_identity_export e\n               WHERE e.payload' not in allocated


def test_published_export_history_probes_indexed_uuid_with_safe_cast():
    sql = MIGRATION.read_text()
    assert re.search(r'h\.raw_customer_id=CASE WHEN\s*'
                     r'e\.payload->>\'raw_customer_id\' !~\*.*? THEN NULL::uuid\s*'
                     r'ELSE \(e\.payload->>\'raw_customer_id\'\)::uuid END', sql, re.S)


def test_timeout_and_temp_guard_are_transaction_local():
    sql = MIGRATION.read_text()
    assert "SET LOCAL statement_timeout='30min';" in sql
    assert "SET LOCAL work_mem='128MB';" in sql
    assert "SET LOCAL temp_file_limit='4GB';" in sql


def test_malformed_published_raw_uuid_fails_without_unsafe_cast(db):
    batch = upload(db, base.A)
    own = base.member(db, batch, 'malformed')
    base.query(db, 'INSERT INTO staging.customer_profile_history SELECT * FROM staging.customer_profile')
    assert base.finish(db, batch)['status'] == 'PENDING'
    base.query(db, 'ALTER TABLE control.customer_identity_export DISABLE TRIGGER customer_identity_export_immutable')
    base.query(db, "UPDATE control.customer_identity_export SET payload=jsonb_set(payload,'{raw_customer_id}',to_jsonb('not-a-uuid'::text))")
    base.query(db, 'ALTER TABLE control.customer_identity_export ENABLE TRIGGER customer_identity_export_immutable')
    db.commit()
    with pytest.raises(psycopg2.Error, match='050.*published'):
        base.query(db, MIGRATION.read_text())
    db.rollback()
    assert base.one(db, "SELECT count(*) FROM pg_attribute WHERE attrelid='identity.customer_entity_member'::regclass AND attname='app_id' AND NOT attisdropped") == 0


def test_transaction_guards_reset_after_successful_cutover(db):
    batch = upload(db, base.A)
    member = base.member(db, batch, 'guard')
    base.query(db, 'INSERT INTO staging.customer_profile_history SELECT * FROM staging.customer_profile')
    before = base.query(db, 'SELECT current_setting(\'statement_timeout\'),current_setting(\'temp_file_limit\')')
    install(db)
    assert base.query(db, 'SELECT current_setting(\'statement_timeout\'),current_setting(\'temp_file_limit\')') == before
    assert base.one(db, 'SELECT app_id FROM identity.customer_entity_member WHERE standardized_record_id=%s', (member['standard'],)) == base.A


@pytest.mark.parametrize('field,relation', [
    ('source_customer_id', 'raw.customer'),
    ('app_id', 'raw.customer'),
    ('source_system_id', 'raw.customer'),
    ('source_customer_id', 'control.customer_identity_member_export'),
])
def test_exact_immutable_lineage_drift_fails_closed(db, field, relation):
    base.query(db, (ROOT / 'database/migrations/041_global_member_bridge.sql').read_text())
    db.commit()
    batch = upload(db, base.A)
    own = base.member(db, batch, 'lineage')
    base.query(db, 'INSERT INTO staging.customer_profile_history SELECT * FROM staging.customer_profile')
    assert base.finish(db, batch)['status'] == 'PENDING'
    if relation == 'raw.customer':
        # raw history is intentionally immutable; bypass only within this
        # disposable owned database to simulate preexisting corruption.
        base.query(db, 'ALTER TABLE raw.customer DISABLE TRIGGER ALL')
        if field == 'app_id':
            base.query(db, 'UPDATE raw.customer SET app_id=NULL WHERE raw_customer_id=%s', (own['raw'],))
        elif field == 'source_customer_id':
            base.query(db, 'UPDATE raw.customer SET source_customer_id=%s WHERE raw_customer_id=%s', ('drift', own['raw']))
        else:
            base.query(db, 'UPDATE raw.customer SET source_system_id=999 WHERE raw_customer_id=%s', (own['raw'],))
        base.query(db, 'ALTER TABLE raw.customer ENABLE TRIGGER ALL')
    else:
        base.query(db, 'ALTER TABLE control.customer_identity_member_export DISABLE TRIGGER ALL')
        base.query(db, "UPDATE control.customer_identity_member_export SET source_customer_id='drift'")
        base.query(db, 'ALTER TABLE control.customer_identity_member_export ENABLE TRIGGER ALL')
    db.commit()
    with pytest.raises(psycopg2.Error, match='050.*(?:historical|published)'):
        base.query(db, MIGRATION.read_text())
    db.rollback()
    assert base.one(db, "SELECT count(*) FROM pg_attribute WHERE attrelid='identity.customer_entity_member'::regclass AND attname='app_id' AND NOT attisdropped") == 0


def replay(db, count, *, active=True):
    """Each batch retains the same raw UUID but gets a new typed history ID."""
    first = upload(db, base.A)
    own = base.member(db, first, 'replay', first='First')
    batches = [first]
    ids = [own['standard']]
    base.query(db, 'INSERT INTO staging.customer_profile_history SELECT * FROM staging.customer_profile WHERE raw_customer_id=%s', (own['raw'],))
    for number in range(1, count):
        batch = upload(db, base.A)
        batches.append(batch)
        base.query(db, 'INSERT INTO raw.customer_batch_member(ingestion_batch_id,raw_customer_id,source_system_id,source_customer_id) VALUES (%s,%s,1,%s)', (batch['batch'], own['raw'], 'replay'))
        base.query(db, 'INSERT INTO raw.customer(raw_customer_id,batch_id,source_system_id,source_customer_id) VALUES (%s,%s,1,%s)', (own['raw'], batch['batch'], 'replay'))
        new_id = str(uuid4())
        ids.append(new_id)
        columns = [r[0] for r in base.query(db, "SELECT attname FROM pg_attribute WHERE attrelid='staging.customer_profile_history'::regclass AND attnum>0 AND NOT attisdropped ORDER BY attnum")]
        projection = ','.join('%s::uuid' if c == 'standardized_record_id' else '%s::uuid' if c == 'batch_id' else 'p."' + c + '"' for c in columns)
        base.query(db, f'INSERT INTO staging.customer_profile_history({",".join(chr(34)+c+chr(34) for c in columns)}) SELECT {projection} FROM staging.customer_profile p WHERE p.raw_customer_id=%s', (new_id, batch['batch'], own['raw']))
    if not active:
        base.query(db, 'DELETE FROM identity.customer_entity_member WHERE standardized_record_id=%s', (own['standard'],))
        base.query(db, 'DELETE FROM staging.customer_profile WHERE standardized_record_id=%s', (own['standard'],))
    return batches, own, ids


@pytest.mark.parametrize('count', [2, 3, 4, 5])
def test_repeated_typed_ids_preserve_active_member_and_immutable_history(db, count):
    _, own, ids = replay(db, count)
    history = base.query(db, 'SELECT batch_id,raw_customer_id,standardized_record_id FROM staging.customer_profile_history ORDER BY batch_id')
    assert set(r[2] for r in history) == set(ids)
    install(db)
    assert base.query(db, 'SELECT batch_id,raw_customer_id,standardized_record_id FROM staging.customer_profile_history ORDER BY batch_id') == history
    assert base.one(db, 'SELECT standardized_record_id FROM staging.customer_profile WHERE app_id=%s AND source_customer_id=%s', (base.A, 'replay')) == own['standard']
    assert base.query(db, 'SELECT standardized_record_id,customer_entity_id,app_id FROM identity.customer_entity_member') == [(own['standard'], own['entity'], base.A)]


@pytest.mark.parametrize('count', [2, 3, 4, 5])
def test_inactive_replay_allocates_one_stable_provenance_bound_id(db, count):
    _, own, ids = replay(db, count, active=False)
    history = base.query(db, 'SELECT batch_id,standardized_record_id FROM staging.customer_profile_history ORDER BY batch_id')
    install(db)
    assigned = base.one(db, 'SELECT standardized_record_id FROM staging.customer_profile WHERE app_id=%s AND source_customer_id=%s', (base.A, 'replay'))
    assert assigned not in ids
    expected = base.one(db, '''SELECT md5('050/customer-logical-member/v1:' ||
        length(app_id)::text || ':' || app_id || ':' || source_system_id::text || ':' ||
        length(source_customer_id)::text || ':' || source_customer_id || ':' ||
        batch_id::text || ':' || raw_customer_id::text)::uuid::text
        FROM staging.customer_profile WHERE standardized_record_id=%s''', (assigned,))
    assert assigned == expected
    assert base.query(db, 'SELECT batch_id,standardized_record_id FROM staging.customer_profile_history ORDER BY batch_id') == history
    assert base.one(db, 'SELECT count(*) FROM identity.customer_entity_member') == 0


def test_inactive_published_history_reserves_old_id_without_resurrection(db):
    batch, own, ids = replay(db, 2)
    transfer = base.finish(db, batch[0])
    assert transfer['status'] == 'PENDING'
    before = base.query(db, 'SELECT transfer_id,row_number,payload::text FROM control.customer_identity_export')
    base.query(db, 'DELETE FROM identity.customer_entity_member WHERE standardized_record_id=%s', (own['standard'],))
    base.query(db, 'DELETE FROM staging.customer_profile WHERE standardized_record_id=%s', (own['standard'],))
    db.commit()
    install(db)
    assert base.query(db, 'SELECT transfer_id,row_number,payload::text FROM control.customer_identity_export') == before
    assert base.one(db, 'SELECT standardized_record_id FROM staging.customer_profile WHERE source_customer_id=%s', ('replay',)) not in ids
    assert base.one(db, 'SELECT count(*) FROM identity.customer_entity_member') == 0


def test_published_payload_source_must_match_exact_typed_observation(db):
    batches, own, ids = replay(db, 2)
    transfer = base.finish(db, batches[0])
    assert transfer['status'] == 'PENDING'
    base.query(db, 'ALTER TABLE control.customer_identity_export DISABLE TRIGGER customer_identity_export_immutable')
    base.query(db, "UPDATE control.customer_identity_export SET payload=jsonb_set(payload,'{source_system_id}','999'::jsonb)")
    base.query(db, 'ALTER TABLE control.customer_identity_export ENABLE TRIGGER customer_identity_export_immutable')
    db.commit()
    with pytest.raises(psycopg2.Error, match='050.*published'):
        base.query(db, MIGRATION.read_text())
    db.rollback()


def test_immutable_publication_entity_drift_preserves_current_and_export_versions(db):
    """A historical transfer can outlive its member's entity assignment."""
    batches, own, _ = replay(db, 3)
    base.query(db, (ROOT / 'database/migrations/041_global_member_bridge.sql').read_text())
    db.commit()
    assert base.finish(db, batches[0])['status'] == 'PENDING'
    before = base.query(db, '''SELECT transfer_id,row_number,app_id,user_id,payload::text
        FROM control.customer_identity_export ORDER BY transfer_id,row_number''')
    bridge = base.query(db, '''SELECT transfer_id,standardized_record_id,customer_entity_id
        FROM control.customer_identity_member_export''')
    assert bridge
    new_entity = str(uuid4())
    base.query(db, '''INSERT INTO identity.customer_entity(customer_entity_id,last_resolution_run_id)
        VALUES (%s,%s)''', (new_entity,batches[0]['run']))
    base.query(db, '''UPDATE identity.customer_entity_member SET customer_entity_id=%s
        WHERE standardized_record_id=%s''', (new_entity,own['standard']))
    db.commit()
    install(db)
    assert base.query(db, '''SELECT transfer_id,row_number,app_id,user_id,payload::text
        FROM control.customer_identity_export ORDER BY transfer_id,row_number''') == before
    assert base.query(db, '''SELECT transfer_id,standardized_record_id,customer_entity_id
        FROM control.customer_identity_member_export''') == bridge
    assert base.one(db, '''SELECT customer_entity_id FROM identity.customer_entity_member
        WHERE standardized_record_id=%s''', (own['standard'],)) == new_entity
    assert base.one(db, "SELECT app_id FROM identity.customer_entity_member WHERE standardized_record_id=%s", (own['standard'],)) == base.A


def test_immutable_member_export_reserves_inactive_historical_identity(db):
    batches, own, _ = replay(db, 5)
    base.query(db, (ROOT / 'database/migrations/041_global_member_bridge.sql').read_text())
    db.commit()
    assert base.finish(db, batches[0])['status'] == 'PENDING'
    before = base.query(db, '''SELECT transfer_id,standardized_record_id,raw_customer_id,
        customer_entity_id FROM control.customer_identity_member_export''')
    base.query(db, 'DELETE FROM identity.customer_entity_member WHERE standardized_record_id=%s', (own['standard'],))
    base.query(db, 'DELETE FROM staging.customer_profile WHERE standardized_record_id=%s', (own['standard'],))
    db.commit()
    install(db)
    assert base.query(db, '''SELECT transfer_id,standardized_record_id,raw_customer_id,
        customer_entity_id FROM control.customer_identity_member_export''') == before
    assigned = base.one(db, 'SELECT standardized_record_id FROM staging.customer_profile WHERE app_id=%s AND source_customer_id=%s', (base.A, 'replay'))
    assert assigned not in {row[1] for row in before}
    assert base.one(db, 'SELECT count(*) FROM identity.customer_entity_member') == 0


def test_fresh_deterministic_id_must_not_reuse_released_id(db):
    batches, own, _ = replay(db, 3)
    assert base.finish(db, batches[0])['status'] == 'PENDING'
    base.query(db, 'DELETE FROM identity.customer_entity_member WHERE standardized_record_id=%s', (own['standard'],))
    base.query(db, 'DELETE FROM staging.customer_profile WHERE standardized_record_id=%s', (own['standard'],))
    expected = base.one(db, '''SELECT md5('050/customer-logical-member/v1:' ||
        length(h.app_id)::text || ':' || h.app_id || ':' || h.source_system_id::text || ':' ||
        length(h.source_customer_id)::text || ':' || h.source_customer_id || ':' ||
        h.batch_id::text || ':' || h.raw_customer_id::text)::uuid::text
        FROM staging.customer_profile_history h JOIN control.ingestion_batch b ON b.batch_id=h.batch_id
        WHERE h.app_id=%s AND h.source_customer_id='replay'
        ORDER BY b.created_at DESC,b.batch_id DESC LIMIT 1''', (base.A,))
    base.query(db, 'ALTER TABLE control.customer_identity_export DISABLE TRIGGER customer_identity_export_immutable')
    base.query(db, "UPDATE control.customer_identity_export SET payload=jsonb_set(payload,'{standardized_record_id}',to_jsonb(%s::text))", (expected,))
    base.query(db, 'ALTER TABLE control.customer_identity_export ENABLE TRIGGER customer_identity_export_immutable')
    db.commit()
    with pytest.raises(psycopg2.Error, match='050.*(?:allocated|released|collid)'):
        base.query(db, MIGRATION.read_text())
    db.rollback()
    assert base.one(db, "SELECT count(*) FROM pg_attribute WHERE attrelid='identity.customer_entity_member'::regclass AND attname='app_id' AND NOT attisdropped") == 0


def test_released_id_cannot_be_claimed_by_unrelated_historical_owner(db):
    base.query(db, (ROOT / 'database/migrations/041_global_member_bridge.sql').read_text())
    db.commit()
    first = upload(db, base.A)
    own = base.member(db, first, 'reserved')
    base.query(db, 'INSERT INTO staging.customer_profile_history SELECT * FROM staging.customer_profile')
    assert base.finish(db, first)['status'] == 'PENDING'
    second = upload(db, base.B)
    other = base.member(db, second, 'alias')
    base.query(db, 'INSERT INTO staging.customer_profile_history SELECT * FROM staging.customer_profile WHERE standardized_record_id=%s', (other['standard'],))
    base.query(db, 'ALTER TABLE staging.customer_profile_history DISABLE TRIGGER customer_app_id_derive')
    base.query(db, 'UPDATE staging.customer_profile_history SET standardized_record_id=%s WHERE standardized_record_id=%s', (own['standard'],other['standard']))
    base.query(db, 'ALTER TABLE staging.customer_profile_history ENABLE TRIGGER customer_app_id_derive')
    db.commit()
    with pytest.raises(psycopg2.Error, match='050.*(?:published|collid)'):
        base.query(db, MIGRATION.read_text())
    db.rollback()
    assert base.one(db, "SELECT count(*) FROM pg_attribute WHERE attrelid='identity.customer_entity_member'::regclass AND attname='app_id' AND NOT attisdropped") == 0


@pytest.mark.parametrize('bridge', [False, True])
def test_exported_id_historical_collision_fails_with_or_without_optional_bridge(db, bridge):
    """Both catalog shapes reject history claiming a published ID for another tuple."""
    if bridge:
        base.query(db, (ROOT / 'database/migrations/041_global_member_bridge.sql').read_text())
        db.commit()
    first = upload(db, base.A)
    published = base.member(db, first, 'published-owner')
    base.query(db, 'INSERT INTO staging.customer_profile_history SELECT * FROM staging.customer_profile')
    assert base.finish(db, first)['status'] == 'PENDING'
    second = upload(db, base.B)
    other = base.member(db, second, 'other-owner')
    base.query(db, 'INSERT INTO staging.customer_profile_history SELECT * FROM staging.customer_profile WHERE standardized_record_id=%s', (other['standard'],))
    base.query(db, 'ALTER TABLE staging.customer_profile_history DISABLE TRIGGER customer_app_id_derive')
    base.query(db, 'UPDATE staging.customer_profile_history SET standardized_record_id=%s WHERE standardized_record_id=%s',
               (published['standard'], other['standard']))
    base.query(db, 'ALTER TABLE staging.customer_profile_history ENABLE TRIGGER customer_app_id_derive')
    db.commit()
    assert bool(base.one(db, "SELECT to_regclass('control.customer_identity_member_export') IS NOT NULL")) == bridge
    with pytest.raises(psycopg2.Error, match='050 published (?:member )?ID collides with historical owner'):
        base.query(db, MIGRATION.read_text())
    db.rollback()
    assert base.one(db, "SELECT count(*) FROM pg_attribute WHERE attrelid='identity.customer_entity_member'::regclass AND attname='app_id' AND NOT attisdropped") == 0


def test_versioned_export_with_changed_entity_and_inactive_key_cutover(db):
    base.query(db, (ROOT / 'database/migrations/041_global_member_bridge.sql').read_text())
    db.commit()
    first = upload(db, base.A)
    old = base.member(db, first, 'versioned')
    inactive = base.member(db, first, 'inactive')
    base.query(db, 'INSERT INTO staging.customer_profile_history SELECT * FROM staging.customer_profile')
    base.query(db, 'UPDATE identity.customer_entity_member SET resolution_run_id=%s', (first['run'],))
    base.query(db, 'UPDATE identity.customer_entity SET last_resolution_run_id=%s', (first['run'],))
    assert base.finish(db, first)['status'] == 'PENDING'
    second = upload(db, base.A)
    base.query(db, 'UPDATE raw.customer_current SET batch_id=%s WHERE raw_customer_id=%s', (second['batch'], old['raw']))
    base.query(db, '''INSERT INTO raw.customer(raw_customer_id,batch_id,source_system_id,source_customer_id)
        VALUES (%s,%s,1,'versioned') ON CONFLICT (batch_id,raw_customer_id) DO NOTHING''', (old['raw'], second['batch']))
    base.query(db, 'UPDATE staging.customer_profile SET batch_id=%s WHERE standardized_record_id=%s', (second['batch'],old['standard']))
    base.query(db, 'INSERT INTO staging.customer_profile_history SELECT * FROM staging.customer_profile WHERE standardized_record_id=%s', (old['standard'],))
    newer_entity = str(uuid4())
    base.query(db, 'INSERT INTO identity.customer_entity(customer_entity_id,last_resolution_run_id) VALUES (%s,%s)', (newer_entity,second['run']))
    base.query(db, 'UPDATE identity.customer_entity_member SET customer_entity_id=%s,resolution_run_id=%s WHERE standardized_record_id=%s', (newer_entity,second['run'],old['standard']))
    base.query(db, 'UPDATE identity.customer_entity SET last_resolution_run_id=%s WHERE customer_entity_id=%s', (second['run'],newer_entity))
    assert base.finish(db, second)['status'] == 'PENDING'
    # A later operational re-normalization changes the ACTIVE ID while both
    # immutable transfer versions retain their original ID and entity.
    active_id = str(uuid4())
    base.query(db, 'DELETE FROM identity.customer_entity_member WHERE standardized_record_id=%s', (old['standard'],))
    base.query(db, 'UPDATE staging.customer_profile SET standardized_record_id=%s WHERE standardized_record_id=%s', (active_id,old['standard']))
    base.query(db, '''INSERT INTO identity.customer_entity_member
        (standardized_record_id,customer_entity_id,source_system_id,source_customer_id,match_method,resolution_run_id)
        VALUES (%s,%s,1,'versioned','singleton',%s)''', (active_id,newer_entity,second['run']))
    prior = base.query(db, 'SELECT transfer_id,row_number,app_id,user_id,payload::text FROM control.customer_identity_export ORDER BY transfer_id,row_number')
    assert len(prior) >= 3
    assert len({row[4] for row in prior if row[3]=='versioned'}) == 2
    bridge = base.query(db, 'SELECT * FROM control.customer_identity_member_export ORDER BY transfer_id,standardized_record_id')
    assert base.one(db, '''SELECT count(*) FROM control.customer_identity_member_export
        WHERE standardized_record_id=%s''', (old['standard'],)) >= 2
    base.query(db, 'DELETE FROM identity.customer_entity_member WHERE standardized_record_id=%s', (inactive['standard'],))
    base.query(db, 'DELETE FROM staging.customer_profile WHERE standardized_record_id=%s', (inactive['standard'],))
    db.commit()
    install(db)
    assert base.query(db, 'SELECT transfer_id,row_number,app_id,user_id,payload::text FROM control.customer_identity_export ORDER BY transfer_id,row_number') == prior
    assert base.query(db, 'SELECT * FROM control.customer_identity_member_export ORDER BY transfer_id,standardized_record_id') == bridge
    assert base.one(db, 'SELECT customer_entity_id FROM identity.customer_entity_member WHERE standardized_record_id=%s', (active_id,)) == newer_entity
    assert base.one(db, 'SELECT standardized_record_id FROM staging.customer_profile WHERE source_customer_id=%s', ('inactive',)) != inactive['standard']


def test_unowned_cross_app_historical_alias_gets_distinct_provenance_id(db):
    first = upload(db, base.A)
    own = base.member(db, first, 'shared')
    base.query(db, 'INSERT INTO staging.customer_profile_history SELECT * FROM staging.customer_profile WHERE raw_customer_id=%s', (own['raw'],))
    second = upload(db, base.B)
    base.query(db, 'INSERT INTO raw.customer_batch_member(ingestion_batch_id,raw_customer_id,source_system_id,source_customer_id) VALUES (%s,%s,1,%s)', (second['batch'], own['raw'], 'shared'))
    base.query(db, 'INSERT INTO raw.customer(raw_customer_id,batch_id,source_system_id,source_customer_id) VALUES (%s,%s,1,%s)', (own['raw'], second['batch'], 'shared'))
    columns = [r[0] for r in base.query(db, "SELECT attname FROM pg_attribute WHERE attrelid='staging.customer_profile_history'::regclass AND attnum>0 AND NOT attisdropped ORDER BY attnum")]
    projection = ','.join('%s::uuid' if c == 'batch_id' else '%s::text' if c == 'app_id' else 'p."' + c + '"' for c in columns)
    base.query(db, f'INSERT INTO staging.customer_profile_history({",".join(chr(34)+c+chr(34) for c in columns)}) SELECT {projection} FROM staging.customer_profile p WHERE p.raw_customer_id=%s', (second['batch'], base.B, own['raw']))
    base.query(db, 'DELETE FROM identity.customer_entity_member WHERE standardized_record_id=%s', (own['standard'],))
    base.query(db, 'DELETE FROM staging.customer_profile WHERE standardized_record_id=%s', (own['standard'],))
    history = base.query(db, 'SELECT batch_id,app_id,standardized_record_id FROM staging.customer_profile_history ORDER BY batch_id')
    install(db)
    profiles = base.query(db, "SELECT app_id,standardized_record_id FROM staging.customer_profile WHERE source_customer_id='shared' ORDER BY app_id")
    assert [r[0] for r in profiles] == sorted([base.A,base.B])
    assert len({r[1] for r in profiles}) == 2
    assert all(r[1] != own['standard'] for r in profiles)
    assert base.query(db, 'SELECT batch_id,app_id,standardized_record_id FROM staging.customer_profile_history ORDER BY batch_id') == history


def test_null_app_manual_upload_history_stays_null_and_is_not_reconstructed(db):
    for batch_ref in ('legacy-manual-upload', 'legacy-manual-upload-2'):
        batch = base.batch(db, base.A, ref=batch_ref)
        own = base.member(db, batch, batch_ref)
        base.query(db, 'INSERT INTO staging.customer_profile_history SELECT * FROM staging.customer_profile WHERE raw_customer_id=%s', (own['raw'],))
        assert base.one(db, 'SELECT app_id FROM control.ingestion_batch WHERE batch_id=%s', (batch['batch'],)) is None
        assert base.one(db, 'SELECT app_id FROM staging.customer_profile_history WHERE batch_id=%s', (batch['batch'],)) is None
    install(db)
    assert base.one(db, 'SELECT count(*) FROM staging.customer_profile_history WHERE app_id IS NULL') == 2
    assert base.one(db, 'SELECT count(*) FROM identity.customer_entity_member WHERE app_id IS NULL') == 2
    assert base.one(db, 'SELECT count(*) FROM staging.customer_profile WHERE app_id IS NOT NULL') == 0


def install(conn):
    assert MIGRATION.is_file()
    base.query(conn, MIGRATION.read_text())
    conn.commit()


def upload(conn, app, source=1):
    return base.batch(conn, app, source=source)


def legacy_alias(conn):
    first = upload(conn, base.A)
    old = base.member(conn, first, 'shared', first='Alice')
    base.query(conn, 'INSERT INTO staging.customer_profile_history SELECT * FROM staging.customer_profile WHERE raw_customer_id=%s', (old['raw'],))
    second = upload(conn, base.B)
    base.query(conn, "UPDATE control.ingestion_batch SET created_at=created_at+interval '1 second' WHERE batch_id=%s", (second['batch'],))
    base.member(conn, second, 'shared', raw=old['raw'], entity=old['entity'], first='Bob')
    base.query(conn, 'INSERT INTO staging.customer_profile_history SELECT * FROM staging.customer_profile WHERE raw_customer_id=%s', (old['raw'],))
    return first, second, old


def published_active_alias(conn):
    """Published A ID, independent typed A/B IDs, and B's immutable match FK."""
    import test_connector_identity_publication as connector
    connector.install(conn)
    first = upload(conn, base.A)
    old = base.member(conn, first, 'shared', first='Alice')
    base.query(conn, 'INSERT INTO staging.customer_profile_history SELECT * FROM staging.customer_profile WHERE raw_customer_id=%s', (old['raw'],))
    import test_customer_concurrent_capture as overlap
    overlap.pin_matcher_snapshot(conn)
    assert base.finish(conn, first)['status'] == 'PENDING'
    base.query(conn, "UPDATE control.customer_identity_transfer SET status='PUBLISHED',published_at=clock_timestamp() WHERE transfer_id=%s", (first['run'],))
    # The typed observation is not the resolver's immutable published ID.
    typed_a = str(uuid4())
    base.query(conn, 'ALTER TABLE staging.customer_profile_history DISABLE TRIGGER customer_app_id_derive')
    base.query(conn, 'UPDATE staging.customer_profile_history SET standardized_record_id=%s WHERE batch_id=%s', (typed_a, first['batch']))
    base.query(conn, 'ALTER TABLE staging.customer_profile_history ENABLE TRIGGER customer_app_id_derive')
    second = upload(conn, base.B)
    base.query(conn, "UPDATE control.ingestion_batch SET created_at=created_at+interval '1 second' WHERE batch_id=%s", (second['batch'],))
    base.member(conn, second, 'shared', raw=old['raw'], entity=old['entity'], first='Bob')
    base.query(conn, 'INSERT INTO staging.customer_profile_history SELECT * FROM staging.customer_profile WHERE raw_customer_id=%s', (old['raw'],))
    typed_b = str(uuid4())
    base.query(conn, 'ALTER TABLE staging.customer_profile_history DISABLE TRIGGER customer_app_id_derive')
    base.query(conn, 'UPDATE staging.customer_profile_history SET standardized_record_id=%s WHERE batch_id=%s', (typed_b, second['batch']))
    base.query(conn, 'ALTER TABLE staging.customer_profile_history ENABLE TRIGGER customer_app_id_derive')
    anchor = base.member(conn, second, 'anchor')
    base.query(conn, 'INSERT INTO staging.customer_profile_history SELECT * FROM staging.customer_profile WHERE standardized_record_id=%s', (anchor['standard'],))
    left, right = sorted((old['standard'], anchor['standard']))
    base.query(conn, '''INSERT INTO identity.match_pair(run_id,standardized_record_id_l,standardized_record_id_r,
        match_probability,match_method) VALUES (%s,%s,%s,0.9,'deterministic')''', (second['run'],left,right))
    conn.commit()
    return first, second, old, (typed_a, typed_b), (left,right)


def test_published_historical_alias_preserves_public_id_and_active_match_fk(db):
    first, second, old, typed, pair = published_active_alias(db)
    exported = base.query(db, 'SELECT transfer_id,row_number,app_id,user_id,payload::text FROM control.customer_identity_export')
    bridge = base.query(db, 'SELECT * FROM control.customer_identity_member_export')
    pair_before = base.query(db, 'SELECT run_id,standardized_record_id_l,standardized_record_id_r FROM identity.match_pair')
    active_before = base.query(db, 'SELECT * FROM identity.customer_entity_member ORDER BY standardized_record_id')
    install(db)
    assert base.query(db, 'SELECT transfer_id,row_number,app_id,user_id,payload::text FROM control.customer_identity_export') == exported
    assert base.query(db, 'SELECT * FROM control.customer_identity_member_export') == bridge
    assert base.query(db, 'SELECT run_id,standardized_record_id_l,standardized_record_id_r FROM identity.match_pair') == pair_before
    assert [row[:-1] for row in base.query(db, 'SELECT * FROM identity.customer_entity_member ORDER BY standardized_record_id')] == active_before
    assigned = base.one(db, 'SELECT standardized_record_id FROM staging.customer_profile WHERE app_id=%s AND source_customer_id=%s', (base.A,'shared'))
    assert assigned not in (old['standard'], *typed)
    assert assigned == base.one(db, '''SELECT md5('050/customer-logical-member/v1:' ||
        length(app_id)::text || ':' || app_id || ':' || source_system_id::text || ':' ||
        length(source_customer_id)::text || ':' || source_customer_id || ':' ||
        batch_id::text || ':' || raw_customer_id::text)::uuid::text
        FROM staging.customer_profile WHERE standardized_record_id=%s''', (assigned,))
    assert base.one(db, 'SELECT app_id FROM identity.customer_entity_member WHERE standardized_record_id=%s', (old['standard'],)) == base.B
    assert base.one(db, 'SELECT status FROM control.customer_identity_transfer WHERE transfer_id=%s', (first['run'],)) == 'PUBLISHED'


@pytest.mark.parametrize('bridge', [False, True])
def test_same_owner_published_resolver_id_not_in_typed_history_is_preserved(db, bridge):
    if bridge:
        base.query(db, (ROOT / 'database/migrations/041_global_member_bridge.sql').read_text())
        db.commit()
    batch = upload(db, base.A)
    owner = base.member(db, batch, 'same-owner-resolver')
    base.query(db, 'INSERT INTO staging.customer_profile_history SELECT * FROM staging.customer_profile')
    assert base.finish(db, batch)['status'] == 'PENDING'
    typed_id = str(uuid4())
    base.query(db, 'ALTER TABLE staging.customer_profile_history DISABLE TRIGGER customer_app_id_derive')
    base.query(db, 'UPDATE staging.customer_profile_history SET standardized_record_id=%s WHERE batch_id=%s',
               (typed_id, batch['batch']))
    base.query(db, 'ALTER TABLE staging.customer_profile_history ENABLE TRIGGER customer_app_id_derive')
    before_export = base.query(db, 'SELECT payload::text FROM control.customer_identity_export')
    before_bridge = (base.query(db, 'SELECT standardized_record_id,raw_customer_id FROM control.customer_identity_member_export')
                     if bridge else None)
    db.commit()
    install(db)
    assert base.one(db, 'SELECT standardized_record_id FROM staging.customer_profile') == owner['standard']
    assert base.one(db, 'SELECT standardized_record_id FROM staging.customer_profile_history') == typed_id
    assert base.one(db, 'SELECT standardized_record_id FROM identity.customer_entity_member') == owner['standard']
    assert base.query(db, 'SELECT payload::text FROM control.customer_identity_export') == before_export
    if bridge:
        assert base.query(db, 'SELECT standardized_record_id,raw_customer_id FROM control.customer_identity_member_export') == before_bridge


def test_unpublished_transfer_cannot_authorize_cross_app_alias(db):
    first, _, _, _, _ = published_active_alias(db)
    base.query(db, "UPDATE control.customer_identity_transfer SET status='PENDING',published_at=NULL WHERE transfer_id=%s", (first['run'],))
    db.commit()
    with pytest.raises(psycopg2.Error, match='050.*published'):
        base.query(db, MIGRATION.read_text())
    db.rollback()
    assert base.one(db, "SELECT count(*) FROM pg_attribute WHERE attrelid='identity.customer_entity_member'::regclass AND attname='app_id' AND NOT attisdropped") == 0


@pytest.mark.parametrize('drift', ['source', 'missing_published_history', 'missing_active_history',
                                     'same_app_id', 'multiple_owners'])
def test_published_alias_requires_unambiguous_exact_provenance(db, drift):
    first, second, old, typed, _ = published_active_alias(db)
    if drift == 'source':
        base.query(db, 'ALTER TABLE control.customer_identity_member_export DISABLE TRIGGER customer_identity_member_export_immutable')
        base.query(db, 'UPDATE control.customer_identity_member_export SET source_system_id=999 WHERE transfer_id=%s', (first['run'],))
        base.query(db, 'ALTER TABLE control.customer_identity_member_export ENABLE TRIGGER customer_identity_member_export_immutable')
    elif drift in ('missing_published_history', 'missing_active_history'):
        batch = first if drift == 'missing_published_history' else second
        base.query(db, 'ALTER TABLE staging.customer_profile_history DISABLE TRIGGER ALL')
        base.query(db, 'DELETE FROM staging.customer_profile_history WHERE batch_id=%s AND source_customer_id=%s', (batch['batch'],'shared'))
        base.query(db, 'ALTER TABLE staging.customer_profile_history ENABLE TRIGGER ALL')
    elif drift == 'same_app_id':
        # A's typed ID now claims the released ID while B is active on it.
        base.query(db, 'ALTER TABLE staging.customer_profile_history DISABLE TRIGGER ALL')
        base.query(db, 'UPDATE staging.customer_profile_history SET standardized_record_id=%s WHERE batch_id=%s AND source_customer_id=%s',
                   (old['standard'], first['batch'], 'shared'))
        base.query(db, 'ALTER TABLE staging.customer_profile_history ENABLE TRIGGER ALL')
    else:
        base.query(db, 'ALTER TABLE control.customer_identity_export DISABLE TRIGGER customer_identity_export_immutable')
        base.query(db, 'UPDATE control.customer_identity_export SET app_id=%s,payload=jsonb_set(payload,\'{app_id}\',to_jsonb(%s::text)) WHERE transfer_id=%s',
                   (base.B,base.B,first['run']))
        base.query(db, 'ALTER TABLE control.customer_identity_export ENABLE TRIGGER customer_identity_export_immutable')
    db.commit()
    with pytest.raises(psycopg2.Error, match='050.*(?:published|legacy|historical)'):
        base.query(db, MIGRATION.read_text())
    db.rollback()
    assert base.one(db, "SELECT count(*) FROM pg_attribute WHERE attrelid='identity.customer_entity_member'::regclass AND attname='app_id' AND NOT attisdropped") == 0


def test_published_alias_integrates_038_050_051_052_053_and_future_capture(db):
    import test_customer_concurrent_capture as overlap
    first, second, old, _, _ = published_active_alias(db)
    exported = base.query(db, 'SELECT app_id,user_id,payload::text FROM control.customer_identity_export')
    # The core fixture starts at the 019 table shape but installs only 019's
    # capture body. Install its immutable triggers required by the real 053 gate.
    base.query(db, '''CREATE FUNCTION raw.reject_customer_history_mutation() RETURNS trigger
        LANGUAGE plpgsql AS $body$ BEGIN RAISE EXCEPTION 'Customer historical observations are immutable'; END $body$''')
    base.query(db, '''CREATE TRIGGER customer_history_immutable BEFORE UPDATE OR DELETE OR TRUNCATE
        ON raw.customer FOR EACH STATEMENT EXECUTE FUNCTION raw.reject_customer_history_mutation()''')
    base.query(db, '''CREATE TRIGGER customer_profile_history_immutable BEFORE UPDATE OR DELETE OR TRUNCATE
        ON staging.customer_profile_history FOR EACH STATEMENT EXECUTE FUNCTION raw.reject_customer_history_mutation()''')
    base.query(db, (ROOT / 'database/migrations/038_customer_forward_source_namespace.sql').read_text())
    db.commit()
    install(db)
    for name in ('051_customer_logical_capture.sql', '052_customer_ordinary_activation.sql',
                 '053_history_logical_fk.sql'):
        base.query(db, (ROOT / 'database/migrations' / name).read_text())
        db.commit()
    assert base.query(db, 'SELECT app_id,user_id,payload::text FROM control.customer_identity_export') == exported
    base.query(db, 'UPDATE identity.customer_entity_member SET resolution_run_id=%s WHERE standardized_record_id=%s', (second['run'],old['standard']))
    base.query(db, 'UPDATE identity.customer_entity SET last_resolution_run_id=%s,member_count=1 WHERE customer_entity_id=%s', (second['run'],old['entity']))
    overlap.pin_matcher_snapshot(db)
    transfer = base.finish(db, second)
    assert transfer['status'] == 'PENDING', transfer
    assert base.query(db, 'SELECT app_id,user_id,payload::text FROM control.customer_identity_export WHERE transfer_id=%s', (first['run'],)) == exported
    assert base.one(db, "SELECT payload->>'standardized_record_id' FROM control.customer_identity_export WHERE transfer_id=%s AND app_id=%s AND user_id='shared'", (second['run'],base.B)) == old['standard']


def test_existing_published_export_is_byte_stable_across_cutover(db):
    batch = upload(db, base.A)
    member = base.member(db, batch, 'published', first='Published')
    base.query(db, 'INSERT INTO staging.customer_profile_history SELECT * FROM staging.customer_profile WHERE raw_customer_id=%s', (member['raw'],))
    transfer = base.finish(db, batch)
    assert transfer['status'] == 'PENDING'
    before = base.query(db, '''SELECT transfer_id,row_number,app_id,user_id,payload::text
        FROM control.customer_identity_export ORDER BY transfer_id,row_number''')
    assert before
    install(db)
    assert base.query(db, '''SELECT transfer_id,row_number,app_id,user_id,payload::text
        FROM control.customer_identity_export ORDER BY transfer_id,row_number''') == before
    assert base.one(db, 'SELECT standardized_record_id FROM staging.customer_profile') == member['standard']
    assert base.query(db, 'SELECT standardized_record_id,customer_entity_id FROM identity.customer_entity_member') == [(member['standard'],member['entity'])]


def test_historical_alias_backfills_active_profiles_without_mutating_history_or_member_id(db):
    first, second, old = legacy_alias(db)
    history = base.query(db, '''SELECT batch_id,raw_customer_id,source_system_id,source_customer_id,
        app_id,first_name_raw,standardized_record_id FROM staging.customer_profile_history ORDER BY app_id''')
    exports = base.query(db, 'SELECT count(*) FROM control.customer_identity_export')
    install(db)
    assert base.query(db, '''SELECT batch_id,raw_customer_id,source_system_id,source_customer_id,
        app_id,first_name_raw,standardized_record_id FROM staging.customer_profile_history ORDER BY app_id''') == history
    assert base.query(db, 'SELECT count(*) FROM control.customer_identity_export') == exports
    profiles = base.query(db, '''SELECT app_id,raw_customer_id,standardized_record_id,first_name_raw
        FROM staging.customer_profile WHERE source_system_id=1 AND source_customer_id='shared' ORDER BY app_id''')
    assert len(profiles) == 2
    assert [(row[0], row[1], row[3]) for row in profiles] == [
        (base.A, old['raw'], 'Alice'), (base.B, old['raw'], 'Bob')]
    assert profiles[1][2] == old['standard']
    assert profiles[0][2] != old['standard']
    assert base.one(db, 'SELECT app_id FROM identity.customer_entity_member WHERE standardized_record_id=%s', (old['standard'],)) == base.B
    assert base.one(db, 'SELECT count(*) FROM identity.customer_entity_member') == 1


def test_active_profile_and_member_share_app_scoped_source_key_not_raw_uuid(db):
    first = upload(db, base.A)
    old = base.member(db, first, 'same')
    base.query(db, 'INSERT INTO staging.customer_profile_history SELECT * FROM staging.customer_profile WHERE raw_customer_id=%s', (old['raw'],))
    install(db)
    second = upload(db, base.B)
    new_id, new_entity = str(uuid4()), str(uuid4())
    base.query(db, '''INSERT INTO staging.customer_profile
        (standardized_record_id,raw_customer_id,batch_id,source_system_id,source_customer_id,first_name_raw)
        VALUES (%s,%s,%s,1,'same','Bob')''', (new_id, old['raw'], second['batch']))
    base.query(db, '''INSERT INTO identity.customer_entity(customer_entity_id,last_resolution_run_id)
        VALUES (%s,%s)''', (new_entity, second['run']))
    base.query(db, '''INSERT INTO identity.customer_entity_member
        (standardized_record_id,customer_entity_id,source_system_id,source_customer_id,match_method,resolution_run_id)
        VALUES (%s,%s,1,'same','singleton',%s)''', (new_id,new_entity,second['run']))
    assert base.query(db, '''SELECT p.app_id,m.app_id,m.standardized_record_id
        FROM staging.customer_profile p JOIN identity.customer_entity_member m USING(standardized_record_id)
        ORDER BY p.app_id''') == [(base.A,base.A,old['standard']), (base.B,base.B,new_id)]
    base.query(db, 'SAVEPOINT wrong_tuple')
    with pytest.raises(psycopg2.Error):
        base.query(db, "UPDATE identity.customer_entity_member SET app_id=%s WHERE standardized_record_id=%s", (base.B,old['standard']))
    base.query(db, 'ROLLBACK TO SAVEPOINT wrong_tuple')


def test_replaying_same_app_does_not_allocate_new_standardized_id(db):
    first = upload(db, base.A)
    old = base.member(db, first, 'same')
    base.query(db, 'INSERT INTO staging.customer_profile_history SELECT * FROM staging.customer_profile WHERE raw_customer_id=%s', (old['raw'],))
    install(db)
    assert base.one(db, 'SELECT count(*) FROM staging.customer_profile') == 1
    assert base.one(db, 'SELECT standardized_record_id FROM staging.customer_profile') == old['standard']
    assert base.one(db, 'SELECT count(*) FROM identity.customer_entity_member') == 1


def test_null_app_historical_upload_fails_closed(db):
    first = upload(db, base.A)
    old = base.member(db, first, 'same')
    base.query(db, 'INSERT INTO staging.customer_profile_history SELECT * FROM staging.customer_profile WHERE raw_customer_id=%s', (old['raw'],))
    # Emulate an incomplete 026 backfill on this historical row. The old app
    # must not be inferred from the current source/customer UUID.
    base.query(db, 'ALTER TABLE staging.customer_profile_history DISABLE TRIGGER customer_app_id_derive')
    base.query(db, 'UPDATE staging.customer_profile_history SET app_id=NULL WHERE batch_id=%s', (first['batch'],))
    base.query(db, 'ALTER TABLE staging.customer_profile_history ENABLE TRIGGER customer_app_id_derive')
    db.commit()
    with pytest.raises(psycopg2.Error, match='050.*(?:historical|legacy current)'):
        base.query(db, MIGRATION.read_text())
    db.rollback()
    assert base.one(db, "SELECT count(*) FROM pg_attribute WHERE attrelid='identity.customer_entity_member'::regclass AND attname='app_id'") == 0



def test_reuse_same_app_source_key_with_new_raw_uuid_cannot_allocate_second_id(db):
    first = upload(db, base.A)
    old = base.member(db, first, 'same')
    base.query(db, 'INSERT INTO staging.customer_profile_history SELECT * FROM staging.customer_profile WHERE raw_customer_id=%s', (old['raw'],))
    install(db)
    second = upload(db, base.A)
    base.query(db, 'SAVEPOINT duplicate_source')
    with pytest.raises(psycopg2.errors.UniqueViolation):
        base.query(db, '''INSERT INTO staging.customer_profile
            (raw_customer_id,batch_id,source_system_id,source_customer_id)
            VALUES (%s,%s,1,'same')''', (str(uuid4()),second['batch']))
    base.query(db, 'ROLLBACK TO SAVEPOINT duplicate_source')
    assert base.one(db, 'SELECT standardized_record_id FROM staging.customer_profile') == old['standard']


def test_legacy_connector_member_remains_unique_for_null_app(db):
    first = upload(db, base.A)
    old = base.member(db, first, 'same')
    base.query(db, 'INSERT INTO staging.customer_profile_history SELECT * FROM staging.customer_profile WHERE raw_customer_id=%s', (old['raw'],))
    install(db)
    # NULL-app members retain the old source-only uniqueness rather than
    # dropping global connector admission in the partial app index cutover.
    indexes = base.query(db, '''SELECT indexname FROM pg_indexes
        WHERE schemaname='identity' AND tablename='customer_entity_member'
          AND indexname='customer_entity_member_connector_source_key' ''')
    assert indexes == [('customer_entity_member_connector_source_key',)]
    assert base.one(db, 'SELECT app_id FROM identity.customer_entity_member WHERE standardized_record_id=%s', (old['standard'],)) == base.A


def test_unexpected_raw_unique_index_drift_fails_without_mutating_schema(db):
    first = upload(db, base.A)
    old = base.member(db, first, 'same')
    base.query(db, 'INSERT INTO staging.customer_profile_history SELECT * FROM staging.customer_profile WHERE raw_customer_id=%s', (old['raw'],))
    base.query(db, 'DROP INDEX staging.uq_customer_profile_raw_customer')
    db.commit()
    with pytest.raises(psycopg2.Error, match='050.*catalog'):
        base.query(db, MIGRATION.read_text())
    db.rollback()
    assert base.one(db, "SELECT count(*) FROM pg_attribute WHERE attrelid='identity.customer_entity_member'::regclass AND attname='app_id'") == 0


def test_historical_unique_standardized_id_is_reused_when_safe(db):
    a = upload(db, base.A)
    old = base.member(db, a, 'shared', first='Alice')
    base.query(db, 'INSERT INTO staging.customer_profile_history SELECT * FROM staging.customer_profile WHERE raw_customer_id=%s', (old['raw'],))
    b = upload(db, base.B)
    base.query(db, "UPDATE control.ingestion_batch SET created_at=created_at+interval '1 second' WHERE batch_id=%s", (b['batch'],))
    # An unpublished legacy replacement profile moved the former app's ID
    # off the active table; its historical ID must survive the cutover.
    base.query(db, 'DELETE FROM identity.customer_entity_member WHERE standardized_record_id=%s', (old['standard'],))
    base.query(db, 'UPDATE staging.customer_profile SET standardized_record_id=%s WHERE raw_customer_id=%s', (str(uuid4()), old['raw']))
    base.query(db, "UPDATE raw.customer_current SET batch_id=%s,first_name='Bob' WHERE raw_customer_id=%s", (b['batch'], old['raw']))
    base.query(db, "UPDATE staging.customer_profile SET batch_id=%s,first_name_raw='Bob' WHERE raw_customer_id=%s", (b['batch'], old['raw']))
    base.query(db, 'INSERT INTO staging.customer_profile_history SELECT * FROM staging.customer_profile WHERE raw_customer_id=%s', (old['raw'],))
    install(db)
    assert base.one(db, 'SELECT standardized_record_id FROM staging.customer_profile WHERE app_id=%s', (base.A,)) == old['standard']


def test_038_chain_keeps_forward_stitch_gate_closed_after_050(db):
    first, second, old = legacy_alias(db)
    db.commit()
    base.query(db, (ROOT / 'database/migrations/038_customer_forward_source_namespace.sql').read_text())
    db.commit()
    install(db)
    gate = base.one(db, '''SELECT count(*) FROM pg_trigger
        WHERE tgrelid='control.stitching_job'::regclass
          AND tgname='customer_forward_stitch_gate' AND tgenabled='O' ''')
    assert gate == 1
    assert base.one(db, 'SELECT standardized_record_id FROM staging.customer_profile WHERE app_id=%s', (base.B,)) == old['standard']
    assert base.one(db, 'SELECT count(*) FROM staging.customer_profile WHERE app_id=%s', (base.A,)) == 1


def test_ambiguous_legacy_tuple_fails_atomically_without_changing_active_schema(db):
    first = upload(db, base.A)
    old = base.member(db, first, 'shared')
    second = upload(db, base.B)
    base.query(db, 'INSERT INTO staging.customer_profile_history SELECT * FROM staging.customer_profile WHERE raw_customer_id=%s', (old['raw'],))
    # Wrong legacy member identity is a concrete ambiguity: no normalized
    # profile for the previously published member's source tuple.
    base.query(db, "UPDATE identity.customer_entity_member SET source_customer_id='other' WHERE standardized_record_id=%s", (old['standard'],))
    db.commit()
    with pytest.raises(psycopg2.Error, match='050.*legacy|050.*ambiguous'):
        base.query(db, MIGRATION.read_text())
    db.rollback()
    assert base.one(db, "SELECT count(*) FROM pg_attribute WHERE attrelid='identity.customer_entity_member'::regclass AND attname='app_id'") == 0
    assert base.one(db, 'SELECT count(*) FROM staging.customer_profile') == 1
