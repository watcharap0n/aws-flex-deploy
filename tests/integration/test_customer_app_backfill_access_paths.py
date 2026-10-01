"""028 access-path regression, using only the owned loopback fixture from 027.

Fast contract tests run normally. Full stale-statistics regression is opt-in:
BACKFILL_ACCESS_PATHS_BENCHMARK=1 python -m pytest <this file> -k stale -s
Set BACKFILL_ACCESS_PATHS_RESULTS to retain its machine-readable measurements.
No live clients or customer data; no ANALYZE after the new ledger inserts.
"""
import hashlib
import json
import os
from pathlib import Path
import time
from typing import Any

import psycopg2
import pytest

import test_customer_app_backfill as base
from test_customer_app_backfill import db, owned_database  # noqa: F401

MIGRATION = base.MIGRATIONS / '028_customer_app_backfill_access_paths.sql'
FINALIZER = 'control.finalize_customer_app_backfill()'
ORIGINAL_SHA256 = 'ac15f5fca9482a71fa6e84b0a0bb9a049501991b2d4f032ebc474e1d44c01e0a'
ORIGINAL_PROSRC_MD5 = 'b53dd3139ebdcb2dc0ca038b8d4d1381'
q = base.query


def install_sql(c, text):
    c.commit()
    c.autocommit = True
    try:
        q(c, text)
    except psycopg2.Error:
        q(c, 'ROLLBACK')
        raise
    finally:
        c.autocommit = False


def function_source(migration):
    return migration.read_text().split('FUNCTION ' + FINALIZER, 1)[1].split('AS $$', 1)[1].split('$$;', 1)[0]


def catalog_contract(c):
    """Only the finalizer's prosrc may change, not its identity/ACL or other objects."""
    return {
        'functions': q(c, '''SELECT CASE WHEN p.oid=%s::regprocedure THEN to_jsonb(p)-'prosrc'
            ELSE to_jsonb(p) END FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
            WHERE n.nspname IN ('control','raw','staging','identity') ORDER BY p.oid''', (FINALIZER,)),
        'relations': q(c, '''SELECT to_jsonb(c) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname IN ('control','raw','staging','identity') ORDER BY c.oid'''),
        'triggers': q(c, '''SELECT to_jsonb(t) FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid
            JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname IN ('control','raw','staging','identity') ORDER BY t.oid'''),
        'constraints': q(c, '''SELECT to_jsonb(c) FROM pg_constraint c JOIN pg_namespace n ON n.oid=c.connamespace
            WHERE n.nspname IN ('control','raw','staging','identity') ORDER BY c.oid'''),
        'indexes': q(c, '''SELECT to_jsonb(i) FROM pg_index i JOIN pg_class c ON c.oid=i.indexrelid
            JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE n.nspname IN ('control','raw','staging','identity') ORDER BY i.indexrelid'''),
    }


def test_migration_is_idempotent_and_preserves_catalog_contract(db):
    assert hashlib.sha256(base.MIGRATION.read_bytes()).hexdigest() == ORIGINAL_SHA256
    install_sql(db, base.MIGRATION.read_text())
    original = q(db, 'SELECT prosrc,md5(prosrc) FROM pg_proc WHERE oid=%s::regprocedure', (FINALIZER,))[0]
    assert original == (function_source(base.MIGRATION), ORIGINAL_PROSRC_MD5)
    before = catalog_contract(db)
    install_sql(db, MIGRATION.read_text())
    assert catalog_contract(db) == before
    installed = q(db, 'SELECT prosrc,md5(prosrc) FROM pg_proc WHERE oid=%s::regprocedure', (FINALIZER,))[0]
    assert installed[0] == function_source(MIGRATION)
    assert installed[1] != ORIGINAL_PROSRC_MD5
    install_sql(db, MIGRATION.read_text())
    assert catalog_contract(db) == before
    assert q(db, 'SELECT prosrc,md5(prosrc) FROM pg_proc WHERE oid=%s::regprocedure', (FINALIZER,)) == [installed]


@pytest.mark.parametrize('drift', ['missing', 'body', 'security', 'search_path'])
def test_precondition_rejects_unknown_finalizer_without_replacing_it(db, drift):
    if drift != 'missing':
        install_sql(db, base.MIGRATION.read_text())
        if drift == 'body':
            definition = q(db, 'SELECT pg_get_functiondef(%s::regprocedure)', (FINALIZER,))[0][0]
            q(db, definition.replace('DECLARE r identity.resolution_run%ROWTYPE;', '-- unreviewed drift\nDECLARE r identity.resolution_run%ROWTYPE;'))
        elif drift == 'security':
            q(db, 'ALTER FUNCTION control.finalize_customer_app_backfill() SECURITY INVOKER')
        else:
            q(db, 'ALTER FUNCTION control.finalize_customer_app_backfill() SET search_path=public')
    db.commit()
    before = q(db, 'SELECT to_jsonb(p) FROM pg_proc p WHERE oid=to_regprocedure(%s)', (FINALIZER,))
    with pytest.raises(psycopg2.Error, match='028 requires the exact 027 or 028'):
        install_sql(db, MIGRATION.read_text())
    assert q(db, 'SELECT to_jsonb(p) FROM pg_proc p WHERE oid=to_regprocedure(%s)', (FINALIZER,)) == before


def test_only_the_two_proven_validation_blocks_change():
    old = function_source(base.MIGRATION)
    new = function_source(MIGRATION)
    # Slice at invariant boundary lines, then prove all other body bytes match.
    winner_start = '        OR EXISTS(SELECT FROM winners w LEFT JOIN raw.customer h\n'
    winner_end = '        THEN -1 ELSE (SELECT count(*) FROM winners) END INTO n;'
    pair_start = '    IF EXISTS(SELECT FROM control.customer_app_backfill_pair p\n'
    pair_end = '        WHERE p.run_id=NEW.run_id AND (l.run_id IS NULL OR rhs.run_id IS NULL\n'
    def strip_blocks(body):
        for start, end in ((winner_start, winner_end), (pair_start, pair_end)):
            a, remainder = body.split(start, 1)
            _, b = remainder.split(end, 1)
            body = a + start + end + b
        return body
    assert strip_blocks(old) == strip_blocks(new)
    assert new.split(winner_start, 1)[1].split(winner_end, 1)[0] == '''            ON (h.batch_id,h.raw_customer_id,h.source_system_id,h.source_customer_id)=
                (w.ingestion_batch_id,w.raw_customer_id,w.source_system_id,w.source_customer_id)
            WHERE h.raw_customer_id IS NULL OR h.app_id IS DISTINCT FROM NEW.app_id)
        OR EXISTS(SELECT ingestion_batch_id,raw_customer_id,source_system_id,source_customer_id FROM winners
            EXCEPT SELECT source_batch_id,raw_customer_id,source_system_id,user_id
            FROM control.customer_app_backfill_record WHERE run_id=NEW.run_id)
'''
    assert new.split(pair_start, 1)[1].split(pair_end, 1)[0] == '''        LEFT JOIN LATERAL (
            SELECT x.run_id,x.payload FROM control.customer_app_backfill_record x
            WHERE x.run_id=p.run_id AND x.input_payload->>'record_id'=p.left_record_id::text
            LIMIT 1
        ) l ON true
        LEFT JOIN LATERAL (
            SELECT x.run_id,x.payload FROM control.customer_app_backfill_record x
            WHERE x.run_id=p.run_id AND x.input_payload->>'record_id'=p.right_record_id::text
            LIMIT 1
        ) rhs ON true
'''


@pytest.mark.parametrize('problem', ['source_batch_id', 'raw_customer_id', 'source_system_id', 'user_id', 'missing_record'])
def test_exact_winner_tuple_is_required_with_atomic_rollback(db, problem):
    base.install(db)
    older = base.batch(db)
    raw = base.upload(db, older)
    newer = base.batch(db, created='2026-09-02T00:00:00Z')
    base.upload(db, newer)
    run, workspace = base.prepare(db, [older, newer])
    if problem != 'missing_record':
        base.record(db, run, workspace, older if problem == 'source_batch_id' else newer,
                    str(base.uuid4()) if problem == 'raw_customer_id' else raw,
                    source=2 if problem == 'source_system_id' else 1,
                    user='wrong-user' if problem == 'user_id' else 'same-user')
    before = base.current_state(db)
    db.commit()
    with pytest.raises(psycopg2.Error, match='BACKFILL_LATEST_INPUT_INCOMPLETE'):
        base.finish(db, run)
    db.rollback()
    assert q(db, 'SELECT status FROM control.customer_app_backfill_run WHERE run_id=%s', (run,)) == [('BUILDING',)]
    assert q(db, 'SELECT status FROM identity.resolution_run WHERE run_id=%s', (run,)) == [('RUNNING',)]
    assert q(db, 'SELECT count(*) FROM control.customer_identity_transfer') == [(0,)]
    assert q(db, 'SELECT count(*) FROM control.customer_identity_export') == [(0,)]
    assert base.current_state(db) == before


@pytest.mark.parametrize('endpoint', ['left_record_id', 'right_record_id'])
def test_pair_endpoint_must_exist_in_same_run_with_atomic_rollback(db, endpoint):
    base.install(db)
    batch = base.batch(db)
    raw = base.upload(db, batch)
    run, workspace = base.prepare(db, [batch])
    other_run, _ = base.prepare(db, [batch])
    local_id = '80000000-0000-0000-0000-000000000000'
    remote_id = ('10000000-0000-0000-0000-000000000000' if endpoint == 'left_record_id'
                 else 'f0000000-0000-0000-0000-000000000000')
    base.record(db, run, workspace, batch, raw, record_id=local_id)
    base.record(db, other_run, workspace, batch, raw, record_id=remote_id)
    left, right = sorted([local_id, remote_id])
    q(db, '''INSERT INTO control.customer_app_backfill_pair
        (run_id,left_record_id,right_record_id,probability,method,evidence)
        VALUES(%s,%s,%s,1.0,'synthetic','{}')''', (run, left, right))
    q(db, 'UPDATE identity.resolution_run SET accepted_pair_count=1 WHERE run_id=%s', (run,))
    before = base.current_state(db)
    db.commit()
    with pytest.raises(psycopg2.Error, match='BACKFILL_INVALID_PAIR'):
        base.finish(db, run)
    db.rollback()
    assert q(db, 'SELECT status FROM control.customer_app_backfill_run WHERE run_id=%s', (run,)) == [('BUILDING',)]
    assert q(db, 'SELECT status FROM identity.resolution_run WHERE run_id=%s', (run,)) == [('RUNNING',)]
    assert q(db, 'SELECT count(*) FROM control.customer_identity_transfer') == [(0,)]
    assert q(db, 'SELECT count(*) FROM control.customer_identity_export') == [(0,)]
    assert base.current_state(db) == before


def current_fingerprint(c):
    # Full-row, order-independent digest and count, not samples; bounded client memory.
    return {table: q(c, f'''SELECT count(*),md5(string_agg(row_text, E'\\n' ORDER BY row_text COLLATE "C"))
        FROM (SELECT md5(to_jsonb(t)::text) row_text FROM {table} t) rows''') for table in (
            'raw.customer_current', 'staging.customer_profile', 'identity.customer_entity',
            'identity.customer_entity_member', 'identity.match_pair', 'control.stitching_job')}


def capture_stale_plans(c, run, users, source_md5):
    body = q(c, 'SELECT prosrc FROM pg_proc WHERE oid=%s::regprocedure', (FINALIZER,))[0][0]
    with c.cursor() as cursor:
        literal_run = cursor.mogrify('%s::uuid', (run,)).decode()
    body = body.replace('NEW.run_id', literal_run).replace('NEW.app_id', "'3308'::text").replace('NEW.expected_users', str(users))
    winners = 'WITH keys AS MATERIALIZED (' + body.split('WITH keys AS MATERIALIZED (', 1)[1].split('    IF n<0', 1)[0].replace(' END INTO n;', ' END;')
    pairs = 'SELECT EXISTS(SELECT FROM control.customer_app_backfill_pair p' + body.split('IF EXISTS(SELECT FROM control.customer_app_backfill_pair p', 1)[1].split(") THEN\n        RAISE EXCEPTION 'BACKFILL_INVALID_PAIR'", 1)[0] + ')'
    plans = {name: q(c, 'EXPLAIN (FORMAT JSON) ' + query)[0][0] for name, query in (('winners', winners), ('pairs', pairs))}
    def nodes(plan):
        yield plan
        for child in plan.get('Plans', []):
            yield from nodes(child)
    if source_md5 != ORIGINAL_PROSRC_MD5:
        assert any(n['Node Type'] == 'SetOp' and n.get('Command') == 'Except' for n in nodes(plans['winners'][0]['Plan']))
        endpoints = [n for n in nodes(plans['pairs'][0]['Plan'])
                     if n.get('Index Name') == 'customer_app_backfill_record_internal_id']
        assert len(endpoints) == 2
        for endpoint, name in zip(endpoints, ('left_record_id', 'right_record_id')):
            assert 'run_id = p.run_id' in endpoint['Index Cond']
            assert 'record_id' in endpoint['Index Cond'] and name in endpoint['Index Cond']
    if os.environ.get('BACKFILL_ACCESS_PATHS_RESULTS'):
        path = Path(os.environ['BACKFILL_ACCESS_PATHS_RESULTS']).parent / f'plans_{users}_{source_md5}.json'
        path.write_text(json.dumps(plans, indent=2) + '\n')
    return plans


def insert_synthetic_records(c, run, workspace, app, batch, merged_users):
    """Real immutable ledger inserts, with a bounded connected entity group."""
    q(c, """INSERT INTO control.customer_app_backfill_record
        (run_id,row_number,app_id,source_system_id,user_id,raw_customer_id,source_batch_id,input_payload,payload)
        SELECT %s,i,%s,source_system_id,source_customer_id,raw_customer_id,batch_id,
        jsonb_build_object('record_id',md5('record-'||i::text)::uuid,
            'raw',jsonb_build_object('raw_customer_id',raw_customer_id,'batch_id',batch_id,
                'source_system_id',source_system_id,'source_customer_id',source_customer_id,'app_id',%s),
            'normalized','{}'::jsonb),
        jsonb_build_object('customer_entity_id',md5('entity-'||CASE WHEN i<=%s THEN '1' ELSE i::text END)::uuid,
            'match_probability',1.0,'match_method','synthetic','source_system_id',source_system_id,
            'standardized_record_id',NULL,'raw_customer_id',raw_customer_id,'resolution_run_id',%s,
            'workspace_id',%s,'app_id',%s,'user_id',source_customer_id,
            'profile',jsonb_build_object('customer_id',source_customer_id))
        FROM (SELECT h.*,row_number() OVER(ORDER BY source_customer_id COLLATE "C") i
            FROM raw.customer h WHERE batch_id=%s) h""",
      (run, app, app, merged_users, run, workspace, app, batch))


def seed_stale_run(c, users=37128, entities=33042, pairs=45018, memberships=147762):
    """175 committed analyzed ledger rows, then an unseen uncommitted run."""
    assert users >= entities > 0 and memberships >= users
    q(c, "SET statement_timeout='120s'; SET jit=off; SET work_mem='4MB'")
    seed_batch = base.batch(c, app='99001')
    q(c, """INSERT INTO raw.customer_current(batch_id,source_system_id,source_customer_id)
        SELECT %s,1,'seed-'||lpad(i::text,6,'0') FROM generate_series(1,175) i""", (seed_batch,))
    seed_run, seed_workspace = base.prepare(c, [seed_batch], app='99001', expected=175)
    insert_synthetic_records(c, seed_run, seed_workspace, '99001', seed_batch, 1)
    base.finish(c, seed_run)
    q(c, """UPDATE control.customer_identity_transfer SET status='PUBLISHED',published_at=clock_timestamp()
        WHERE transfer_id=%s""", (seed_run,))
    c.commit()
    q(c, 'ANALYZE control.customer_app_backfill_record')
    c.commit()
    seed_snapshot = q(c, 'SELECT to_jsonb(t) FROM control.customer_identity_transfer t WHERE transfer_id=%s', (seed_run,))
    seed_exports = q(c, 'SELECT to_jsonb(t) FROM control.customer_identity_export t WHERE transfer_id=%s ORDER BY row_number', (seed_run,))
    batches = [base.batch(c, app='3308', created=f'2026-09-{i:02d}T00:00:00Z') for i in range(1, 11)]
    q(c, """INSERT INTO raw.customer_current(batch_id,source_system_id,source_customer_id)
        SELECT %s,1,'test-'||lpad(i::text,6,'0') FROM generate_series(1,%s) i""", (batches[-1], users))
    older = memberships - users
    for index, batch in enumerate(batches[:-1]):
        count = older // 9 + (index < older % 9)
        assert count <= users
        q(c, """INSERT INTO raw.customer_batch_member
            (ingestion_batch_id,raw_customer_id,source_system_id,source_customer_id)
            SELECT %s,raw_customer_id,source_system_id,source_customer_id FROM raw.customer_current
            WHERE batch_id=%s ORDER BY source_customer_id LIMIT %s""", (batch, batches[-1], count))
        q(c, """INSERT INTO raw.customer(batch_id,raw_customer_id,source_system_id,source_customer_id,app_id)
            SELECT m.ingestion_batch_id,m.raw_customer_id,m.source_system_id,m.source_customer_id,'3308'
            FROM raw.customer_batch_member m WHERE m.ingestion_batch_id=%s""", (batch,))
    for table in ('raw.customer', 'raw.customer_batch_member', 'control.ingestion_batch'):
        q(c, 'ANALYZE ' + table)
    run, workspace = base.prepare(c, batches, app='3308', expected=users)
    c.commit()
    assert q(c, '''SELECT count(*),sum(expected_members),sum(available_observations)
        FROM control.customer_app_backfill_batch WHERE run_id=%s''', (run,)) == [(10, memberships, memberships)]
    stats = q(c, """SELECT reltuples::bigint FROM pg_class
        WHERE oid='control.customer_app_backfill_record'::regclass""")
    assert stats == [(175,)]
    before = current_fingerprint(c)
    merged_users = users - entities + 1
    insert_synthetic_records(c, run, workspace, '3308', batches[-1], merged_users)
    q(c, """INSERT INTO control.customer_app_backfill_pair
        (run_id,left_record_id,right_record_id,probability,method,evidence)
        SELECT %s,least(md5('record-'||i::text)::uuid,md5('record-'||(i+d)::text)::uuid),
            greatest(md5('record-'||i::text)::uuid,md5('record-'||(i+d)::text)::uuid),1.0,'synthetic','{}'
        FROM generate_series(1,%s) i CROSS JOIN generate_series(1,12) d
        WHERE i+d<=%s ORDER BY i,d LIMIT %s""", (run, merged_users - 1, merged_users, pairs))
    q(c, 'UPDATE identity.resolution_run SET accepted_pair_count=%s,entity_count=%s WHERE run_id=%s', (pairs, entities, run))
    assert q(c, """SELECT count(*),count(DISTINCT payload->>'customer_entity_id')
        FROM control.customer_app_backfill_record WHERE run_id=%s""", (run,)) == [(users, entities)]
    assert q(c, 'SELECT count(*) FROM control.customer_app_backfill_pair WHERE run_id=%s', (run,)) == [(pairs,)]
    assert q(c, "SELECT reltuples::bigint FROM pg_class WHERE oid='control.customer_app_backfill_record'::regclass") == stats
    return run, before, seed_run, seed_snapshot, seed_exports


def benchmark_finalization(c, *, users=37128, entities=33042, pairs=45018, memberships=147762):
    """Time the complete finalizer plus COMMIT, then verify durable exact state."""
    report: dict[str, Any] = dict(users=users, entities=entities, pairs=pairs, memberships=memberships,
                  statement_timeout='25s', analyzed_old_records=175, new_run_analyzed=False)
    report['server_version'] = q(c, 'SHOW server_version')[0][0]
    report['prosrc_md5'] = q(c, 'SELECT md5(prosrc) FROM pg_proc WHERE oid=%s::regprocedure', (FINALIZER,))[0][0]
    started = time.monotonic()
    run, before, seed_run, seed_snapshot, seed_exports = seed_stale_run(c, users, entities, pairs, memberships)
    report['setup_seconds'] = round(time.monotonic() - started, 3)
    capture_stale_plans(c, run, users, report['prosrc_md5'])
    report['stale_plans_verified'] = True
    assert q(c, '''SELECT count(*) FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid
        JOIN pg_namespace n ON n.oid=c.relnamespace
        WHERE n.nspname IN ('control','raw','staging','identity') AND t.tgenabled<>'O' ''') == [(0,)]
    assert q(c, '''SELECT count(*) FROM pg_constraint c JOIN pg_namespace n ON n.oid=c.connamespace
        WHERE n.nspname IN ('control','raw','staging','identity') AND NOT c.convalidated''') == [(0,)]
    report['all_triggers_and_constraints_enabled'] = True
    report['active_maintenance_before_finish'] = q(c, '''SELECT backend_type,wait_event_type,wait_event,query
        FROM pg_stat_activity WHERE datname=current_database() AND backend_type='autovacuum worker' ''')
    q(c, "SET LOCAL statement_timeout='25s'")
    started = time.monotonic()
    try:
        base.finish(c, run)
        report['finalizer_seconds'] = round(time.monotonic() - started, 3)
        c.commit()
        report['finalizer_and_commit_seconds'] = round(time.monotonic() - started, 3)
        assert q(c, 'SELECT status FROM control.customer_app_backfill_run WHERE run_id=%s', (run,)) == [('SUCCEEDED',)]
        assert q(c, '''SELECT status,source_record_count,selected_record_count,standardized_record_count,
            entity_count,accepted_pair_count,completed_at IS NOT NULL
            FROM identity.resolution_run WHERE run_id=%s''', (run,)) == [('SUCCEEDED', users, users, users, entities, pairs, True)]
        assert q(c, '''SELECT transfer_id=backfill_run_id,transfer_id=resolution_run_id,status,row_count,app_id,
            stitching_job_id,ingestion_batch_id FROM control.customer_identity_transfer WHERE transfer_id=%s''',
            (run,)) == [(True, True, 'PENDING', users, '3308', None, None)]
        assert q(c, 'SELECT count(*) FROM control.customer_identity_export WHERE transfer_id=%s', (run,)) == [(users,)]
        assert q(c, '''SELECT count(*) FROM (
            (SELECT row_number,app_id,user_id,payload FROM control.customer_identity_export WHERE transfer_id=%s
             EXCEPT ALL SELECT row_number,app_id,user_id,payload FROM control.customer_app_backfill_record WHERE run_id=%s)
            UNION ALL
            (SELECT row_number,app_id,user_id,payload FROM control.customer_app_backfill_record WHERE run_id=%s
             EXCEPT ALL SELECT row_number,app_id,user_id,payload FROM control.customer_identity_export WHERE transfer_id=%s)
            ) difference''', (run, run, run, run)) == [(0,)]
        assert q(c, 'SELECT count(*) FROM control.customer_app_backfill_pair WHERE run_id=%s', (run,)) == [(pairs,)]
        assert current_fingerprint(c) == before
        assert q(c, 'SELECT to_jsonb(t) FROM control.customer_identity_transfer t WHERE transfer_id=%s', (seed_run,)) == seed_snapshot
        assert q(c, 'SELECT to_jsonb(t) FROM control.customer_identity_export t WHERE transfer_id=%s ORDER BY row_number', (seed_run,)) == seed_exports
        report.update(status='SUCCEEDED', committed=True, export_rows=users, outbox_row_count=users,
                      export_exact=True, current_unchanged=True, seed_175_published_unchanged=True)
        return report
    except psycopg2.Error as error:
        report.update(status='FAILED', sqlstate=error.pgcode,
                      finalizer_seconds=round(time.monotonic() - started, 3))
        c.rollback()
        assert q(c, 'SELECT status FROM control.customer_app_backfill_run WHERE run_id=%s', (run,)) == [('BUILDING',)]
        assert q(c, 'SELECT status FROM identity.resolution_run WHERE run_id=%s', (run,)) == [('RUNNING',)]
        for table, key in (('customer_app_backfill_record', 'run_id'), ('customer_app_backfill_pair', 'run_id'),
                           ('customer_identity_transfer', 'transfer_id'), ('customer_identity_export', 'transfer_id')):
            assert q(c, f'SELECT count(*) FROM control.{table} WHERE {key}=%s', (run,)) == [(0,)]
        assert current_fingerprint(c) == before
        report['rollback_verified'] = True
        raise
    finally:
        print(json.dumps(report, sort_keys=True), flush=True)
        if os.environ.get('BACKFILL_ACCESS_PATHS_RESULTS'):
            path = Path(os.environ['BACKFILL_ACCESS_PATHS_RESULTS'])
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open('a') as output:
                output.write(json.dumps(report, sort_keys=True) + '\n')


@pytest.mark.skipif(os.environ.get('BACKFILL_ACCESS_PATHS_BENCHMARK') != '1',
                    reason='opt-in full 37,128-user stale-statistics finalization')
def test_full_stale_statistics_finalization_at_failed_app_grain(db):
    base.install(db)
    report = benchmark_finalization(db)
    assert report['finalizer_seconds'] < 25
