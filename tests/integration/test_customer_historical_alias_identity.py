"""Red contract: distinct app source identities must survive an old shared raw UUID.

Real disposable PostgreSQL fixture on localhost:55441; no staging writes.
The existing capture guard must stay in place until this contract and the
end-to-end matcher/publisher proof both pass.
"""
import pytest
import test_customer_concurrent_capture as capture
import test_customer_global_upload_capture as fixture
from datetime import datetime, timezone
import sys

sys.path.insert(0, str(fixture.STITCHER / 'src'))


db = fixture.db


@pytest.mark.xfail(strict=True, reason='legacy global raw UUID collapses two app origins into one matcher member')
def test_shared_legacy_raw_uuid_has_two_source_members(db):
    capture.install(db)
    older = fixture.batch(db, fixture.A)
    first = fixture.member(db, older, 'shared-source-user', first='Older')
    fixture.query(db, 'INSERT INTO staging.customer_profile_history '
                      'SELECT * FROM staging.customer_profile WHERE raw_customer_id=%s',
                  (first['raw'],))
    newer = fixture.batch(db, fixture.B)
    second = fixture.member(db, newer, 'shared-source-user', raw=first['raw'],
                            entity=first['entity'], first='Newer')
    fixture.query(db, 'INSERT INTO staging.customer_profile_history '
                      'SELECT * FROM staging.customer_profile WHERE raw_customer_id=%s',
                  (second['raw'],))
    history = fixture.query(db, 'SELECT DISTINCT app_id FROM staging.customer_profile_history '
                                'WHERE raw_customer_id=%s', (first['raw'],))
    assert history is not None
    assert {row[0] for row in history} == {fixture.A, fixture.B}
    source_members = fixture.query(db, '''SELECT p.app_id,m.standardized_record_id::text
        FROM identity.customer_entity_member m
        JOIN staging.customer_profile p USING(standardized_record_id)
        WHERE p.raw_customer_id=%s''', (first['raw'],))
    assert source_members is not None
    assert {row[0] for row in source_members} == {fixture.A, fixture.B}
    assert len({row[1] for row in source_members}) == 2


def test_historical_global_frame_keeps_two_origins_of_one_raw_uuid(db):
    """Use immutable batch/source observations, never the overwritten current profile."""
    from identity_stitcher.config import AppConfig, DatabaseConfig
    from identity_stitcher.historical_global import build_frame
    from identity_stitcher.linkage import resolve_frame

    first = fixture.batch(db, fixture.A)
    old = fixture.member(db, first, 'same-user', first='Older', email='older@example.com')
    fixture.query(db, 'INSERT INTO staging.customer_profile_history '
                      'SELECT * FROM staging.customer_profile WHERE raw_customer_id=%s', (old['raw'],))
    second = fixture.batch(db, fixture.B)
    fixture.query(db, "UPDATE control.ingestion_batch SET created_at=created_at+interval '1 second' WHERE batch_id=%s", (second['batch'],))
    fixture.member(db, second, 'same-user', raw=old['raw'], entity=old['entity'],
                   first='Newer', email='newer@example.com')
    fixture.query(db, 'INSERT INTO staging.customer_profile_history '
                      'SELECT * FROM staging.customer_profile WHERE raw_customer_id=%s', (old['raw'],))
    with db.cursor() as cur:
        cur.execute('''SELECT h.* FROM raw.customer h
            JOIN raw.customer_batch_member m
              ON (m.ingestion_batch_id,m.raw_customer_id,m.source_system_id,m.source_customer_id)=
                 (h.batch_id,h.raw_customer_id,h.source_system_id,h.source_customer_id)
            JOIN staging.customer_profile_history s
              ON (s.batch_id,s.raw_customer_id,s.app_id,s.source_system_id,s.source_customer_id)=
                 (h.batch_id,h.raw_customer_id,h.app_id,h.source_system_id,h.source_customer_id)
            ORDER BY h.app_id''')
        columns = [d.name for d in cur.description]
        observations = [dict(zip(columns, row)) for row in cur.fetchall()]
    config = AppConfig(DatabaseConfig('invalid', 1, 'invalid', 'invalid', 'invalid'),
                       max_random_training_pairs=100, duckdb_memory_limit='256MB')
    frame, origins = build_frame(observations, config, datetime(2026, 9, 28, tzinfo=timezone.utc))
    assert len(frame) == len(origins) == 2
    assert len(set(frame.unique_id)) == 2
    assert set(frame.raw_customer_id) == {old['raw']}
    assert {item['app_id'] for item in origins.values()} == {fixture.A, fixture.B}
    assert {item['batch_id'] for item in origins.values()} == {first['batch'], second['batch']}
    assert set(frame.first_name_raw) == {'Older', 'Newer'}
    result = resolve_frame(frame, config)
    assert result.entity_count == 2  # UUID collision alone is not a person match.


def test_verified_observations_are_exact_batch_history_not_mutable_current(db):
    from identity_stitcher.historical_global import load_verified_observations, build_frame
    from identity_stitcher.config import AppConfig, DatabaseConfig
    from identity_stitcher.linkage import resolve_frame

    old = fixture.batch(db, fixture.A)
    source = fixture.member(db, old, 'source-user', first='Before', email='shared@example.com')
    fixture.query(db, "UPDATE staging.customer_profile SET standardization_status='READY', normalization_version='v1' WHERE raw_customer_id=%s", (source['raw'],))
    fixture.query(db, 'INSERT INTO staging.customer_profile_history '
                      'SELECT * FROM staging.customer_profile WHERE raw_customer_id=%s', (source['raw'],))
    new = fixture.batch(db, fixture.B)
    fixture.query(db, "UPDATE control.ingestion_batch SET created_at=created_at+interval '1 second' WHERE batch_id=%s", (new['batch'],))
    fixture.member(db, new, 'source-user', raw=source['raw'], entity=source['entity'],
                   first='After', email='shared@example.com')
    fixture.query(db, "UPDATE staging.customer_profile SET standardization_status='READY', normalization_version='v1' WHERE raw_customer_id=%s", (source['raw'],))
    fixture.query(db, 'INSERT INTO staging.customer_profile_history '
                      'SELECT * FROM staging.customer_profile WHERE raw_customer_id=%s', (source['raw'],))
    db.commit()
    db.set_session(isolation_level='REPEATABLE READ', readonly=True)
    rows = load_verified_observations(db, (fixture.A, fixture.B), max_members=2)
    assert len(rows) == 2
    assert {(r['app_id'], r['first_name']) for r in rows} == {
        (fixture.A, 'Before'), (fixture.B, 'After')}
    cfg = AppConfig(DatabaseConfig('invalid', 1, 'invalid', 'invalid', 'invalid'),
                    max_random_training_pairs=100, duckdb_memory_limit='256MB')
    frame, origins = build_frame(rows, cfg, datetime(2026, 9, 28, tzinfo=timezone.utc))
    assert len(origins) == 2
    result = resolve_frame(frame, cfg)
    assert result.entity_count == 2  # shared raw UUID and email alone are not sufficient.


def test_verified_observations_refuse_missing_normalized_history(db):
    from identity_stitcher.historical_global import load_verified_observations
    from identity_stitcher.app_backfill import BackfillError
    old = fixture.batch(db, fixture.A)
    fixture.member(db, old, 'missing-normalization', first='Before')
    db.commit()
    db.set_session(isolation_level='REPEATABLE READ', readonly=True)
    with pytest.raises(BackfillError, match='INCOMPLETE_TYPED_HISTORY'):
        load_verified_observations(db, (fixture.A,), max_members=2)
