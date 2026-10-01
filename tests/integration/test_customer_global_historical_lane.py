"""048 forward-only historical global lane on a fresh, owned localhost PG database.

The imported fixture refuses an unexpected PG system ID and drops only its UUID
named UTF8 database. Never uses staging/production credentials or deploys SQL.
Run: PYTHONDONTWRITEBYTECODE=1 ../fargate-user-stitching/.venv/bin/python
    -m pytest --confcutdir=tests/integration -p no:cacheprovider tests/integration/test_customer_global_historical_lane.py
"""
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import psycopg2
from psycopg2.extras import Json
import pytest

import test_customer_app_backfill as base
import test_customer_app_backfill_logical_winner as fixture

ROOT = Path(__file__).resolve().parents[2]
SQL = ROOT / 'database/migrations/048_customer_global_historical_lane.sql'
TFG = '1282009980020330'
TARGET = '1789615257658778166'
q = base.query
owned_database = fixture.owned_database
db = fixture.db


def install(c):
    c.commit()
    c.autocommit = True
    q(c, 'CREATE EXTENSION IF NOT EXISTS pgcrypto')
    for file in ('039_customer_app_backfill_logical_winner.sql',
                 '041_global_member_bridge.sql', '048_customer_global_historical_lane.sql'):
        q(c, (base.MIGRATIONS / file).read_text())
    c.autocommit = False


def batch(c, app, *, created):
    return base.batch(c, app=app, source=1, created=created)


def observe(c, batch_id, app, *, user='same-user', name='Old',
            email='shared@example.test', phone='+66812345678', full_name=None,
            dob=None):
    # Produce immutable observations with their identifiers at upload time;
    # never rewrite history later to manufacture a matcher edge in a fixture.
    raw = q(c, '''INSERT INTO raw.customer_current(batch_id,source_system_id,
        source_customer_id,first_name,email,phone,full_name,date_of_birth)
        VALUES(%s,1,%s,%s,%s,%s,%s,%s) ON CONFLICT(source_system_id,source_customer_id)
        DO UPDATE SET batch_id=EXCLUDED.batch_id,first_name=EXCLUDED.first_name,
            email=EXCLUDED.email,phone=EXCLUDED.phone,
            full_name=EXCLUDED.full_name,date_of_birth=EXCLUDED.date_of_birth
        RETURNING raw_customer_id::text''', (batch_id,user,name,email,phone,
                                            full_name,dob))[0][0]
    row = q(c, 'SELECT standardized_record_id::text FROM staging.customer_profile WHERE raw_customer_id=%s', (raw,))
    if row:
        q(c, '''UPDATE staging.customer_profile SET batch_id=%s,source_system_id=1,
            source_customer_id=%s,first_name_raw=%s,first_name_normalized=%s,
            email_normalized=%s,phone_normalized=%s,
            full_name_normalized=%s,date_of_birth=%s,
            email_is_valid=%s,phone_is_valid=true,
            standardization_status='READY',normalization_version='v1'
            WHERE raw_customer_id=%s''', (batch_id, user, name, name.lower(),
                                         email, phone,full_name.casefold() if full_name else None,
                                         dob,'@' in email if email else False,raw))
    else:
        q(c, '''INSERT INTO staging.customer_profile(raw_customer_id,batch_id,
            source_system_id,source_customer_id,first_name_raw,first_name_normalized,
            email_normalized,phone_normalized,full_name_normalized,date_of_birth,
            email_is_valid,phone_is_valid,
            standardization_status,normalization_version)
            VALUES(%s,%s,1,%s,%s,%s,%s,%s,%s,%s,%s,true,'READY','v1')''',
          (raw, batch_id, user, name, name.lower(), email, phone,
           full_name.casefold() if full_name else None,dob,
           '@' in email if email else False))
    q(c, '''INSERT INTO staging.customer_profile_history
        SELECT * FROM staging.customer_profile WHERE raw_customer_id=%s''', (raw,))
    assert q(c, 'SELECT app_id FROM raw.customer WHERE batch_id=%s AND raw_customer_id=%s',
             (batch_id, raw)) == [(app,)]
    return raw


def published_tfg(c, tfg_batch, raw):
    run, workspace = base.prepare(c, [tfg_batch], app=TFG)
    old = base.record(c, run, workspace, tfg_batch, raw, user='same-user', app=TFG)
    base.finish(c, run)
    q(c, "UPDATE control.customer_identity_transfer SET status='PUBLISHED' WHERE transfer_id=%s", (run,))
    return run, old['customer_entity_id']


def prepare(c, tfg_batch, target_batch, tfg_raw, target_raw, tfg_entity, *,
            target_entity=None, include_normalized=True, include_tfg=True, selected=1,
            pair=False, extra_tfg=None):
    run = str(uuid4())
    target_entity = target_entity or str(uuid4())
    workspace = q(c, '''INSERT INTO control.customer_identity_upload_scope(app_id)
        VALUES(%s) ON CONFLICT DO NOTHING RETURNING workspace_id::text''', (TARGET,))
    workspace = workspace[0][0] if workspace else q(c,
        'SELECT workspace_id::text FROM control.customer_identity_upload_scope WHERE app_id=%s',
        (TARGET,))[0][0]
    tfg_workspace = q(c,
        'SELECT workspace_id::text FROM control.customer_identity_upload_scope WHERE app_id=%s',
        (TFG,))[0][0]
    q(c, '''INSERT INTO identity.resolution_run(run_id,status,algorithm,algorithm_version,
        normalization_version,match_threshold,source_record_count,selected_record_count,
        standardized_record_count,entity_count,metrics)
        VALUES(%s,'RUNNING','splink','test','global-historical-v1',0.9,%s,%s,%s,%s,'{}')''',
        (run, 3 if extra_tfg else 2, selected, 3 if extra_tfg else 2, 2))
    baseline = q(c, 'SELECT coalesce(max(publication_seq),0) FROM control.customer_identity_transfer')[0][0]
    q(c, '''INSERT INTO control.customer_global_run(run_id,target_batch_id,target_app_id,
        workspace_id,expected_users,expected_members,input_sha256,cutoff,
        baseline_publication_seq,config)
        VALUES(%s,%s,%s,%s,%s,%s,%s,'2026-09-19T00:00:00Z',%s,'{}')''',
        (run, target_batch, TARGET, workspace, selected, 3 if extra_tfg else 2,
         'a'*64, baseline))
    for batch_id, app in ((tfg_batch, TFG), (target_batch, TARGET)):
        if app == TFG and not include_tfg:
            continue
        q(c, '''INSERT INTO control.customer_global_batch(run_id,batch_id,app_id,
            source_system_id,batch_created_at,expected_members,raw_observations,
            normalized_observations)
            SELECT %s,b.batch_id,b.app_id,b.source_system_id,b.created_at,%s,%s,%s
            FROM control.ingestion_batch b WHERE b.batch_id=%s''',
            (run, *([2]*3 if app==TFG and extra_tfg else [1]*3),batch_id))
    members = []
    for batch_id, app, raw, entity, ws, name in (
            (tfg_batch, TFG, tfg_raw, tfg_entity, tfg_workspace, 'Old'),
            (target_batch, TARGET, target_raw, target_entity, workspace, 'New')):
        if app == TFG and not include_tfg:
            continue
        evidence = q(c, '''SELECT h.snapshot_id::text,s.standardized_record_id::text,
            encode(digest(to_jsonb(h)::text,'sha256'),'hex'),
            encode(digest(to_jsonb(s)::text,'sha256'),'hex')
            FROM raw.customer h JOIN staging.customer_profile_history s
              ON (s.batch_id,s.raw_customer_id)=(h.batch_id,h.raw_customer_id)
            WHERE h.batch_id=%s AND h.raw_customer_id=%s''', (batch_id, raw))[0]
        member_id = str(uuid4())
        q(c, '''INSERT INTO control.customer_global_member
            (run_id,app_id,workspace_id,source_system_id,user_id,raw_customer_id,
             source_batch_id,customer_entity_id,match_probability,match_method,record_id,
             raw_snapshot_id,typed_standardized_record_id,raw_sha256,typed_sha256)
            VALUES(%s,%s,%s,1,'same-user',%s,%s,%s,1,'singleton',%s,%s,%s,%s,%s)''',
            (run, app, ws, raw, batch_id, entity, member_id, *evidence))
        members.append((app, member_id))
    target_member_id = next(member_id for app, member_id in members if app == TARGET)
    if extra_tfg:
        raw, user = extra_tfg
        snapshot,standard,raw_hash,typed_hash = q(c,'''SELECT h.snapshot_id::text,
            s.standardized_record_id::text,
            encode(digest(to_jsonb(h)::text,'sha256'),'hex'),
            encode(digest(to_jsonb(s)::text,'sha256'),'hex')
            FROM raw.customer h JOIN staging.customer_profile_history s
            ON (s.batch_id,s.raw_customer_id)=(h.batch_id,h.raw_customer_id)
            WHERE h.batch_id=%s AND h.raw_customer_id=%s''',(tfg_batch,raw))[0]
        extra_id=str(uuid4())
        q(c,'''INSERT INTO control.customer_global_member(run_id,app_id,workspace_id,
            source_system_id,user_id,raw_customer_id,source_batch_id,customer_entity_id,
            match_probability,match_method,record_id,raw_snapshot_id,
            typed_standardized_record_id,raw_sha256,typed_sha256)
            VALUES(%s,%s,%s,1,%s,%s,%s,%s,0.95,'deterministic',%s,%s,%s,%s,%s)''',
            (run,TFG,tfg_workspace,user,raw,tfg_batch,tfg_entity,extra_id,snapshot,
             standard,raw_hash,typed_hash))
        first_id=next(member_id for app,member_id in members if app == TFG)
        left,right=sorted((extra_id,first_id))
        q(c,'''INSERT INTO control.customer_global_pair(run_id,left_record_id,
            right_record_id,probability,method,evidence)
            VALUES(%s,%s,%s,0.95,'deterministic','{}')''',(run,left,right))
        q(c,'''UPDATE identity.resolution_run SET accepted_pair_count=1 WHERE run_id=%s''',(run,))
    payload = dict(app_id=TARGET,user_id='same-user',customer_entity_id=target_entity,
        match_probability=1.0,match_method='singleton',source_system_id=1,
        standardized_record_id=target_member_id,raw_customer_id=target_raw,
        resolution_run_id=run,workspace_id=workspace,
        profile=dict(customer_id='same-user',first_name='New',middle_name=None,
            last_name=None,full_name=None,email=None,phone=None,address_line_1=None,
            district=None,province=None,postal_code=None,country_code=None,
            date_of_birth=None,gender=None,customer_key_id=None,
            profile_source_record_id=None,source_created_at=None,source_updated_at=None,
            profile_updated_at='2026-09-01T00:00:00Z',
            standardized_at='2026-09-01T00:00:00Z',
            first_seen_at='2026-09-01T00:00:00Z',
            last_seen_at='2026-09-01T00:00:00Z'))
    input_payload = {'record_id':target_member_id,'raw':{'batch_id':target_batch,
        'raw_customer_id':target_raw,'app_id':TARGET,'source_system_id':1,
        'source_customer_id':'same-user','first_name':'New'},
        'normalized':{'first_name_normalized':'new'} if include_normalized else {}}
    q(c, '''INSERT INTO control.customer_global_record(run_id,row_number,app_id,
        source_system_id,user_id,raw_customer_id,source_batch_id,input_payload,payload)
        VALUES(%s,1,%s,1,'same-user',%s,%s,%s,%s)''',
        (run, TARGET, target_raw, target_batch, Json(input_payload), Json(payload)))
    if pair:
        tfg_member_id = next(member_id for app, member_id in members if app == TFG)
        left,right = sorted((target_member_id,tfg_member_id))
        q(c, '''INSERT INTO control.customer_global_pair
            (run_id,left_record_id,right_record_id,probability,method,evidence)
            VALUES(%s,%s,%s,0.95,'deterministic',%s)''',
          (run,left,right,Json({'independent_match':'email+phone'})))
        q(c, 'UPDATE identity.resolution_run SET candidate_pair_count=1,accepted_pair_count=1,entity_count=1 WHERE run_id=%s', (run,))
    return run, payload


def state(c):
    return {table: q(c, f'''SELECT count(*),md5(coalesce(string_agg(md5(to_jsonb(t)::text),E'\\n'
        ORDER BY md5(to_jsonb(t)::text) COLLATE "C"),'')) FROM {table} t''') for table in (
        'raw.customer','raw.customer_batch_member','raw.customer_current',
        'staging.customer_profile','staging.customer_profile_history',
        'identity.customer_entity','identity.customer_entity_member','identity.match_pair',
        'control.stitching_job')}


def scenario(c):
    install(c)
    tfg = batch(c, TFG, created='2026-09-01T00:00:00Z')
    old = observe(c, tfg, TFG, email='shared@example.com')
    prior, entity = published_tfg(c, tfg, old)
    target = batch(c, TARGET, created='2026-09-02T00:00:00Z')
    new = observe(c, target, TARGET, name='New', email='shared@example.com')
    assert old == new  # legacy cross-app, cross-batch raw UUID alias
    return tfg, target, old, new, prior, entity

def test_completed_connector_batch_is_not_an_upload_history_manifest(db, owned_database):
    from identity_stitcher.global_historical import extract_history
    tfg,target, *_ = scenario(db)
    connector = batch(db, TFG, created='2026-09-03T00:00:00Z')
    q(db, '''UPDATE control.ingestion_batch SET metadata=jsonb_build_object(
        'connector_sync_run_id',%s::text) WHERE batch_id=%s''',(str(uuid4()), connector))
    # Simulate the newer connector's stored app lineage without changing the
    # producer's pinned trigger. The explicit source discriminator still excludes it.
    q(db, 'ALTER TABLE control.ingestion_batch DISABLE TRIGGER USER')
    q(db, 'UPDATE control.ingestion_batch SET app_id=%s WHERE batch_id=%s',(TFG,connector))
    q(db, 'ALTER TABLE control.ingestion_batch ENABLE TRIGGER USER')
    assert q(db,'SELECT status,app_id FROM control.ingestion_batch WHERE batch_id=%s',(connector,)) == [('completed',TFG)]
    assert q(db,'SELECT error_code FROM control.customer_app_scope_status(%s)',(connector,)) == [('CONNECTOR_EXCLUDED',)]
    q(db, 'INSERT INTO control.customer_identity_upload_scope(app_id) VALUES(%s) ON CONFLICT DO NOTHING', (TARGET,))
    db.commit()
    reader=psycopg2.connect(fixture.ENDPOINT,dbname=owned_database)
    reader.set_session(readonly=True,isolation_level='REPEATABLE READ')
    try:
        batches,observations,selected,_,_=extract_history(reader,target,TARGET,
                            datetime(2026,9,19,tzinfo=timezone.utc),max_members=10)
        assert {b['batch_id'] for b in batches} == {tfg,target}
        assert len(observations)==2 and selected==1
    finally:reader.close()


def test_connector_batch_not_required_by_global_finalizer_and_published_keys_untouched(db):
    tfg,target,old,new,prior,entity = scenario(db)
    connector = batch(db, TFG, created='2026-09-03T00:00:00Z')
    q(db, '''UPDATE control.ingestion_batch SET metadata=jsonb_build_object(
        'connector_sync_run_id',%s::text) WHERE batch_id=%s''',(str(uuid4()),connector))
    # This fixture pins 026's upload-only scope; the real 045 connector route
    # stores app_id, whereas this disposable DB's older trigger does not.
    q(db, 'ALTER TABLE control.ingestion_batch DISABLE TRIGGER USER')
    q(db, 'UPDATE control.ingestion_batch SET app_id=%s WHERE batch_id=%s',(TFG,connector))
    q(db, 'ALTER TABLE control.ingestion_batch ENABLE TRIGGER USER')
    assert q(db, 'SELECT error_code FROM control.customer_app_scope_status(%s)',
             (connector,)) == [('CONNECTOR_EXCLUDED',)]
    run,_ = prepare(db,tfg,target,old,new,entity)
    q(db, 'SELECT control.finish_customer_global_run(%s)',(run,))
    db.commit()
    assert q(db,'''SELECT status,row_count FROM control.customer_identity_transfer
        WHERE global_run_id=%s''',(run,)) == [('PENDING',1)]
    assert q(db,'''SELECT status FROM control.customer_identity_transfer
        WHERE transfer_id=%s''',(prior,)) == [('PUBLISHED',)]


def test_real_executor_creates_fresh_pending_transfer_from_cross_app_typed_match(db, owned_database):
    """Actual matcher, read-only PG extraction, finalizer; CH inventory is fake."""
    import hashlib
    from identity_stitcher.config import AppConfig, DatabaseConfig
    from identity_stitcher.global_historical import execute_historical
    from identity_stitcher.publication import canonical_payload
    tfg,target,old,new,prior,entity = scenario(db)
    source = q(db, '''SELECT e.payload,t.workspace_id::text,t.publication_seq,t.row_count
        FROM control.customer_identity_export e
        JOIN control.customer_identity_transfer t USING(transfer_id)
        WHERE e.transfer_id=%s''', (prior,))[0]
    payload,workspace,seq,total = source
    encoded = canonical_payload(payload)
    digest = hashlib.sha256((encoded+'\n').encode()).hexdigest()
    q(db, 'UPDATE control.customer_identity_transfer SET source_sha256=%s WHERE transfer_id=%s',
      (digest,prior))
    q(db, '''INSERT INTO control.customer_identity_upload_scope(app_id)
        VALUES(%s) ON CONFLICT DO NOTHING''', (TARGET,))
    db.commit()
    assert set(q(db, '''SELECT b.batch_id::text,b.app_id,b.status,
        count(m.raw_customer_id),count(h.raw_customer_id),count(s.raw_customer_id),
        scope.app_id,scope.error_code
        FROM control.ingestion_batch b
        CROSS JOIN LATERAL control.customer_app_scope_status(b.batch_id) scope
        LEFT JOIN raw.customer_batch_member m ON m.ingestion_batch_id=b.batch_id
        LEFT JOIN raw.customer h ON (h.batch_id,h.raw_customer_id,h.source_system_id,h.source_customer_id)=
            (m.ingestion_batch_id,m.raw_customer_id,m.source_system_id,m.source_customer_id)
        LEFT JOIN staging.customer_profile_history s ON (s.batch_id,s.raw_customer_id,
            s.source_system_id,s.source_customer_id)=(m.ingestion_batch_id,m.raw_customer_id,
            m.source_system_id,m.source_customer_id)
        WHERE b.batch_id IN (%s,%s) GROUP BY b.batch_id,scope.app_id,scope.error_code''',
        (tfg,target))) == {(target,TARGET,'completed',1,1,1,TARGET,None),
                          (tfg,TFG,'completed',1,1,1,TFG,None)}
    marker = dict(transfer_id=prior,resolution_run_id=prior,workspace_id=workspace,
                  app_id=TFG,publication_seq=seq,row_count=total,source_sha256=digest)
    version = dict(transfer_id=prior,resolution_run_id=prior,workspace_id=workspace,
                   app_id=TFG,user_id='same-user',publication_seq=seq,row_number=1,
                   payload=encoded)
    class CommittedClickHouse:
        database = 'cdp_customer'
        def iter_rows(self, sql):
            if '.identity_profile_versions' in sql:
                return iter((version,))
            if '.identity_publications' in sql:
                return iter((marker,))
            raise AssertionError('unexpected CH query')
    reader = psycopg2.connect(fixture.ENDPOINT, dbname=owned_database)
    try:
        reader.set_session(isolation_level='REPEATABLE READ', readonly=True)
        run = str(uuid4())
        result = execute_historical(db, reader, CommittedClickHouse(),
            AppConfig(DatabaseConfig('127.0.0.1',55441,owned_database,'postgres','','disable'),
                      duckdb_memory_limit='256MB',duckdb_threads=1),
            target_batch=target,target_app=TARGET,run_id=run,
            cutoff=datetime(2026,9,19,tzinfo=timezone.utc),max_members=10,
            max_published_rows=10)
    finally:
        reader.close()
    assert result['status'] == 'SUCCEEDED'
    assert q(db, '''SELECT status,row_count,member_count FROM control.customer_identity_transfer
        WHERE global_run_id=%s''', (run,)) == [('PENDING',1,2)]
    assert q(db, '''SELECT payload->>'customer_entity_id' FROM control.customer_identity_export
        WHERE transfer_id=%s''', (run,)) == [(entity,)]
    assert q(db, 'SELECT count(*) FROM control.customer_global_pair WHERE run_id=%s', (run,)) == [(1,)]
    # Exercise the actual publisher source/member gates against this committed
    # PostgreSQL outbox before any ClickHouse side effects.
    from identity_stitcher.publication import PublicationWorker
    publisher = PublicationWorker.__new__(PublicationWorker)
    publisher.conn = db
    publisher.batch_rows = 10
    transfer = q(db, '''SELECT transfer_id::text,resolution_run_id::text,
        global_run_id::text,publication_seq,row_count,member_count
        FROM control.customer_identity_transfer WHERE global_run_id=%s''',(run,))[0]
    keys = ('transfer_id','resolution_run_id','global_run_id','publication_seq',
            'row_count','member_count')
    metadata = dict(zip(keys,transfer,strict=True))
    sources = list(publisher._source(metadata))
    members = list(publisher._member_rows(metadata))
    assert len(sources) == 1
    assert len(members) == 2
    assert sources[0]['user_id'] == 'same-user'
    assert publisher._source_hash == hashlib.sha256(
        (canonical_payload(q(db,'''SELECT payload FROM control.customer_identity_export
            WHERE transfer_id=%s''',(run,))[0][0])+'\n').encode()).hexdigest()
    assert {member['app_id'] for member in members} == {TFG,TARGET}


def test_genuine_cross_app_pair_can_attach_target_to_published_tfg_entity(db):
    tfg,target,old,new,prior,entity = scenario(db)
    run,payload = prepare(db,tfg,target,old,new,entity,target_entity=entity,pair=True)
    q(db,'SELECT control.finish_customer_global_run(%s)',(run,))
    db.commit()
    assert q(db,'SELECT status,member_count,row_count FROM control.customer_identity_transfer WHERE global_run_id=%s',(run,)) == [('PENDING',2,1)]
    assert q(db,'SELECT payload->>\'customer_entity_id\' FROM control.customer_identity_export WHERE transfer_id=%s',(run,)) == [(entity,)]
    assert q(db,'SELECT count(*) FROM control.customer_global_pair WHERE run_id=%s',(run,)) == [(1,)]
    assert q(db,'SELECT payload->>\'customer_entity_id\' FROM control.customer_identity_export WHERE transfer_id=%s',(prior,)) == [(entity,)]


def test_prior_tfg_version_can_differ_but_current_published_entity_is_preserved(db):
    tfg,target,old,new,first,retired = scenario(db)
    second, workspace = base.prepare(db, [tfg], app=TFG,
        baseline=q(db,'SELECT max(publication_seq) FROM control.customer_identity_transfer')[0][0])
    current = base.record(db, second, workspace, tfg, old, app=TFG)
    assert current['customer_entity_id'] != retired
    base.finish(db, second)
    q(db, "UPDATE control.customer_identity_transfer SET status='PUBLISHED' WHERE transfer_id=%s", (second,))
    run,_ = prepare(db,tfg,target,old,new,current['customer_entity_id'],
                    target_entity=current['customer_entity_id'],pair=True)
    q(db,'SELECT control.finish_customer_global_run(%s)',(run,))
    db.commit()
    assert q(db,'''SELECT payload->>'customer_entity_id'
        FROM control.customer_identity_export WHERE transfer_id=%s''',(first,)) == [(retired,)]
    assert q(db,'''SELECT payload->>'customer_entity_id'
        FROM control.customer_identity_export WHERE transfer_id=%s''',(second,)) == [(current['customer_entity_id'],)]
    assert q(db,'''SELECT payload->>'customer_entity_id'
        FROM control.customer_identity_export WHERE transfer_id=%s''',(run,)) == [(current['customer_entity_id'],)]


def test_same_app_accepted_edge_does_not_require_cross_app_anchors(db):
    tfg,target,old,new,prior,entity = scenario(db)
    another = observe(db,tfg,TFG,user='another',email='different@example.test',
                      phone='+66810000000')
    run,_ = prepare(db,tfg,target,old,new,entity,extra_tfg=(another,'another'))
    q(db,'SELECT control.finish_customer_global_run(%s)',(run,))
    db.commit()
    assert q(db,'''SELECT status,row_count,member_count FROM control.customer_identity_transfer
        WHERE global_run_id=%s''',(run,)) == [('PENDING',1,3)]

def test_phone_full_name_dob_pair_without_email_preserves_tfg_publication(db):
    install(db)
    tfg = batch(db, TFG, created='2026-09-01T00:00:00Z')
    old = observe(db,tfg,TFG,email='not-an-email',phone='+66812345678',
                  full_name='Shared Person',dob='1990-01-01')
    prior,entity = published_tfg(db,tfg,old)
    target = batch(db,TARGET,created='2026-09-02T00:00:00Z')
    new = observe(db,target,TARGET,name='New',email='not-an-email',
                  phone='+66812345678',full_name='Shared Person',dob='1990-01-01')
    run,_ = prepare(db,tfg,target,old,new,entity,target_entity=entity,pair=True)
    q(db,'SELECT control.finish_customer_global_run(%s)',(run,))
    db.commit()
    assert q(db,'''SELECT status,row_count,member_count FROM control.customer_identity_transfer
        WHERE global_run_id=%s''',(run,)) == [('PENDING',1,2)]
    assert q(db,'''SELECT payload->>'customer_entity_id' FROM control.customer_identity_export
        WHERE transfer_id=%s''',(run,)) == [(entity,)]
    assert q(db,'''SELECT payload->>'customer_entity_id' FROM control.customer_identity_export
        WHERE transfer_id=%s''',(prior,)) == [(entity,)]

def test_complete_insufficient_identifier_history_still_keeps_singleton(db):
    tfg,target,old,new,prior,entity = scenario(db)
    q(db,'ALTER TABLE staging.customer_profile_history DISABLE TRIGGER USER')
    q(db,'''UPDATE staging.customer_profile_history
        SET standardization_status='INSUFFICIENT_IDENTIFIERS',
            email_is_valid=false,phone_is_valid=false
        WHERE batch_id=%s''',(target,))
    q(db,'ALTER TABLE staging.customer_profile_history ENABLE TRIGGER USER')
    run,_ = prepare(db,tfg,target,old,new,entity)
    q(db,'SELECT control.finish_customer_global_run(%s)',(run,))
    db.commit()
    assert q(db,'SELECT status FROM control.customer_identity_transfer WHERE global_run_id=%s',
             (run,)) == [('PENDING',)]


def test_deterministic_edge_is_not_a_splink_candidate(db):
    tfg,target,old,new,prior,entity = scenario(db)
    run,_ = prepare(db,tfg,target,old,new,entity,target_entity=entity,pair=True)
    q(db, 'UPDATE identity.resolution_run SET candidate_pair_count=0 WHERE run_id=%s', (run,))
    q(db, 'SELECT control.finish_customer_global_run(%s)', (run,))
    db.commit()
    assert q(db, '''SELECT status FROM control.customer_identity_transfer
        WHERE global_run_id=%s''', (run,)) == [('PENDING',)]


def test_forged_cross_app_pair_with_only_shared_raw_uuid_fails_closed(db):
    tfg,target,old,new,prior,entity = scenario(db)
    q(db, 'ALTER TABLE staging.customer_profile_history DISABLE TRIGGER USER')
    q(db, '''UPDATE staging.customer_profile_history
        SET email_normalized='unrelated@example.test',phone_normalized='+66800000000'
        WHERE batch_id=%s''', (target,))
    q(db, 'ALTER TABLE staging.customer_profile_history ENABLE TRIGGER USER')
    run,_ = prepare(db,tfg,target,old,new,entity,target_entity=entity,pair=True)
    db.commit()
    with pytest.raises(psycopg2.Error, match='GLOBAL_CROSS_APP_PAIR_UNPROVEN'):
        q(db,'SELECT control.finish_customer_global_run(%s)',(run,))
    db.rollback()
    assert q(db,'SELECT count(*) FROM control.customer_identity_transfer WHERE global_run_id=%s',(run,)) == [(0,)]


def test_shared_raw_entity_alias_without_independent_pair_fails_closed(db):
    tfg,target,old,new,prior,entity = scenario(db)
    run,_ = prepare(db,tfg,target,old,new,entity,target_entity=entity)
    db.commit()
    with pytest.raises(psycopg2.Error,match='GLOBAL_CROSS_APP_ENTITY_UNPROVEN'):
        q(db,'SELECT control.finish_customer_global_run(%s)',(run,))
    db.rollback()
    assert q(db,'SELECT count(*) FROM control.customer_identity_transfer WHERE global_run_id=%s',(run,)) == [(0,)]


def test_equal_entity_with_pair_but_only_raw_uuid_evidence_rejects(db):
    tfg,target,old,new,prior,entity = scenario(db)
    run,_ = prepare(db,tfg,target,old,new,entity,target_entity=entity,pair=True)
    q(db,'ALTER TABLE staging.customer_profile_history DISABLE TRIGGER USER')
    q(db, "UPDATE staging.customer_profile_history SET email_normalized=NULL WHERE batch_id=%s",(target,))
    q(db,'ALTER TABLE staging.customer_profile_history ENABLE TRIGGER USER')
    db.commit()
    with pytest.raises(psycopg2.Error,match='GLOBAL_'):
        q(db,'SELECT control.finish_customer_global_run(%s)',(run,))
    db.rollback()


def test_shared_raw_different_entities_target_exports_only_and_preserves_tfg(db):
    tfg, target, old, new, prior, tfg_entity = scenario(db)
    run, payload = prepare(db, tfg, target, old, new, tfg_entity)
    before = state(db)
    prior_export = q(db, 'SELECT to_jsonb(e) FROM control.customer_identity_export e WHERE transfer_id=%s', (prior,))
    q(db, 'SELECT control.finish_customer_global_run(%s)', (run,))
    db.commit()
    assert q(db, '''SELECT status,row_count,member_count,selected_row_count,app_id,
        global_run_id::text,backfill_run_id,stitching_job_id FROM control.customer_identity_transfer
        WHERE transfer_id=%s''', (run,)) == [
        ('PENDING',1,2,1,TARGET,run,None,None)]
    assert q(db, '''SELECT app_id,user_id,payload->>'customer_entity_id'
        FROM control.customer_identity_export WHERE transfer_id=%s''', (run,)) == [
        (TARGET,'same-user',payload['customer_entity_id'])]
    assert set(q(db, 'SELECT app_id,customer_entity_id::text FROM control.customer_global_member WHERE run_id=%s', (run,))) == {
        (TFG,tfg_entity),(TARGET,payload['customer_entity_id'])}
    assert q(db, 'SELECT to_jsonb(e) FROM control.customer_identity_export e WHERE transfer_id=%s', (prior,)) == prior_export
    assert state(db) == before
    q(db, 'SELECT control.finish_customer_global_run(%s)', (run,))
    assert q(db, 'SELECT count(*) FROM control.customer_identity_transfer WHERE global_run_id=%s', (run,)) == [(1,)]


@pytest.mark.parametrize('fault', ['tfg_repoint','no_tfg_batch','bad_raw_hash',
                                   'missing_typed','bad_normalized','selected_count',
                                   'wrong_payload_entity','extra_export'])
def test_invalid_or_incomplete_proof_rolls_back_without_outbox(db, fault):
    tfg, target, old, new, prior, entity = scenario(db)
    run, payload = prepare(db,tfg,target,old,new,entity,
                           include_tfg=(fault!='no_tfg_batch'),
                           include_normalized=(fault!='bad_normalized'),
                           selected=2 if fault=='selected_count' else 1)
    if fault == 'tfg_repoint':
        q(db, 'ALTER TABLE control.customer_global_member DISABLE TRIGGER USER')
        q(db, 'UPDATE control.customer_global_member SET customer_entity_id=%s WHERE run_id=%s AND app_id=%s',
          (str(uuid4()), run, TFG))
        q(db, 'ALTER TABLE control.customer_global_member ENABLE TRIGGER USER')
    if fault == 'bad_raw_hash':
        q(db, 'ALTER TABLE control.customer_global_member DISABLE TRIGGER USER')
        q(db, "UPDATE control.customer_global_member SET raw_sha256=%s WHERE run_id=%s AND app_id=%s", ('b'*64,run,TARGET))
        q(db, 'ALTER TABLE control.customer_global_member ENABLE TRIGGER USER')
    if fault == 'missing_typed':
        q(db, 'ALTER TABLE staging.customer_profile_history DISABLE TRIGGER USER')
        q(db, 'DELETE FROM staging.customer_profile_history WHERE batch_id=%s', (target,))
        q(db, 'ALTER TABLE staging.customer_profile_history ENABLE TRIGGER USER')
    if fault == 'wrong_payload_entity':
        q(db, 'ALTER TABLE control.customer_global_record DISABLE TRIGGER USER')
        q(db, "UPDATE control.customer_global_record SET payload=jsonb_set(payload,'{customer_entity_id}',to_jsonb(%s::text)) WHERE run_id=%s",
          (str(uuid4()),run))
        q(db, 'ALTER TABLE control.customer_global_record ENABLE TRIGGER USER')
    if fault == 'extra_export':
        q(db, 'ALTER TABLE control.customer_global_record DISABLE TRIGGER USER')
        q(db, "UPDATE control.customer_global_record SET payload=jsonb_set(payload,'{app_id}',to_jsonb(%s::text)) WHERE run_id=%s",
          (TFG,run))
        q(db, 'ALTER TABLE control.customer_global_record ENABLE TRIGGER USER')
    before = state(db)
    db.commit()
    with pytest.raises(psycopg2.Error, match='GLOBAL_'):
        q(db,'SELECT control.finish_customer_global_run(%s)', (run,))
    db.rollback()
    assert q(db,'SELECT status FROM control.customer_global_run WHERE run_id=%s',(run,)) == [('BUILDING',)]
    assert q(db,'SELECT status FROM identity.resolution_run WHERE run_id=%s',(run,)) == [('RUNNING',)]
    assert q(db,'SELECT count(*) FROM control.customer_identity_transfer WHERE global_run_id=%s',(run,)) == [(0,)]
    assert state(db) == before


def test_no_direct_member_or_export_append_and_no_rewrite_after_finalization(db):
    tfg,target,old,new,prior,entity = scenario(db)
    run,_ = prepare(db,tfg,target,old,new,entity)
    q(db,'SELECT control.finish_customer_global_run(%s)',(run,))
    db.commit()
    for table in ('customer_global_run','customer_global_batch','customer_global_member',
                  'customer_global_record','customer_identity_export'):
        with pytest.raises(psycopg2.Error):
            q(db,f'DELETE FROM control.{table} WHERE '+('run_id=%s' if table!='customer_identity_export' else 'transfer_id=%s'),(run,))
        db.rollback()
    with pytest.raises(psycopg2.Error):
        q(db, '''INSERT INTO control.customer_global_member
           SELECT * FROM control.customer_global_member WHERE run_id=%s''',(run,))
    db.rollback()


def test_second_app_publication_after_pinned_baseline_rejects_stale_run(db):
    tfg,target,old,new,prior,entity = scenario(db)
    run,_ = prepare(db,tfg,target,old,new,entity)
    db.commit()
    # New, independent prior PENDING target publication takes a later sequence.
    newer = str(uuid4())
    q(db, '''INSERT INTO identity.resolution_run(run_id,status,normalization_version,match_threshold)
        VALUES(%s,'RUNNING','fixture',0.9)''',(newer,))
    q(db, '''INSERT INTO control.customer_app_backfill_run(run_id,app_id,workspace_id,
        expected_users,input_sha256,cutoff,baseline_publication_seq,config)
        SELECT %s,target_app_id,workspace_id,0,%s,cutoff,baseline_publication_seq,'{}'
        FROM control.customer_global_run WHERE run_id=%s''',(newer,'b'*64,run))
    # Inject another transfer through the legitimate 027 path, with an empty manifest
    # impossible for nonempty target. Instead a synthetic outbox row is an isolated
    # database-only concurrent-publication marker (not production behavior).
    q(db,'''INSERT INTO control.customer_identity_transfer
        (transfer_id,resolution_run_id,backfill_run_id,workspace_id,app_id,status,row_count)
        SELECT %s,%s,%s,workspace_id,target_app_id,'PENDING',0
        FROM control.customer_global_run WHERE run_id=%s''',(newer,newer,newer,run))
    db.commit()
    with pytest.raises(psycopg2.Error,match='GLOBAL_STALE_PUBLICATION'):
        q(db,'SELECT control.finish_customer_global_run(%s)',(run,))
    db.rollback()
    assert q(db,'SELECT count(*) FROM control.customer_identity_transfer WHERE global_run_id=%s',(run,)) == [(0,)]
