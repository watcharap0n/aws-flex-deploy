"""027: real PostgreSQL, a fresh task-owned database on loopback:61577 only.

Run with ../fargate-user-stitching/.venv/bin/python -m pytest <this file>.
No production clients; refuses preexisting databases and never resets shared DBs.
"""
from contextlib import closing
from pathlib import Path
from uuid import uuid4

import psycopg2
from psycopg2 import sql
from psycopg2.extras import Json
import pytest

ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = ROOT / 'database/migrations'
MIGRATION = MIGRATIONS / '027_customer_app_backfill.sql'
STITCHER = ROOT.parent / 'fargate-user-stitching'
APP = '000222'
ENDPOINT = 'host=127.0.0.1 port=61577 user=postgres connect_timeout=3'


def query(c, statement, args=None):
    with c.cursor() as q:
        q.execute(statement, args)
        return q.fetchall() if q.description else []


@pytest.fixture(scope='module')
def owned_database():
    name = 'customer_app_backfill_test_' + uuid4().hex[:12]
    with closing(psycopg2.connect(ENDPOINT, dbname='postgres')) as admin:
        admin.autocommit = True
        assert not query(admin, 'SELECT 1 FROM pg_database WHERE datname=%s', (name,))
        query(admin, sql.SQL("CREATE DATABASE {} TEMPLATE template0 ENCODING 'UTF8'").format(sql.Identifier(name)))
        try:
            yield name
        finally:
            query(admin, sql.SQL('DROP DATABASE {}').format(sql.Identifier(name)))
            assert not query(admin, 'SELECT 1 FROM pg_database WHERE datname=%s', (name,))


@pytest.fixture
def db(owned_database):
    with closing(psycopg2.connect(ENDPOINT, dbname=owned_database)) as c:
        assert (c.info.host, c.info.port, c.info.dbname) == ('127.0.0.1', 61577, owned_database)
        c.autocommit = True
        assert query(c, 'SHOW server_encoding') == [('UTF8',)]
        query(c, 'DROP SCHEMA IF EXISTS identity CASCADE; DROP SCHEMA IF EXISTS connector CASCADE')
        query(c, (ROOT / 'tests/integration/fixtures/customer_history_base.sql').read_text())
        query(c, '''ALTER TABLE control.source_landing_batch ADD source_code text, ADD batch_ref text,
            ADD status text, ADD output_published_at timestamptz, ADD updated_at timestamptz DEFAULT now();
            ALTER TABLE control.ingestion_batch ADD batch_ref text, ADD records_received bigint,
            ADD records_rejected bigint DEFAULT 0, ADD completed_at timestamptz, ADD updated_at timestamptz DEFAULT now();
            CREATE TABLE control.source_system(source_system_id bigint PRIMARY KEY,source_code text,source_name text);
            INSERT INTO control.source_system VALUES(1,'not-an-app','synthetic'),(2,'also-not-an-app','synthetic');''')
        query(c, (STITCHER / 'src/identity_stitcher/sql/001_identity_model.sql').read_text())
        for file in ('014_pipeline_tracking.sql', '019_customer_typed_history.sql',
                     '022_customer_bulk_ingestion.sql', '024_customer_identity_publication.sql',
                     '025_customer_publication_status.sql', '026_customer_app_scope.sql'):
            query(c, (MIGRATIONS / file).read_text())
        c.autocommit = False
        try:
            yield c
        finally:
            c.rollback()


def install(c):
    assert MIGRATION.exists(), 'historical app resolution ledger migration 027 missing'
    c.commit()
    c.autocommit = True
    query(c, MIGRATION.read_text())
    query(c, (MIGRATIONS / '028_customer_app_backfill_access_paths.sql').read_text())
    c.autocommit = False


def batch(c, app=APP, source=1, created='2026-09-01T00:00:00Z'):
    b, landing = str(uuid4()), str(uuid4())
    ref = f'2026-09-01-120001-{app}-{uuid4().hex[:12]}'
    query(c, '''INSERT INTO control.source_landing_batch(batch_id,batch_ref,source_code,output_status)
        SELECT %s,%s,source_code,'PUBLISHED' FROM control.source_system WHERE source_system_id=%s''', (landing, ref, source))
    query(c, '''INSERT INTO control.ingestion_batch(batch_id,landing_batch_id,source_system_id,batch_ref,status,created_at)
        VALUES(%s,%s,%s,%s,'completed',%s)''', (b, landing, source, ref, created))
    return b


def upload(c, b, user='same-user', name='original', source=1):
    return query(c, '''INSERT INTO raw.customer_current(batch_id,source_system_id,source_customer_id,first_name)
        VALUES(%s,%s,%s,%s) ON CONFLICT(source_system_id,source_customer_id)
        DO UPDATE SET batch_id=EXCLUDED.batch_id,first_name=EXCLUDED.first_name
        RETURNING raw_customer_id::text''', (b, source, user, name))[0][0]


def prepare(c, batches, app=APP, expected=1, baseline=0):
    run = str(uuid4())
    workspace = query(c, '''INSERT INTO control.customer_identity_upload_scope(app_id) VALUES(%s)
        ON CONFLICT DO NOTHING RETURNING workspace_id::text''', (app,))
    workspace = workspace[0][0] if workspace else query(c,
        'SELECT workspace_id::text FROM control.customer_identity_upload_scope WHERE app_id=%s', (app,))[0][0]
    query(c, '''INSERT INTO identity.resolution_run(run_id,status,algorithm,algorithm_version,normalization_version,
        match_threshold,source_record_count,selected_record_count,standardized_record_count,entity_count,metrics)
        VALUES(%s,'RUNNING','splink','test','app-historical-v1',0.9,%s,%s,%s,%s,'{"scope":"app"}')''',
        (run, expected, expected, expected, expected))
    query(c, '''INSERT INTO control.customer_app_backfill_run(run_id,app_id,workspace_id,status,expected_users,
        input_sha256,cutoff,baseline_publication_seq,config)
        VALUES(%s,%s,%s,'BUILDING',%s,%s,'2026-09-19T00:00:00Z',%s,'{"normalization":"test"}')''',
        (run, app, workspace, expected, 'a' * 64, baseline))
    query(c, '''INSERT INTO control.customer_app_backfill_batch(run_id,batch_id,expected_members,available_observations,batch_created_at)
        SELECT %s,b.batch_id,count(m.raw_customer_id),count(h.raw_customer_id),b.created_at
        FROM control.ingestion_batch b LEFT JOIN raw.customer_batch_member m ON m.ingestion_batch_id=b.batch_id
        LEFT JOIN raw.customer h ON (h.batch_id,h.raw_customer_id,h.source_system_id,h.source_customer_id)=
            (m.ingestion_batch_id,m.raw_customer_id,m.source_system_id,m.source_customer_id)
        WHERE b.batch_id=ANY(%s::uuid[]) GROUP BY b.batch_id''', (run, batches))
    return run, workspace


def record(c, run, workspace, b, raw, user='same-user', app=APP, source=1, number=1, override=None,
           record_id=None, normalized=None):
    payload = dict(customer_entity_id=str(uuid4()), match_probability=1.0, match_method='singleton',
        source_system_id=source, standardized_record_id=None, raw_customer_id=raw,
        resolution_run_id=run, workspace_id=workspace, app_id=app, user_id=user,
        profile={'customer_id': user, 'first_name': 'resolved'})
    if override:
        payload.update(override)
    query(c, '''INSERT INTO control.customer_app_backfill_record(run_id,row_number,app_id,source_system_id,
        user_id,raw_customer_id,source_batch_id,input_payload,payload)
        VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s)''',
        (run, number, app, source, user, raw, b, Json({'record_id': record_id or str(uuid4()),
            'normalized': normalized if normalized is not None else {'first_name': 'original'},
            'raw': {'raw_customer_id': raw, 'batch_id': b, 'source_system_id': source, 'source_customer_id': user, 'app_id': app}}), Json(payload)))
    return payload


def finish(c, run):
    query(c, 'SELECT control.finish_customer_app_backfill(%s)', (run,))


def current_state(c):
    return {table: query(c, f'SELECT to_jsonb(t) FROM {table} t ORDER BY to_jsonb(t)::text') for table in (
        'raw.customer_current', 'staging.customer_profile', 'identity.customer_entity',
        'identity.customer_entity_member', 'identity.match_pair', 'control.stitching_job')}


def test_historical_success_is_atomic_owned_resolution_and_export_without_current_writes(db):
    install(db)
    a = batch(db)
    raw = upload(db, a)
    other = batch(db, app='222', created='2026-09-02T00:00:00Z')
    assert upload(db, other, name='other-app-current') == raw
    before = current_state(db)
    run, workspace = prepare(db, [a])
    payload = record(db, run, workspace, a, raw)
    finish(db, run)
    assert query(db, 'SELECT status,completed_at IS NOT NULL FROM identity.resolution_run WHERE run_id=%s', (run,)) == [('SUCCEEDED', True)]
    assert query(db, '''SELECT transfer_id::text,resolution_run_id::text,backfill_run_id::text,
        stitching_job_id,ingestion_batch_id,status,row_count,app_id FROM control.customer_identity_transfer''') == [
        (run, run, run, None, None, 'PENDING', 1, APP)]
    assert query(db, 'SELECT row_number,user_id,payload FROM control.customer_identity_export') == [(1, 'same-user', payload)]
    assert current_state(db) == before
    db.commit()
    finish(db, run)
    finish(db, run)
    assert query(db, 'SELECT count(*) FROM control.customer_identity_export') == [(1,)]
    assert query(db, 'SELECT count(*) FROM control.customer_identity_transfer') == [(1,)]
    assert current_state(db) == before


def test_pair_evidence_is_run_local_and_never_exported(db):
    install(db)
    b = batch(db)
    raws = [upload(db, b, user=u) for u in ('a', 'b')]
    run, workspace = prepare(db, [b], expected=2)
    entity = str(uuid4())
    payloads = [record(db, run, workspace, b, raw, user=u, number=i,
        override={'customer_entity_id': entity, 'match_probability': 0.95, 'match_method': 'splink'})
        for i, (raw, u) in enumerate(zip(raws, ('a', 'b')), 1)]
    ids = sorted(r[0] for r in query(db, "SELECT input_payload->>'record_id' FROM control.customer_app_backfill_record"))
    query(db, '''INSERT INTO control.customer_app_backfill_pair(run_id,left_record_id,right_record_id,probability,method,evidence)
        VALUES(%s,%s,%s,0.95,'splink','{"rule":"exact"}')''', (run, *ids))
    query(db, 'UPDATE identity.resolution_run SET candidate_pair_count=1,accepted_pair_count=1,entity_count=1 WHERE run_id=%s', (run,))
    finish(db, run)
    assert query(db, 'SELECT count(*) FROM control.customer_app_backfill_pair') == [(1,)]
    assert query(db, 'SELECT count(*) FROM identity.match_pair') == [(0,)]
    assert query(db, "SELECT bool_and(NOT payload ? 'evidence') FROM control.customer_identity_export") == [(True,)]
    assert query(db, 'SELECT payload FROM control.customer_identity_export ORDER BY row_number') == [(p,) for p in payloads]


def test_deterministic_accepted_pairs_can_exceed_probabilistic_candidates(db):
    install(db)
    b = batch(db)
    raws = [upload(db, b, user=u) for u in ('a', 'b')]
    run, workspace = prepare(db, [b], expected=2)
    entity = str(uuid4())
    for i, (raw, user) in enumerate(zip(raws, ('a', 'b')), 1):
        record(db, run, workspace, b, raw, user=user, number=i,
            override={'customer_entity_id': entity, 'match_method': 'deterministic'})
    ids = sorted(r[0] for r in query(db, "SELECT input_payload->>'record_id' FROM control.customer_app_backfill_record"))
    query(db, '''INSERT INTO control.customer_app_backfill_pair
        (run_id,left_record_id,right_record_id,probability,method,evidence)
        VALUES(%s,%s,%s,1.0,'deterministic','{}')''', (run, *ids))
    query(db, 'UPDATE identity.resolution_run SET candidate_pair_count=0,accepted_pair_count=1,entity_count=1 WHERE run_id=%s', (run,))
    finish(db, run)
    assert query(db, 'SELECT status,candidate_pair_count,accepted_pair_count FROM identity.resolution_run WHERE run_id=%s', (run,)) == [('SUCCEEDED', 0, 1)]
    assert query(db, 'SELECT row_count FROM control.customer_identity_transfer WHERE backfill_run_id=%s', (run,)) == [(2,)]


def test_real_matcher_deterministic_report_finalizes_without_probabilistic_candidates(db):
    import pandas as pd
    from identity_stitcher.config import AppConfig, DatabaseConfig
    from identity_stitcher.linkage import resolve_frame

    install(db)
    b = batch(db)
    users = ('a', 'b', 'c')
    raws = [upload(db, b, user=user) for user in users]
    run, workspace = prepare(db, [b], expected=3)
    profiles = [dict(
        unique_id=str(uuid4()), source_system_id=1, source_customer_id=user,
        first_name_normalized='original', last_name_normalized=None,
        full_name_normalized='original', email_normalized=None, email_is_valid=False,
        phone_normalized=None, phone_is_valid=False, address_normalized=None,
        district_normalized=None, province_normalized=None, country_code=None,
        date_of_birth=None, gender_normalized=None,
        national_id_hash='synthetic-shared-national-id', device_ids_normalized=[],
    ) for user in users]
    config = AppConfig(
        database=DatabaseConfig('127.0.0.1', 61577, db.info.dbname, 'postgres', ''),
        match_threshold=0.9, normalization_version='app-historical-v1',
        max_blocking_group_size=2, duckdb_memory_limit='256MB', duckdb_threads=1,
    )
    # Real blocker masking and deterministic spanning edges, no matcher mocks.
    result = resolve_frame(pd.DataFrame(profiles), config)
    assert (result.candidate_pair_count, len(result.pairs), result.entity_count) == (0, 2, 1)
    payloads = []
    for number, (raw, user, profile) in enumerate(zip(raws, users, profiles), 1):
        record_id = profile['unique_id']
        payloads.append(record(db, run, workspace, b, raw, user=user, number=number,
            record_id=record_id, normalized=profile, override={
                'customer_entity_id': result.memberships[record_id],
                'match_probability': result.member_probabilities[record_id],
                'match_method': result.member_methods[record_id],
            }))
    for pair in result.pairs:
        query(db, '''INSERT INTO control.customer_app_backfill_pair
            (run_id,left_record_id,right_record_id,probability,method,evidence)
            VALUES(%s,%s,%s,%s,%s,%s)''', (run, pair.record_id_l, pair.record_id_r,
                pair.probability, pair.method, Json(pair.evidence)))
    query(db, '''UPDATE identity.resolution_run SET candidate_pair_count=%s,
        accepted_pair_count=%s,entity_count=%s WHERE run_id=%s''',
        (result.candidate_pair_count, len(result.pairs), result.entity_count, run))
    before = current_state(db)
    finish(db, run)
    db.commit()
    assert query(db, '''SELECT status,candidate_pair_count,accepted_pair_count,entity_count
        FROM identity.resolution_run WHERE run_id=%s''', (run,)) == [('SUCCEEDED', 0, 2, 1)]
    assert query(db, '''SELECT status,row_count FROM control.customer_identity_transfer
        WHERE backfill_run_id=%s''', (run,)) == [('PENDING', 3)]
    assert query(db, '''SELECT payload FROM control.customer_identity_export
        WHERE transfer_id=%s ORDER BY row_number''', (run,)) == [(p,) for p in payloads]
    assert query(db, '''SELECT count(*) FROM control.customer_app_backfill_pair
        WHERE run_id=%s''', (run,)) == [(2,)]
    assert current_state(db) == before


def test_accepted_pair_cannot_cross_exported_entities(db):
    install(db)
    b = batch(db)
    raws = [upload(db, b, user=u) for u in ('a', 'b')]
    run, workspace = prepare(db, [b], expected=2)
    payloads = [record(db, run, workspace, b, raw, user=u, number=i)
        for i, (raw, u) in enumerate(zip(raws, ('a', 'b')), 1)]
    assert payloads[0]['customer_entity_id'] != payloads[1]['customer_entity_id']
    ids = sorted(r[0] for r in query(db, "SELECT input_payload->>'record_id' FROM control.customer_app_backfill_record"))
    query(db, '''INSERT INTO control.customer_app_backfill_pair
        (run_id,left_record_id,right_record_id,probability,method,evidence)
        VALUES(%s,%s,%s,0.95,'splink','{}')''', (run, *ids))
    query(db, '''UPDATE identity.resolution_run SET candidate_pair_count=1,
        accepted_pair_count=1,entity_count=2 WHERE run_id=%s''', (run,))
    before = current_state(db)
    db.commit()
    with pytest.raises(psycopg2.Error, match='BACKFILL_INVALID_PAIR'):
        finish(db, run)
    db.rollback()
    assert query(db, 'SELECT status FROM control.customer_app_backfill_run WHERE run_id=%s', (run,)) == [('BUILDING',)]
    assert query(db, 'SELECT status FROM identity.resolution_run WHERE run_id=%s', (run,)) == [('RUNNING',)]
    assert query(db, 'SELECT count(*) FROM control.customer_identity_transfer') == [(0,)]
    assert query(db, 'SELECT count(*) FROM control.customer_identity_export') == [(0,)]
    assert current_state(db) == before


@pytest.mark.parametrize('use_local_id', [True, False])
def test_historical_standardized_record_id_must_be_null(db, use_local_id):
    install(db)
    b = batch(db)
    raw = upload(db, b)
    run, workspace = prepare(db, [b])
    local_id = str(uuid4())
    record(db, run, workspace, b, raw, record_id=local_id,
        override={'standardized_record_id': local_id if use_local_id else str(uuid4())})
    db.commit()
    with pytest.raises(psycopg2.Error, match='BACKFILL_INVALID_PAYLOAD'):
        finish(db, run)
    db.rollback()
    assert query(db, 'SELECT status FROM control.customer_app_backfill_run WHERE run_id=%s', (run,)) == [('BUILDING',)]
    assert query(db, 'SELECT status FROM identity.resolution_run WHERE run_id=%s', (run,)) == [('RUNNING',)]
    assert query(db, 'SELECT count(*) FROM control.customer_identity_transfer') == [(0,)]
    assert query(db, 'SELECT count(*) FROM control.customer_identity_export') == [(0,)]


def test_pair_cannot_reference_another_run_record(db):
    install(db)
    b = batch(db)
    raw = upload(db, b)
    run, workspace = prepare(db, [b])
    record(db, run, workspace, b, raw)
    ids = sorted([query(db, "SELECT input_payload->>'record_id' FROM control.customer_app_backfill_record")[0][0], str(uuid4())])
    with pytest.raises(psycopg2.Error, match='PAIR|foreign key'):
        query(db, '''INSERT INTO control.customer_app_backfill_pair(run_id,left_record_id,right_record_id,probability,method,evidence)
            VALUES(%s,%s,%s,0.95,'splink','{}')''', (run, *ids))
        finish(db, run)


def grant_runtime(c, role):
    query(c, sql.SQL('GRANT USAGE ON SCHEMA control,raw,identity TO {}').format(sql.Identifier(role)))
    query(c, sql.SQL('''GRANT SELECT ON raw.customer,raw.customer_batch_member,
        control.ingestion_batch,control.source_landing_batch,control.source_system,
        control.customer_identity_upload_scope,control.customer_identity_transfer,
        identity.resolution_run TO {}''').format(sql.Identifier(role)))
    query(c, sql.SQL('''GRANT SELECT,INSERT ON control.customer_app_backfill_run,
        control.customer_app_backfill_batch,control.customer_app_backfill_record,
        control.customer_app_backfill_pair TO {}''').format(sql.Identifier(role)))
    query(c, sql.SQL('GRANT UPDATE(status,error_code) ON control.customer_app_backfill_run TO {}').format(sql.Identifier(role)))
    query(c, sql.SQL('GRANT INSERT(app_id,workspace_id) ON control.customer_identity_upload_scope TO {}').format(sql.Identifier(role)))
    query(c, sql.SQL('GRANT EXECUTE ON FUNCTION control.customer_app_scope_status(uuid) TO {}').format(sql.Identifier(role)))
    query(c, sql.SQL('''GRANT INSERT ON identity.resolution_run TO {};
        GRANT UPDATE(source_record_count,selected_record_count,standardized_record_count,
            candidate_pair_count,accepted_pair_count,entity_count,metrics) ON identity.resolution_run TO {};
        GRANT EXECUTE ON FUNCTION control.finish_customer_app_backfill(uuid) TO {}''').format(*[sql.Identifier(role)] * 3))


def test_private_default_acls_and_narrow_runtime_can_finalize(db):
    role = 'backfill_runtime_' + uuid4().hex[:12]
    query(db, sql.SQL('CREATE ROLE {}').format(sql.Identifier(role)))
    query(db, sql.SQL('ALTER DEFAULT PRIVILEGES IN SCHEMA control GRANT ALL ON TABLES TO {}').format(sql.Identifier(role)))
    query(db, sql.SQL('ALTER DEFAULT PRIVILEGES IN SCHEMA control GRANT EXECUTE ON FUNCTIONS TO {}').format(sql.Identifier(role)))
    db.commit()
    try:
        install(db)
        assert query(db, '''SELECT c.relname FROM pg_class c
            WHERE c.relnamespace='control'::regnamespace AND c.relname LIKE 'customer_app_backfill_%%'
            AND c.relkind='r' AND has_table_privilege(%s,c.oid,'SELECT,INSERT,UPDATE,DELETE,TRUNCATE')''', (role,)) == []
        assert query(db, '''SELECT p.proname FROM pg_proc p WHERE p.pronamespace='control'::regnamespace
            AND p.proname LIKE '%%customer_app_backfill%%' AND has_function_privilege(%s,p.oid,'EXECUTE')''', (role,)) == []
        b = batch(db)
        raw = upload(db, b)
        grant_runtime(db, role)
        query(db, sql.SQL('SET LOCAL ROLE {}').format(sql.Identifier(role)))
        run, workspace = prepare(db, [b])
        record(db, run, workspace, b, raw)
        assert query(db, 'SELECT app_id,error_code FROM control.customer_app_scope_status(%s)', (b,)) == [(APP, None)]
        finish(db, run)
        assert query(db, 'SELECT status FROM control.customer_identity_transfer') == [('PENDING',)]
        for table in ('raw.customer_current','staging.customer_profile','identity.customer_entity',
                      'identity.customer_entity_member','control.customer_identity_export'):
            assert query(db, '''SELECT has_table_privilege(current_user,c.oid,'INSERT,UPDATE,DELETE,TRUNCATE')
                FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
                WHERE n.nspname || '.' || c.relname=%s''', (table,)) == [(False,)]
        assert query(db, "SELECT has_column_privilege(current_user,'identity.resolution_run','status','UPDATE')") == [(False,)]
    finally:
        db.rollback()
        query(db, 'RESET ROLE')
        query(db, sql.SQL('DROP OWNED BY {}; DROP ROLE {}').format(sql.Identifier(role), sql.Identifier(role)))
        db.commit()


@pytest.mark.parametrize('missing_latest', [False, True])
def test_membership_winners_not_available_history_define_completeness(db, missing_latest):
    install(db)
    older = batch(db, created='2026-09-01T00:00:00Z')
    newer = batch(db, created='2026-09-02T00:00:00Z')
    available, missing = (older, newer) if missing_latest else (newer, older)
    raw = upload(db, available)
    # Genuine pre-cutover membership-only observation: not fabricated raw history.
    query(db, '''INSERT INTO raw.customer_batch_member(ingestion_batch_id,raw_customer_id,source_system_id,source_customer_id)
        VALUES(%s,%s,1,'same-user')''', (missing, raw))
    run, workspace = prepare(db, [older, newer])
    record(db, run, workspace, available, raw)
    db.commit()
    if missing_latest:
        with pytest.raises(psycopg2.Error, match='LATEST_INPUT_INCOMPLETE'):
            finish(db, run)
        db.rollback()
        assert query(db, 'SELECT status FROM control.customer_app_backfill_run') == [('BUILDING',)]
        assert query(db, 'SELECT status FROM identity.resolution_run') == [('RUNNING',)]
        assert query(db, 'SELECT count(*) FROM control.customer_identity_transfer') == [(0,)]
    else:
        finish(db, run)
        assert query(db, '''SELECT sum(expected_members),sum(available_observations),
            sum(expected_members-available_observations) FROM control.customer_app_backfill_batch''') == [(2, 1, 1)]
        assert query(db, 'SELECT row_count FROM control.customer_identity_transfer') == [(1,)]


@pytest.mark.parametrize('status', ['PUBLISHED', 'PENDING', 'RUNNING', 'FAILED', 'BLOCKED'])
def test_any_newer_same_app_outbox_invalidates_prepared_inputs(db, status):
    install(db)
    b = batch(db)
    raw = upload(db, b)
    run, workspace = prepare(db, [b])
    record(db, run, workspace, b, raw)
    newer, _ = prepare(db, [b])
    query(db, '''INSERT INTO control.customer_identity_transfer(transfer_id,resolution_run_id,backfill_run_id,
        app_id,workspace_id,status,row_count) VALUES(%s,%s,%s,%s,%s,%s,0)''', (newer, newer, newer, APP, workspace, status))
    with pytest.raises(psycopg2.Error, match='STALE_PUBLICATION'):
        finish(db, run)


@pytest.mark.parametrize('field,value', [
    ('app_id', '222'), ('user_id', 'wrong'), ('source_system_id', 2),
    ('raw_customer_id', '00000000-0000-0000-0000-000000000000'),
    ('resolution_run_id', '00000000-0000-0000-0000-000000000000'),
    ('workspace_id', '00000000-0000-0000-0000-000000000000'),
    ('source_batch_id', '00000000-0000-0000-0000-000000000000'),
    ('customer_entity_id', 'not-uuid'), ('match_probability', -0.1),
    ('match_probability', 1.1), ('match_probability', None), ('profile', {'customer_id': 'wrong'}),
])
def test_invalid_payload_fails_without_success_or_outbox(db, field, value):
    install(db)
    b = batch(db)
    raw = upload(db, b)
    run, workspace = prepare(db, [b])
    record(db, run, workspace, b, raw, override={field: value})
    with pytest.raises(psycopg2.Error, match='INVALID_PAYLOAD'):
        finish(db, run)


@pytest.mark.parametrize('table', ['run', 'batch', 'record', 'pair'])
def test_persisted_ledger_cannot_update_delete_or_truncate(db, table):
    install(db)
    b = batch(db)
    raw = upload(db, b)
    run, workspace = prepare(db, [b])
    record(db, run, workspace, b, raw)
    db.commit()
    target = 'control.customer_app_backfill_' + table
    column = {'run': 'input_sha256', 'batch': 'available_observations', 'record': 'payload', 'pair': 'evidence'}[table]
    update = f"UPDATE {target} SET {column}=" + ("repeat('b',64)" if table == 'run' else column)
    for statement in (update, f'DELETE FROM {target}', f'TRUNCATE {target} CASCADE'):
        with pytest.raises(psycopg2.Error, match='immutable'):
            query(db, statement)
        db.rollback()
    finish(db, run)
    db.commit()
    with pytest.raises(psycopg2.Error, match='finalized|duplicate key'):
        query(db, '''INSERT INTO control.customer_app_backfill_record SELECT run_id,99,app_id,source_system_id,
            user_id||'another',raw_customer_id,source_batch_id,input_payload,payload FROM control.customer_app_backfill_record''')
    db.rollback()
    with pytest.raises(psycopg2.Error, match='immutable'):
        query(db, 'UPDATE control.customer_identity_transfer SET backfill_run_id=NULL')


def test_finalization_rollback_is_invisible_to_other_connections(db, owned_database):
    install(db)
    b = batch(db)
    raw = upload(db, b)
    run, workspace = prepare(db, [b])
    record(db, run, workspace, b, raw)
    before = current_state(db)
    db.commit()
    finish(db, run)
    with closing(psycopg2.connect(ENDPOINT, dbname=owned_database)) as observer:
        assert query(observer, 'SELECT status FROM control.customer_app_backfill_run') == [('BUILDING',)]
        assert query(observer, 'SELECT status FROM identity.resolution_run') == [('RUNNING',)]
        assert query(observer, 'SELECT count(*) FROM control.customer_identity_transfer') == [(0,)]
        assert query(observer, 'SELECT count(*) FROM control.customer_identity_export') == [(0,)]
    db.rollback()
    assert current_state(db) == before
    assert query(db, 'SELECT status FROM identity.resolution_run') == [('RUNNING',)]
    finish(db, run)
    db.commit()
    assert query(db, 'SELECT count(*) FROM control.customer_identity_export') == [(1,)]


def test_exact_manifest_and_source_tuple_cannot_be_narrowed(db):
    install(db)
    b = batch(db)
    raw = upload(db, b)
    extra = batch(db, created='2026-09-02T00:00:00Z')
    upload(db, extra, user='other-user')
    run, workspace = prepare(db, [b])
    record(db, run, workspace, b, raw)
    with pytest.raises(psycopg2.Error, match='MANIFEST_SCOPE'):
        finish(db, run)


@pytest.mark.parametrize('problem', ['foreign_batch', 'wrong_source', 'wrong_user', 'old_winner', 'duplicate_source_user'])
def test_wrong_source_selection_never_uses_global_current_fallback(db, problem):
    install(db)
    b = batch(db)
    raw = upload(db, b)
    batches = [b]
    if problem in ('old_winner', 'foreign_batch'):
        other = batch(db, app=APP if problem == 'old_winner' else '333', created='2026-09-02T00:00:00Z')
        upload(db, other, name='later')
        batches.append(other)
    if problem == 'duplicate_source_user':
        other = batch(db, source=2)
        upload(db, other, source=2)
        batches.append(other)
    run, workspace = prepare(db, batches, expected=2 if problem == 'duplicate_source_user' else 1)
    record(db, run, workspace, b, raw, source=2 if problem == 'wrong_source' else 1,
        user='wrong-user' if problem == 'wrong_user' else 'same-user')
    with pytest.raises(psycopg2.Error, match='MANIFEST_SCOPE|LATEST_INPUT_INCOMPLETE'):
        finish(db, run)


def test_027_keeps_existing_views_and_live_capture_working(db):
    before = query(db, '''SELECT c.oid,c.relacl,pg_get_viewdef(c.oid) FROM pg_class c
        WHERE c.relkind='v' AND c.relnamespace='control'::regnamespace ORDER BY c.oid''')
    install(db)
    assert query(db, '''SELECT c.oid,c.relacl,pg_get_viewdef(c.oid) FROM pg_class c
        WHERE c.relkind='v' AND c.relnamespace='control'::regnamespace ORDER BY c.oid''') == before
    b = batch(db)
    raw = upload(db, b)
    job, run, standardized, entity = [str(uuid4()) for _ in range(4)]
    query(db, "INSERT INTO control.stitching_job(job_id,ingestion_batch_id,status) VALUES(%s,%s,'RUNNING')", (job, b))
    query(db, '''INSERT INTO identity.resolution_run(run_id,stitching_job_id,status,normalization_version,match_threshold)
        VALUES(%s,%s,'RUNNING','test',0.9)''', (run, job))
    query(db, '''INSERT INTO staging.customer_profile(standardized_record_id,raw_customer_id,batch_id,source_system_id,source_customer_id)
        VALUES(%s,%s,%s,1,'same-user')''', (standardized, raw, b))
    query(db, 'INSERT INTO identity.customer_entity(customer_entity_id,last_resolution_run_id) VALUES(%s,%s)', (entity, run))
    query(db, '''INSERT INTO identity.customer_entity_member(standardized_record_id,customer_entity_id,source_system_id,
        source_customer_id,match_method,resolution_run_id) VALUES(%s,%s,1,'same-user','singleton',%s)''', (standardized, entity, run))
    query(db, "UPDATE identity.resolution_run SET status='SUCCEEDED',completed_at=clock_timestamp()")
    query(db, "UPDATE control.stitching_job SET status='SUCCEEDED',selected_record_count=1,member_record_count=1")
    assert query(db, 'SELECT status,backfill_run_id FROM control.customer_identity_transfer') == [('PENDING', None)]
    assert query(db, 'SELECT count(*) FROM control.customer_identity_export') == [(1,)]


def test_running_ingestion_is_not_part_of_fixed_completed_manifest(db):
    install(db)
    b = batch(db)
    raw = upload(db, b)
    running = batch(db)
    query(db, "UPDATE control.ingestion_batch SET status='RUNNING' WHERE batch_id=%s", (running,))
    run, workspace = prepare(db, [b])
    record(db, run, workspace, b, raw)
    finish(db, run)
    assert query(db, 'SELECT row_count FROM control.customer_identity_transfer') == [(1,)]


def test_failed_ledger_marks_only_its_resolution_failed(db):
    install(db)
    b = batch(db)
    upload(db, b)
    run, _ = prepare(db, [b])
    before = current_state(db)
    query(db, "UPDATE control.customer_app_backfill_run SET status='FAILED',error_code='MATCH_BUDGET' WHERE run_id=%s", (run,))
    assert query(db, 'SELECT status,completed_at IS NOT NULL,error_message FROM identity.resolution_run') == [('FAILED', True, 'MATCH_BUDGET')]
    assert current_state(db) == before
    assert query(db, 'SELECT count(*) FROM control.customer_identity_transfer') == [(0,)]


@pytest.mark.parametrize('mutation,reason', [
    ("UPDATE identity.resolution_run SET selected_record_count=0", 'INCOMPLETE_REPORT'),
    ("UPDATE identity.resolution_run SET entity_count=0", 'INCOMPLETE_REPORT'),
    ("UPDATE identity.resolution_run SET status='SUCCEEDED',completed_at=now()", 'INVALID_RESOLUTION'),
])
def test_incomplete_or_pretend_resolution_cannot_finalize(db, mutation, reason):
    install(db)
    b = batch(db)
    raw = upload(db, b)
    run, workspace = prepare(db, [b])
    record(db, run, workspace, b, raw)
    query(db, mutation)
    with pytest.raises(psycopg2.Error, match=reason):
        finish(db, run)


def test_row_numbers_are_contiguous_user_sorted_not_matcher_order(db):
    install(db)
    b = batch(db)
    raw = upload(db, b)
    run, workspace = prepare(db, [b])
    record(db, run, workspace, b, raw, number=2)
    with pytest.raises(psycopg2.Error, match='INVALID_PAYLOAD'):
        finish(db, run)


def test_different_app_publication_does_not_invalidate_manifest(db):
    install(db)
    b = batch(db)
    raw = upload(db, b)
    run, workspace = prepare(db, [b])
    record(db, run, workspace, b, raw)
    other = batch(db, app='333')
    other_run, other_workspace = prepare(db, [other], app='333', expected=0)
    query(db, '''INSERT INTO control.customer_identity_transfer(transfer_id,resolution_run_id,backfill_run_id,
        app_id,workspace_id,status,row_count) VALUES(%s,%s,%s,'333',%s,'PENDING',0)''',
        (other_run, other_run, other_run, other_workspace))
    finish(db, run)
    assert query(db, 'SELECT row_count FROM control.customer_identity_transfer WHERE transfer_id=%s', (run,)) == [(1,)]


def test_export_failure_rolls_back_resolution_and_outbox_together(db):
    install(db)
    b = batch(db)
    raw = upload(db, b)
    run, workspace = prepare(db, [b])
    record(db, run, workspace, b, raw)
    query(db, '''CREATE FUNCTION control.test_fail_export() RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN RAISE EXCEPTION 'TEST_EXPORT_STORAGE_FAILURE'; END $$;
        CREATE TRIGGER test_export_failure BEFORE INSERT ON control.customer_identity_export
        FOR EACH ROW EXECUTE FUNCTION control.test_fail_export()''')
    db.commit()
    with pytest.raises(psycopg2.Error, match='TEST_EXPORT_STORAGE_FAILURE'):
        finish(db, run)
    db.rollback()
    assert query(db, 'SELECT status FROM identity.resolution_run') == [('RUNNING',)]
    assert query(db, 'SELECT status FROM control.customer_app_backfill_run') == [('BUILDING',)]
    assert query(db, 'SELECT count(*) FROM control.customer_identity_transfer') == [(0,)]
    assert query(db, 'SELECT count(*) FROM control.customer_identity_export') == [(0,)]


def test_concurrent_historical_finalizers_serialize_and_reject_stale_loser(db, owned_database):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    install(db)
    b = batch(db)
    raw = upload(db, b)
    first, workspace = prepare(db, [b])
    second, _ = prepare(db, [b])
    record(db, first, workspace, b, raw)
    record(db, second, workspace, b, raw)
    db.commit()
    finish(db, first)  # owns global publication lock until commit
    entered = Event()
    def competing_finish():
        with closing(psycopg2.connect(ENDPOINT, dbname=owned_database)) as c:
            query(c, "SET statement_timeout='5s'")
            entered.set()
            try:
                finish(c, second)
                c.commit()
                return 'unexpected success'
            except psycopg2.Error as error:
                c.rollback()
                return error.diag.message_primary
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(competing_finish)
        assert entered.wait(3)
        db.commit()
        assert future.result(timeout=8) == 'BACKFILL_STALE_PUBLICATION'
    assert query(db, 'SELECT count(*) FROM control.customer_identity_transfer') == [(1,)]
    assert query(db, 'SELECT status FROM control.customer_app_backfill_run WHERE run_id=%s', (second,)) == [('BUILDING',)]


def test_connector_lineage_cannot_be_included_as_file_history(db):
    install(db)
    b = batch(db)
    raw = upload(db, b)
    run, workspace = prepare(db, [b])
    record(db, run, workspace, b, raw)
    # Late connector registration leaves physical app_id stale; still reject.
    query(db, '''CREATE SCHEMA connector;
        CREATE TABLE connector.sync_run(ingestion_batch_id uuid,landing_batch_id uuid,stitching_job_id uuid);
        INSERT INTO connector.sync_run(ingestion_batch_id) VALUES(%s)''', (b,))
    with pytest.raises(psycopg2.Error, match='MANIFEST_SCOPE'):
        finish(db, run)
