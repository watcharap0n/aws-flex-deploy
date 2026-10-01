"""054 ordinary ingestion metadata contract on an owned disposable PG :55441 database."""
from uuid import uuid4

import psycopg2
from psycopg2.extras import Json
import pytest

import test_customer_ordinary_activation_052 as activation
import test_customer_forward_source_namespace as forward
import test_customer_global_upload_capture as base


db = forward.db
MIGRATIONS = activation.MIGRATIONS


def setup(conn):
    activation.chain(conn)
    activation.install(conn, '052_customer_ordinary_activation.sql')
    # The base fixture has 019's triggers but not 053's original FK name;
    # install 053's exact target constraint shape to exercise 054's preflight.
    base.query(conn, '''ALTER TABLE raw.customer_batch_member ADD CONSTRAINT customer_batch_member_observation_tuple_key
        UNIQUE (ingestion_batch_id,raw_customer_id,source_system_id,source_customer_id)''')
    base.query(conn, '''ALTER TABLE raw.customer ADD CONSTRAINT customer_history_member_tuple_fkey
        FOREIGN KEY (batch_id,raw_customer_id,source_system_id,source_customer_id)
        REFERENCES raw.customer_batch_member
            (ingestion_batch_id,raw_customer_id,source_system_id,source_customer_id)
        DEFERRABLE INITIALLY DEFERRED''')
    activation.install(conn, '054_customer_forward_batch_runtime_metadata.sql')
    landing, batch = str(uuid4()), str(uuid4())
    ref = f'2026-09-27-120000-{forward.A}-{uuid4().hex[:12]}'
    base.query(conn, "INSERT INTO control.source_landing_batch(batch_id,source_code,batch_ref,output_status) VALUES (%s,'fixture-1',%s,'PUBLISHED')", (landing,ref))
    base.query(conn, '''INSERT INTO control.ingestion_batch
        (batch_id,source_system_id,landing_batch_id,batch_ref,status,metadata)
        VALUES (%s,1,%s,%s,'processing',jsonb_build_object('bucket','fixture','landing_batch_id',%s::text))''',
        (batch,landing,ref,landing))
    return batch


def refused(conn, batch, fragment, *, metadata=None, changes=None):
    base.query(conn, 'SAVEPOINT metadata_refusal')
    with pytest.raises(psycopg2.Error, match=fragment):
        if changes:
            base.query(conn, changes, (batch,))
        else:
            base.query(conn, 'UPDATE control.ingestion_batch SET metadata=%s::jsonb WHERE batch_id=%s',
                       (metadata, batch))
    base.query(conn, 'ROLLBACK TO SAVEPOINT metadata_refusal')


def test_054_completion_and_dispatch_preserve_ordinary_lineage(db):
    batch = setup(db)
    source, landing, ref, app, initial = base.query(db, '''SELECT source_system_id,landing_batch_id,batch_ref,app_id,metadata
        FROM control.ingestion_batch WHERE batch_id=%s''', (batch,))[0]
    assert forward.stage_and_merge(db, batch, 'fresh-user', 'Fresh') == 1
    job, validation = str(uuid4()), str(uuid4())
    # The production completion assignment replaces metadata (rather than ||).
    completed = dict(initial, duplicate_rows=0, stitching_job_id=job, validation_run_id=validation)
    base.query(db, '''UPDATE control.ingestion_batch SET status='completed',completed_at=now(),
        records_received=1,records_loaded=1,records_rejected=0,metadata=%s::jsonb WHERE batch_id=%s''',
        (Json(completed), batch))
    assert base.one(db, 'SELECT status FROM control.ingestion_batch WHERE batch_id=%s', (batch,)) == 'completed'
    submitted = dict(completed, stitching_task_arn='arn:aws:ecs:fixture', stitching_dispatch_status='submitted')
    base.query(db, 'UPDATE control.ingestion_batch SET metadata=%s::jsonb WHERE batch_id=%s',
               (Json(submitted), batch))
    failed = dict(submitted, stitching_dispatch_status='failed', stitching_dispatch_error='fixture failure')
    base.query(db, 'UPDATE control.ingestion_batch SET metadata=%s::jsonb WHERE batch_id=%s',
               (Json(failed), batch))
    assert base.query(db, '''SELECT source_system_id,landing_batch_id,batch_ref,app_id,metadata
        FROM control.ingestion_batch WHERE batch_id=%s''', (batch,)) == [(source, landing, ref, app, failed)]
    refused(db, batch, 'batch source/app lineage is immutable',
            metadata=Json(dict(failed, bucket='changed')))
    refused(db, batch, 'batch source/app lineage is immutable',
            metadata=Json(dict(failed, unexpected='injected')))
    refused(db, batch, 'connector sync lineage cannot change',
            metadata=Json(dict(failed, connector_sync_run_id='forged')))
    for statement in ('UPDATE control.ingestion_batch SET source_system_id=1 WHERE batch_id=%s',
                      'UPDATE control.ingestion_batch SET landing_batch_id=NULL WHERE batch_id=%s',
                      "UPDATE control.ingestion_batch SET batch_ref='other' WHERE batch_id=%s"):
        refused(db, batch, 'batch source/app lineage is immutable', changes=statement)
    assert base.one(db, 'SELECT count(*) FROM raw.customer WHERE batch_id=%s', (batch,)) == 1
    # Existing connector guard must still reject mutable connector metadata.
    import test_connector_identity_publication as connector_fixture
    connector = connector_fixture.connector_batch(db, app=forward.A)
    forward.expect_refused(db,
        "UPDATE control.ingestion_batch SET metadata=metadata || '{\"stitching_task_arn\":\"forged\"}'::jsonb WHERE batch_id=%s",
        (connector['batch'],), '038 connector batch lineage is immutable')


def test_054_failure_marker_and_connector_guard(db):
    batch = setup(db)
    original = base.one(db, 'SELECT metadata FROM control.ingestion_batch WHERE batch_id=%s', (batch,))
    base.query(db, '''UPDATE control.ingestion_batch SET status='failed',
        metadata=metadata || '{"ingestion_error_code":"RaiseException"}'::jsonb WHERE batch_id=%s''', (batch,))
    assert base.one(db, "SELECT metadata->>'ingestion_error_code' FROM control.ingestion_batch WHERE batch_id=%s", (batch,)) == 'RaiseException'
    refused(db, batch, 'batch source/app lineage is immutable', metadata=Json(dict(original, landing_batch_id='fake')))
    # A connector marker must never be added to a file batch, even in failure handling.
    refused(db, batch, 'connector sync lineage cannot change',
            metadata=Json(dict(original, connector_sync_run_id='x')))


def test_054_ecs_stopped_failure_marker_cannot_mutate_source_lineage(db):
    batch = setup(db)
    original = base.one(db, 'SELECT metadata FROM control.ingestion_batch WHERE batch_id=%s',
                        (batch,))
    marker = dict(original, stitching_job_id=str(uuid4()),
                  stitching_task_arn='arn:aws:ecs:fixture',
                  stitching_dispatch_status='failed', stitching_exit_code=137,
                  stitching_stop_code='EssentialContainerExited')
    base.query(db, 'UPDATE control.ingestion_batch SET metadata=%s::jsonb WHERE batch_id=%s',
               (Json(marker), batch))
    assert base.one(db, 'SELECT metadata FROM control.ingestion_batch WHERE batch_id=%s',
                    (batch,)) == marker
    refused(db, batch, 'batch source/app lineage is immutable',
            metadata=Json(dict(marker, bucket='changed')))
    refused(db, batch, 'batch source/app lineage is immutable',
            metadata=Json(dict(marker, unexpected='injected')))
