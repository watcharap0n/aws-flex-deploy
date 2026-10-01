"""Connector-to-existing identity publisher contract on an owned localhost PG fixture."""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional
from uuid import uuid4

import test_customer_global_upload_capture as fixture

ROOT = Path(__file__).resolve().parents[2]
db = fixture.db


def install(db):
    # Exactly the current staging lineage/capture generation, in a disposable DB.
    fixture.query(db, "ALTER TABLE platform.workspace ADD COLUMN app_id text")
    fixture.query(db, "SELECT set_config('cdp.connector_owner_runtime','verified-owner-only',false)")
    for name in ('040_connector_app_lineage.sql', '041_global_member_bridge.sql',
                 '042_customer_capture_lock_retry.sql', '044_customer_overlap_capture.sql',
                 '045_connector_identity_publication.sql'):
        fixture.query(db, (ROOT / 'database/migrations' / name).read_text())
    db.commit()


def connector_batch(db, app: Optional[str]='00042'):
    ws, connection, dataset, snapshot, mapping, operation, sync = (str(uuid4()) for _ in range(7))
    source = 4395
    landing, batch, job, run = (str(uuid4()) for _ in range(4))
    ref = 'db-' + sync.replace('-', '')
    code = 'dc1-' + connection.replace('-', '')
    fixture.query(db, "INSERT INTO control.source_system(source_system_id,source_code,source_name,source_type) VALUES (%s,%s,'fixture','database_connector')", (source, code))
    fixture.query(db, "INSERT INTO control.source_landing_batch(batch_id,source_code,batch_ref,output_status) VALUES (%s,%s,%s,'PUBLISHED')", (landing, code, ref))
    fixture.query(db, """INSERT INTO control.ingestion_batch(batch_id,source_system_id,landing_batch_id,batch_ref,status,metadata)
                  VALUES (%s,%s,%s,%s,'completed',jsonb_build_object('connector_sync_run_id',%s::text))""", (batch, source, landing, ref, sync))
    fixture.query(db, "INSERT INTO control.stitching_job(job_id,ingestion_batch_id,status) VALUES (%s,%s,'RUNNING')", (job, batch))
    fixture.query(db, "INSERT INTO identity.resolution_run(run_id,stitching_job_id,status,normalization_version,match_threshold) VALUES (%s,%s,'RUNNING','fixture',0.9)", (run, job))
    b = dict(landing=landing, batch=batch, job=job, run=run, source=source)
    fixture.query(db, 'INSERT INTO platform.workspace(workspace_id,slug,name,app_id) VALUES (%s,%s,%s,%s)',
                  (ws, 'connector-' + ws[:8], 'Synthetic', app))
    fixture.query(db, """INSERT INTO connector.connection(connection_id,workspace_id,definition_key,name,source_code,source_system_id)
                  VALUES (%s,%s,'postgresql','Synthetic',%s,%s)""", (connection, ws, code, source))
    fixture.query(db, "INSERT INTO connector.dataset(dataset_id,workspace_id,connection_id,object_name) VALUES (%s,%s,%s,'customers')", (dataset, ws, connection))
    fixture.query(db, "INSERT INTO connector.schema_snapshot(schema_snapshot_id,workspace_id,dataset_id,schema_hash,schema_json) VALUES (%s,%s,%s,'fixture','{}')", (snapshot, ws, dataset))
    fixture.query(db, "INSERT INTO connector.mapping_version(mapping_version_id,workspace_id,dataset_id,version,schema_snapshot_id,mapping_json,mapping_hash) VALUES (%s,%s,%s,1,%s,'{}','fixture')", (mapping, ws, dataset, snapshot))
    fixture.query(db, "INSERT INTO connector.operation(operation_id,workspace_id,connection_id,dataset_id,operation_type,idempotency_key) VALUES (%s,%s,%s,%s,'DATASET_SYNC','fixture')", (operation, ws, connection, dataset))
    fixture.query(db, """INSERT INTO connector.sync_run(sync_run_id,operation_id,workspace_id,dataset_id,landing_batch_id,ingestion_batch_id,stitching_job_id,mapping_version_id)
                  VALUES (%s,%s,%s,%s,%s,%s,%s,%s)""", (sync, operation, ws, dataset, b['landing'], b['batch'], b['job'], mapping))
    if app is not None:
        assert fixture.one(db, 'SELECT control.bind_connector_batch_app_id(%s,%s,%s)', (b['batch'], ws, app)) == app
    return dict(b, workspace=ws, sync=sync)


def test_connector_capture_creates_proven_transfer_on_existing_publisher_lane(db):
    install(db)
    b = connector_batch(db)
    m = fixture.member(db, b, 'connector-user')
    fixture.query(db, 'INSERT INTO staging.customer_profile_history SELECT * FROM staging.customer_profile WHERE raw_customer_id=%s', (m['raw'],))
    db.commit()
    db.set_session(isolation_level='REPEATABLE READ')
    t = fixture.finish(db, b)
    assert t is not None
    assert (t['status'], t['app_id'], str(t['workspace_id']), t['row_count']) == ('PENDING', '00042', b['workspace'], 1), t['error_code']
    assert fixture.one(db, 'SELECT provenance_status FROM control.customer_identity_member_export WHERE transfer_id=%s', (b['run'],)) == 'VERIFIED_CONNECTOR'
    assert fixture.one(db, "SELECT payload->>'workspace_id' FROM control.customer_identity_export WHERE transfer_id=%s", (b['run'],)) == b['workspace']


def test_existing_publisher_reads_connector_proof_from_immutable_bridge(db):
    install(db)
    b = connector_batch(db)
    m = fixture.member(db, b, 'connector-user')
    fixture.query(db, 'INSERT INTO staging.customer_profile_history SELECT * FROM staging.customer_profile WHERE raw_customer_id=%s', (m['raw'],))
    db.commit()
    db.set_session(isolation_level='REPEATABLE READ')
    t = fixture.finish(db, b)
    db.commit()
    sys.path.insert(0, str(ROOT.parent / 'fargate-user-stitching' / 'src'))
    from identity_stitcher.publication import PublicationWorker
    worker = PublicationWorker.__new__(PublicationWorker)
    worker.conn, worker.batch_rows = db, 10
    transfer = dict(transfer_id=str(t['transfer_id']), resolution_run_id=b['run'],
                    publication_seq=t['publication_seq'], member_count=t['member_count'])
    members = list(worker._member_rows(transfer))
    proofs = list(worker._source_rows_with_proof(transfer))
    assert len(members) == len(proofs) == 1
    assert members[0]['provenance_status'] == 'VERIFIED_CONNECTOR'
    assert proofs[0][1][0] == '00042'


def test_unbound_connector_never_publishes_as_upload(db):
    install(db)
    b = connector_batch(db, app=None)
    # An unbound workspace cannot be retroactively adopted as an upload.
    # The connector raw-writer gate rejects data before publication.
    import psycopg2
    with db.cursor() as cur:
        with __import__('pytest').raises(psycopg2.errors.RaiseException, match='connector customer app_id unbound'):
            cur.execute('''INSERT INTO raw.customer_current(raw_customer_id,batch_id,source_system_id,source_customer_id)
                           VALUES (%s,%s,%s,%s)''', (str(uuid4()), b['batch'], b['source'], 'connector-user'))
    db.rollback()
