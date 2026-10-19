"""Operator-only recovery for a verified pre-export upload capture failure.

No ingestion replay, no transfer mutation, and no general-purpose batch retry.
This module is NOT an HTTP endpoint. Run on an explicitly selected original
batch/transfer only, with the installed Lambda helper source pinned separately.
"""

import re
from uuid import UUID


class RecoveryIneligible(RuntimeError):
    """The immutable evidence does not justify a new stitching attempt."""


_BATCH_REF = re.compile(r'^\d{4}-\d{2}-\d{2}-\d{6}-(\d+)-[0-9a-f]{12}$')
_RETRYABLE_CAPTURE = frozenset({'CAPTURE_LOCK_CONTENDED', 'SOURCE_IDENTITY_CONFLICT'})


def recover_one(conn, batch_id, original_job_id, transfer_id, ingestion, *, dry_run=False):
    """Allocate at most one new attempt; preserve the immutable original transfer.

    The per-batch session lock covers preflight + allocation; the SELECT FOR
    UPDATE pins the original evidence. No S3 read or customer-value logging.
    A complete validation snapshot is copied, never reconstructed from mutable
    landing/current state. New work is admitted by the ordinary queue tick.
    """
    batch_id, original_job_id, transfer_id = (str(UUID(str(value))) for value in
                                            (batch_id, original_job_id, transfer_id))
    lock_key = 'customer-blocked-recovery:' + batch_id
    try:
        with conn.cursor() as q:
            q.execute('SELECT pg_try_advisory_lock(hashtextextended(%s,0))', (lock_key,))
            locked = q.fetchone()[0]
        conn.commit()
        if not locked:
            raise RecoveryIneligible('RECOVERY_ALREADY_RUNNING')
        try:
            with conn.cursor() as q:
                q.execute('''SELECT b.status,b.batch_ref,b.app_id,b.records_loaded,
                    s.source_code,s.source_type,slb.output_row_count,
                    j.status,j.attempt,j.selected_record_count,t.status,t.error_code,
                    t.row_count,t.attempts,b.source_system_id,b.landing_batch_id::text
                    FROM control.customer_identity_transfer t
                    JOIN control.stitching_job j ON j.job_id=t.stitching_job_id
                        AND j.ingestion_batch_id=t.ingestion_batch_id
                    JOIN control.ingestion_batch b ON b.batch_id=j.ingestion_batch_id
                    JOIN control.source_landing_batch slb ON slb.batch_id=b.landing_batch_id
                    JOIN control.source_system s ON s.source_system_id=b.source_system_id
                    WHERE t.transfer_id=%s AND t.stitching_job_id=%s
                        AND t.ingestion_batch_id=%s FOR UPDATE OF t,j,b''',
                    (transfer_id,original_job_id,batch_id))
                row=q.fetchone()
                if row is None:
                    raise RecoveryIneligible('ORIGINAL_LINEAGE_NOT_FOUND')
                (batch_status, ref, app_id, loaded, source, source_type,
                 landing_rows, job_status, attempt, selected, transfer_status,
                 code, row_count, tries, source_id, landing_id)=row
                if source != 'manual_upload' or source_type != 's3_csv':
                    raise RecoveryIneligible('NOT_PROVEN_MANUAL_UPLOAD')
                q.execute('SELECT control.customer_publication_is_connector(%s::uuid)', (batch_id,))
                if q.fetchone()[0]:
                    raise RecoveryIneligible('CONNECTOR_LINEAGE_EXCLUDED')
                match=_BATCH_REF.fullmatch(ref or '')
                if not match or app_id is None or match.group(1)!=app_id:
                    raise RecoveryIneligible('UPLOAD_APP_SCOPE_UNPROVEN')
                if code not in _RETRYABLE_CAPTURE or transfer_status!='BLOCKED' or row_count!=0 or tries!=0:
                    raise RecoveryIneligible('NOT_PREEXPORT_RECOVERABLE_CAPTURE')
                if batch_status!='completed' or job_status!='SUCCEEDED' or not loaded or selected!=loaded:
                    raise RecoveryIneligible('INGESTION_OR_STITCHING_NOT_COMPLETE')
                q.execute('''SELECT count(*)::bigint, count(*) FILTER (WHERE
                        h.raw_customer_id IS NULL OR h.app_id IS DISTINCT FROM %s
                        OR h.source_system_id IS DISTINCT FROM m.source_system_id
                        OR h.source_customer_id IS DISTINCT FROM m.source_customer_id
                        OR p.raw_customer_id IS NULL OR p.app_id IS DISTINCT FROM %s
                        OR p.source_system_id IS DISTINCT FROM m.source_system_id
                        OR p.source_customer_id IS DISTINCT FROM m.source_customer_id)::bigint,
                        count(DISTINCT m.source_customer_id)::bigint
                    FROM raw.customer_batch_member m
                    LEFT JOIN staging.customer_profile_history h
                        ON h.batch_id=m.ingestion_batch_id AND h.raw_customer_id=m.raw_customer_id
                    LEFT JOIN staging.customer_profile p ON p.raw_customer_id=m.raw_customer_id
                    WHERE m.ingestion_batch_id=%s AND m.source_system_id=%s''',
                    (app_id,app_id,batch_id,source_id))
                members,bad,keys=q.fetchone()
                q.execute('''SELECT count(*)::bigint,count(*) FILTER
                    (WHERE selected_for_canonical)::bigint,
                    count(DISTINCT source_row_number)::bigint,
                    count(DISTINCT r.validation_run_id)::bigint,
                    min(r.validation_run_id::text),
                    min(v.source_row_count)::bigint
                    FROM quality.customer_validation_record r
                    JOIN quality.customer_validation_run v USING(validation_run_id)
                    WHERE v.stitching_job_id=%s AND v.ingestion_batch_id=%s
                      AND v.run_status='SUCCEEDED' ''',(original_job_id,batch_id))
                records,selected_records,positions,validation_runs,old_validation,source_rows=q.fetchone()
                if (members!=loaded or bad!=0 or keys!=members or landing_rows!=loaded
                        or selected_records!=members or positions!=records or validation_runs!=1
                        or source_rows!=records or not old_validation):
                    raise RecoveryIneligible('IMMUTABLE_EVIDENCE_INCOMPLETE')
                q.execute('''SELECT job_id::text,attempt,status FROM control.stitching_job
                    WHERE ingestion_batch_id=%s ORDER BY attempt DESC LIMIT 1''',(batch_id,))
                latest=q.fetchone()
                if latest != (original_job_id,attempt,job_status):
                    if latest[1]>attempt:
                        conn.rollback()
                        return {'status':'ALREADY_ATTEMPTED','job_id':latest[0],
                                'attempt':latest[1], 'batch_id':batch_id}
                    raise RecoveryIneligible('JOB_ATTEMPT_DRIFT')
                if dry_run:
                    conn.rollback()
                    return {'status':'ELIGIBLE','batch_id':batch_id,
                            'original_job_id':original_job_id,'selected_members':members}
                # Add a new job; never rewrite the SUCCEEDED job or BLOCKED transfer.
                q.execute('''INSERT INTO control.stitching_job(ingestion_batch_id,attempt,status)
                    VALUES(%s,%s,'QUEUED') RETURNING job_id::text''',(batch_id,attempt+1))
                job_id=q.fetchone()[0]
                validation_id=ingestion._create_validation_run(q, landing_batch_id=landing_id,
                    ingestion_batch_id=batch_id, stitching_job_id=job_id, attempt=attempt+1)
                ingestion._copy_validation_records(q,validation_run_id=validation_id,
                                                   previous_validation_run_id=old_validation)
                q.execute('''UPDATE quality.customer_validation_run SET
                    selected_record_count=(SELECT count(*) FROM quality.customer_validation_record
                        WHERE validation_run_id=%s AND selected_for_canonical),
                    existing_source_record_count=(SELECT count(*) FROM quality.customer_validation_record
                        WHERE validation_run_id=%s AND selected_for_canonical AND existing_source_record),
                    updated_at=now() WHERE validation_run_id=%s''',
                    (validation_id,validation_id,validation_id))
                q.execute('SELECT count(*) FROM quality.customer_validation_record WHERE validation_run_id=%s',
                          (validation_id,))
                if q.fetchone()[0]!=records:
                    raise RecoveryIneligible('VALIDATION_COPY_INCOMPLETE')
            conn.commit()
            return {'status':'QUEUED','job_id':job_id,'batch_id':batch_id,'attempt':attempt+1}
        except Exception:
            conn.rollback()
            raise
        finally:
            with conn.cursor() as q:
                q.execute('SELECT pg_advisory_unlock(hashtextextended(%s,0))',(lock_key,))
            conn.commit()
    except Exception:
        conn.rollback()
        raise
