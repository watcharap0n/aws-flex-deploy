"""052 coordinated activation against disposable localhost PostgreSQL 55441.

Never points at a deployed database. Exercises actual 019/022/038/050/051 SQL.
"""
from pathlib import Path
from uuid import uuid4
import sys

import psycopg2
import pytest
import test_customer_forward_source_namespace as forward
import test_customer_logical_member_cutover_050 as cutover
import test_customer_global_upload_capture as base

ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = ROOT / 'database/migrations'
db = forward.db


def install(conn, filename):
    path = MIGRATIONS / filename
    assert path.is_file(), f'missing real migration: {filename}'
    base.query(conn, path.read_text())
    conn.commit()


def chain(conn, *, legacy_manual: bool | str=False):
    raw, old_a, old_b = forward.old_alias(conn)
    # Exact historical typed snapshots for both literal apps, before 050.
    for batch in (old_a, old_b):
        base.query(conn, '''INSERT INTO staging.customer_profile_history
            (standardized_record_id,raw_customer_id,batch_id,source_system_id,source_customer_id,
             first_name_raw,standardization_status,standardized_at)
            SELECT gen_random_uuid(),h.raw_customer_id,h.batch_id,h.source_system_id,
                   h.source_customer_id,h.first_name,'READY',clock_timestamp()
            FROM raw.customer h WHERE h.batch_id=%s''', (batch,))
    base.query(conn, '''INSERT INTO staging.customer_profile
        (raw_customer_id,batch_id,source_system_id,source_customer_id,first_name_raw)
        SELECT raw_customer_id,batch_id,source_system_id,source_customer_id,first_name
        FROM raw.customer WHERE batch_id=%s''', (old_b,))
    # 050 requires active standardized ID match exact historical typed row.
    base.query(conn, '''UPDATE staging.customer_profile p SET standardized_record_id=h.standardized_record_id
        FROM staging.customer_profile_history h WHERE h.batch_id=p.batch_id
        AND h.raw_customer_id=p.raw_customer_id''')
    conn.commit()
    if legacy_manual:
        # These rows must predate 038 and its file-admission trigger.
        base.query(conn, "INSERT INTO control.source_system(source_system_id,source_code,source_name) VALUES (2,'manual_upload','manual upload')")
        for ref in ('legacy-manual-upload', 'legacy-manual-upload-2'):
            manual = base.batch(conn, forward.A, source=2, ref=ref)
            base.query(conn, "UPDATE control.source_landing_batch SET source_code='manual_upload' WHERE batch_id=%s", (manual['landing'],))
            own = base.member(conn, manual, ref)
            base.query(conn, '''INSERT INTO staging.customer_profile_history
                SELECT * FROM staging.customer_profile WHERE standardized_record_id=%s''',
                (own['standard'],))
            assert base.one(conn, '''SELECT count(*) FROM connector.sync_run
                WHERE ingestion_batch_id=%s''', (manual['batch'],)) == 0
        if legacy_manual == 'orphan_raw':
            batch = base.one(conn, "SELECT batch_id FROM control.ingestion_batch WHERE batch_ref='legacy-manual-upload'")
            base.query(conn, '''INSERT INTO raw.customer_current(raw_customer_id,batch_id,source_system_id,source_customer_id)
                VALUES (%s,%s,2,'orphan')''', (str(uuid4()),batch))
        conn.commit()
    # Match the live connector-aware 040/045 catalog used by 051.
    base.query(conn, 'ALTER TABLE platform.workspace ADD COLUMN app_id text')
    base.query(conn, "SELECT set_config('cdp.connector_owner_runtime','verified-owner-only',false)")
    install(conn, '040_connector_app_lineage.sql')
    install(conn, '041_global_member_bridge.sql')
    install(conn, '042_customer_capture_lock_retry.sql')
    install(conn, '044_customer_overlap_capture.sql')
    install(conn, '045_connector_identity_publication.sql')
    forward.install(conn)
    cutover.install(conn)
    install(conn, '051_customer_logical_capture.sql')
    return raw, old_a, old_b


def test_052_gate_requires_capture_proof_and_ordinary_forward_merge(db):
    raw, old_a, old_b = chain(db)
    before = base.query(db, '''SELECT batch_id,raw_customer_id,snapshot_id,app_id,first_name
        FROM raw.customer WHERE source_system_id=1 ORDER BY batch_id''')
    install(db, '052_customer_ordinary_activation.sql')
    fresh = forward.upload(db, forward.A)
    source = base.one(db, 'SELECT source_system_id FROM control.ingestion_batch WHERE batch_id=%s', (fresh,))
    sys.path.insert(0, str(ROOT / '_lambda/ingestion_data'))
    from customer_bulk import upsert_customers
    with db.cursor() as cur:
        assert upsert_customers(cur, [{'source_customer_id':'new-user','first_name':'New','status':'ACTIVE'}], fresh, source,
                                forward=True) == 1
    db.commit()
    assert base.one(db, '''SELECT count(*) FROM raw.customer h
        JOIN raw.customer_batch_member m ON m.ingestion_batch_id=h.batch_id
          AND m.raw_customer_id=h.raw_customer_id
        WHERE h.batch_id=%s AND h.app_id=%s AND m.source_system_id=%s''',
        (fresh,forward.A,source)) == 1
    assert base.query(db, '''SELECT batch_id,raw_customer_id,snapshot_id,app_id,first_name
        FROM raw.customer WHERE source_system_id=1 ORDER BY batch_id''') == before
    # 038 gate must not be removed: a demonstrated exact app/source/user
    # representation and publication preflight are required to enter RUNNING.
    job = str(uuid4())
    base.query(db, "INSERT INTO control.stitching_job(job_id,ingestion_batch_id,status) VALUES (%s,%s,'RUNNING')", (job,fresh))
    assert base.one(db,'SELECT status FROM control.stitching_job WHERE job_id=%s',(job,)) == 'RUNNING'
    assert base.one(db,'''SELECT count(*) FROM pg_trigger WHERE tgrelid='control.stitching_job'::regclass
        AND tgname='customer_forward_stitch_gate' AND tgenabled='O' ''') == 1


def test_052_ordinary_publication_has_exact_selected_lineage(db):
    chain(db)
    install(db, '052_customer_ordinary_activation.sql')
    fresh = forward.upload(db, forward.A)
    assert forward.stage_and_merge(db, fresh, 'published-new', 'Published') == 1
    raw = base.one(db,'SELECT raw_customer_id FROM raw.customer WHERE batch_id=%s',(fresh,))
    standard, entity, run, job = (str(uuid4()) for _ in range(4))
    base.query(db, '''INSERT INTO staging.customer_profile
        (standardized_record_id,raw_customer_id,batch_id,source_system_id,source_customer_id,
         first_name_raw,standardization_status,standardized_at)
        VALUES (%s,%s,%s,(SELECT source_system_id FROM control.ingestion_batch WHERE batch_id=%s),
                'published-new','Published','READY',clock_timestamp())''',
        (standard,raw,fresh,fresh))
    base.query(db, '''INSERT INTO staging.customer_profile_history
        SELECT * FROM staging.customer_profile WHERE standardized_record_id=%s''',(standard,))
    base.query(db,"INSERT INTO control.stitching_job(job_id,ingestion_batch_id,status) VALUES (%s,%s,'RUNNING')",(job,fresh))
    base.query(db,'''INSERT INTO identity.resolution_run
        (run_id,stitching_job_id,status,normalization_version,match_threshold)
        VALUES (%s,%s,'RUNNING','fixture',0.9)''',(run,job))
    base.query(db,'INSERT INTO identity.customer_entity(customer_entity_id,last_resolution_run_id,member_count) VALUES (%s,%s,1)',(entity,run))
    base.query(db,'''INSERT INTO identity.customer_entity_member
        (standardized_record_id,customer_entity_id,source_system_id,source_customer_id,match_method,resolution_run_id)
        VALUES (%s,%s,(SELECT source_system_id FROM control.ingestion_batch WHERE batch_id=%s),
                'published-new','singleton',%s)''',(standard,entity,fresh,run))
    db.commit()
    db.set_session(isolation_level='REPEATABLE READ')
    base.query(db,"UPDATE identity.resolution_run SET status='SUCCEEDED',completed_at=clock_timestamp() WHERE run_id=%s",(run,))
    base.query(db,"UPDATE control.stitching_job SET selected_record_count=1,member_record_count=1,status='SUCCEEDED',completed_at=clock_timestamp() WHERE job_id=%s",(job,))
    assert base.one(db,"SELECT status FROM control.customer_identity_transfer WHERE stitching_job_id=%s",(job,)) == 'PENDING'
    assert base.one(db,"SELECT payload->>'standardized_record_id' FROM control.customer_identity_export WHERE user_id='published-new'") == standard


def test_052_publication_rejects_unrepresented_selected_member(db):
    chain(db)
    install(db, '052_customer_ordinary_activation.sql')
    fresh = forward.upload(db, forward.A)
    assert forward.stage_and_merge(db, fresh, 'missing-profile', 'New') == 1
    job = str(uuid4())
    base.query(db, "INSERT INTO control.stitching_job(job_id,ingestion_batch_id,status) VALUES (%s,%s,'RUNNING')", (job,fresh))
    base.query(db,'SAVEPOINT missing_member')
    with pytest.raises(psycopg2.Error, match='052 publication requires exact selected normalized member'):
        base.query(db,"UPDATE control.stitching_job SET status='SUCCEEDED' WHERE job_id=%s",(job,))
    base.query(db,'ROLLBACK TO SAVEPOINT missing_member')
    assert base.one(db,'SELECT status FROM control.stitching_job WHERE job_id=%s',(job,)) == 'RUNNING'


def test_052_quarantines_exact_unscoped_manual_uploads_without_reassignment(db):
    chain(db, legacy_manual=True)
    before = base.query(db, '''SELECT h.batch_id,h.raw_customer_id,h.app_id,r.app_id,b.app_id
        FROM staging.customer_profile_history h JOIN raw.customer r
          ON (r.batch_id,r.raw_customer_id)=(h.batch_id,h.raw_customer_id)
        JOIN control.ingestion_batch b ON b.batch_id=h.batch_id
        WHERE h.app_id IS NULL ORDER BY h.batch_id''')
    assert len(before) == 2 and all(row[2:] == (None,None,None) for row in before)
    install(db, '052_customer_ordinary_activation.sql')
    assert base.query(db, '''SELECT h.batch_id,h.raw_customer_id,h.app_id,r.app_id,b.app_id
        FROM staging.customer_profile_history h JOIN raw.customer r
          ON (r.batch_id,r.raw_customer_id)=(h.batch_id,h.raw_customer_id)
        JOIN control.ingestion_batch b ON b.batch_id=h.batch_id
        WHERE h.app_id IS NULL ORDER BY h.batch_id''') == before
    assert base.one(db, '''SELECT count(*) FROM pg_trigger
        WHERE tgrelid='control.stitching_job'::regclass
          AND tgname='customer_forward_stitch_gate' AND tgenabled='O' ''') == 1


@pytest.mark.parametrize('damage', ['wrong_landing', 'orphan_raw'])
def test_052_manual_exception_fails_closed_on_provenance_drift(db, damage):
    chain(db, legacy_manual=damage if damage == 'orphan_raw' else True)
    batch = base.one(db, '''SELECT batch_id FROM control.ingestion_batch
        WHERE batch_ref='legacy-manual-upload' ''')
    if damage == 'wrong_landing':
        base.query(db, '''UPDATE control.source_landing_batch SET source_code=%s
            WHERE batch_id=(SELECT landing_batch_id FROM control.ingestion_batch WHERE batch_id=%s)''',
            ('unknown',batch))
    db.commit()
    with pytest.raises(psycopg2.Error, match='052 incomplete historical'):
        base.query(db, (MIGRATIONS / '052_customer_ordinary_activation.sql').read_text())
    db.rollback()
    assert base.one(db, '''SELECT count(*) FROM pg_trigger
        WHERE tgrelid='control.stitching_job'::regclass
          AND tgname='customer_forward_stitch_gate' AND tgenabled='O' ''') == 1


def test_052_refuses_missing_051_and_does_not_weaken_038(db):
    forward.old_alias(db)
    db.commit()
    base.query(db, 'ALTER TABLE platform.workspace ADD COLUMN app_id text')
    base.query(db, "SELECT set_config('cdp.connector_owner_runtime','verified-owner-only',false)")
    install(db, '040_connector_app_lineage.sql')
    install(db, '041_global_member_bridge.sql')
    install(db, '042_customer_capture_lock_retry.sql')
    install(db, '044_customer_overlap_capture.sql')
    install(db, '045_connector_identity_publication.sql')
    forward.install(db)
    cutover.install(db)
    with pytest.raises(psycopg2.Error, match='052.*051'):
        base.query(db, (MIGRATIONS/'052_customer_ordinary_activation.sql').read_text())
    db.rollback()
    assert base.one(db,"SELECT count(*) FROM pg_trigger WHERE tgname='customer_forward_stitch_gate' AND tgenabled='O'") == 1
