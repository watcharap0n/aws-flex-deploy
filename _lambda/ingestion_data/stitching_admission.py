"""Durable, database-wide single-flight admission for the full-staging stitcher.

QUEUED control.stitching_job rows are the outbox. A one-minute EventBridge
schedule reconciles them; producers never need a successful network dispatch.
The admission lock serializes launchers; the unchanged writer lock protects
publication. Neither elapsed contention nor a duplicate event fails a job.
"""
from datetime import datetime, timezone
import logging
import os

from botocore.exceptions import ClientError
from psycopg2.extras import Json, RealDictCursor

LOGGER = logging.getLogger(__name__)
ADMISSION_LOCK = 7413220180
WRITER_LOCK = 7413220179
ACTIVE = ('SUBMITTED', 'WAITING_FOR_LOCK', 'VALIDATING', 'RUNNING')


def _active_tasks(ecs, cluster):
    arns = []
    token = None
    while True:
        args = {'cluster': cluster, 'desiredStatus': 'RUNNING'}
        if token:
            args['nextToken'] = token
        page = ecs.list_tasks(**args)
        arns.extend(page['taskArns'])
        token = page.get('nextToken')
        if not token:
            break
    tasks = []
    for offset in range(0, len(arns), 100):
        result = ecs.describe_tasks(cluster=cluster, tasks=arns[offset:offset+100])
        if result.get('failures'):
            raise RuntimeError('Cannot verify active ECS tasks; admission deferred')
        tasks.extend(t for t in result['tasks'] if t['lastStatus'] != 'STOPPED' and (
            t.get('group') == 'family:cdp-stitcher-task' or any(
                c['name'] == os.getenv('ECS_CONTAINER_NAME', 'cdp-stitcher-container')
                for c in t.get('containers', [])
            )
        ))
    return tasks


def tick(conn, ingestion):
    """One bounded admission pass. Never wait for or kill an existing writer."""
    acquired = False
    writer = False
    ecs = ingestion.boto3.client('ecs')
    cluster = ingestion._required_environment('ECS_CLUSTER')
    not_before = os.getenv('STITCHING_ADMISSION_NOT_BEFORE')
    if not_before is None:
        raise ValueError('STITCHING_ADMISSION_NOT_BEFORE is required for historical admission scope')
    datetime.fromisoformat(not_before)
    try:
        with conn.cursor() as q:
            q.execute('SELECT pg_try_advisory_lock(%s)', (ADMISSION_LOCK,))
            acquired = q.fetchone()[0]
        conn.commit()
        if not acquired:
            return {'status': 'admission_busy'}
        with conn.cursor() as q:
            # Queue age matters only once admission is eligible; active work
            # remains monitored even if its ingestion/validation facts change.
            q.execute('''SELECT EXISTS(SELECT 1 FROM control.stitching_job sj
                         LEFT JOIN control.ingestion_batch ib ON ib.batch_id=sj.ingestion_batch_id
                         LEFT JOIN quality.customer_validation_run vr ON vr.stitching_job_id=sj.job_id
                         WHERE sj.created_at >= %s::timestamptz
                           AND (sj.status = ANY(%s) OR (
                               sj.status='QUEUED' AND ib.status='completed'
                               AND vr.stitching_job_id IS NOT NULL))
                           AND sj.created_at < clock_timestamp()-interval '24 hours')''',
                      (not_before,list(ACTIVE)))
            overdue = q.fetchone()[0]
        conn.commit()
        if overdue:
            # Alarm without mutating/abandoning the actual work or writer.
            LOGGER.error('stitching_admission_overdue work_retained=true')
        # Check the actual database writer before all ECS/API work.
        with conn.cursor() as q:
            q.execute('SELECT pg_try_advisory_lock(%s)', (WRITER_LOCK,))
            writer = q.fetchone()[0]
        conn.commit()
        if not writer:
            return {'status': 'waiting_for_writer'}
        tasks = _active_tasks(ecs, cluster)
        if tasks:
            # Repair the commit-after-RunTask gap without ever launching twice.
            for task in tasks:
                with conn.cursor() as q:
                    q.execute('''SELECT sj.job_id::text, sj.ingestion_batch_id::text
                                 FROM control.stitching_job sj JOIN control.stitching_admission a USING(job_id)
                                 WHERE sj.job_id::text=%s AND sj.ecs_task_arn IS NULL''', (task.get('startedBy', ''),))
                    orphan = q.fetchone()
                conn.commit()
                if orphan:
                    ingestion._record_stitching_dispatch(conn, orphan[1], orphan[0], task['taskArn'])
                    with conn, conn.cursor() as q:
                        q.execute("UPDATE control.stitching_admission SET state='LAUNCHED',task_arn=%s,updated_at=clock_timestamp() WHERE job_id=%s",(task['taskArn'],orphan[0]))
            return {'status': 'active_task'}

        # Reconcile uncertain launches including tasks that already stopped.
        with conn.cursor(cursor_factory=RealDictCursor) as q:
            q.execute("SELECT job_id::text FROM control.stitching_admission WHERE state='PREPARED' AND dispatch_attempts>0")
            uncertain = q.fetchall()
        conn.commit()
        for pending in uncertain:
            response = ecs.list_tasks(cluster=cluster, startedBy=pending['job_id'])
            arns = response.get('taskArns', [])
            # startedBy can omit STOPPED tasks; a repeat of the exact pinned
            # request below is the authoritative ECS idempotency recovery.
            if arns:
                response = ecs.describe_tasks(cluster=cluster, tasks=arns)
                matches = [t for t in response.get('tasks', []) if t.get('startedBy') == pending['job_id']]
                if len(matches) != 1:
                    raise RuntimeError('Ambiguous admission tasks require operator reconciliation')
                with conn.cursor() as q:
                    q.execute('SELECT ingestion_batch_id::text FROM control.stitching_job WHERE job_id=%s',(pending['job_id'],))
                    batch = q.fetchone()[0]
                conn.commit()
                task = matches[0]
                ingestion._record_stitching_dispatch(conn,batch,pending['job_id'],task['taskArn'])
                with conn,conn.cursor() as q:
                    q.execute("UPDATE control.stitching_admission SET state='LAUNCHED',task_arn=%s,updated_at=clock_timestamp() WHERE job_id=%s",(task['taskArn'],pending['job_id']))
                if task['lastStatus']=='STOPPED':
                    ingestion._handle_ecs_task_state_change({'detail':task},conn)

        # EventBridge delivery is not assumed reliable: poll exact tracked tasks.
        with conn.cursor(cursor_factory=RealDictCursor) as q:
            q.execute('''SELECT job_id::text, ecs_task_arn, status
                         FROM control.stitching_job WHERE status = ANY(%s)
                           AND created_at >= %s::timestamptz
                         ORDER BY created_at LIMIT 100''', (list(ACTIVE),not_before))
            running = q.fetchall()
        conn.commit()
        for job in running:
            if not job['ecs_task_arn']:
                raise RuntimeError('Active stitching job has no task ARN; operator reconciliation required')
            response = ecs.describe_tasks(cluster=cluster, tasks=[job['ecs_task_arn']])
            if response.get('failures') or not response.get('tasks'):
                raise RuntimeError('Tracked ECS task missing; operator reconciliation required')
            task = response['tasks'][0]
            if task['lastStatus'] != 'STOPPED':
                return {'status': 'active_task', 'job_id': job['job_id']}
            ingestion._handle_ecs_task_state_change({'detail': task}, conn)

        # Defensive recovery for a legacy/direct caller racing admission. Only
        # proven LOCK timeouts are retryable; OOM, validation and SQL errors are
        # left FAILED. Preserve the old attempt and copy its arrival-time facts.
        with conn.cursor(cursor_factory=RealDictCursor) as q:
            q.execute('''SELECT sj.job_id::text, sj.ingestion_batch_id::text, sj.ecs_task_arn
                         FROM control.stitching_job sj
                         JOIN control.stitching_admission a USING(job_id)
                         JOIN quality.customer_validation_run vr ON vr.stitching_job_id=sj.job_id
                         WHERE sj.status='FAILED' AND sj.error_code='TimeoutError'
                           AND vr.error_stage='LOCK' AND a.retry_job_id IS NULL
                         ORDER BY sj.created_at LIMIT 1''')
            retry = q.fetchone()
        conn.commit()
        if retry:
            task = ecs.describe_tasks(cluster=cluster, tasks=[retry['ecs_task_arn']])
            if task.get('failures') or not task.get('tasks') or task['tasks'][0]['lastStatus'] != 'STOPPED':
                raise RuntimeError('Cannot prove contention task stopped; retry deferred')
            with conn.cursor() as q:
                q.execute('''SELECT count(*) FROM control.stitching_admission a
                             JOIN control.stitching_job sj USING(job_id)
                             WHERE sj.ingestion_batch_id=%s AND a.retry_job_id IS NOT NULL''', (retry['ingestion_batch_id'],))
                retries = q.fetchone()[0]
            conn.commit()
            if retries >= 20:
                raise RuntimeError('Repeated external contention exceeded retry budget; operator action required')
            replacement = ingestion._ensure_stitching_job(conn, retry['ingestion_batch_id'])
            with conn, conn.cursor() as q:
                q.execute('UPDATE control.stitching_admission SET retry_job_id=%s,updated_at=clock_timestamp() WHERE job_id=%s', (replacement['job_id'],retry['job_id']))

        with conn.cursor(cursor_factory=RealDictCursor) as q:
            q.execute('''SELECT sj.job_id::text, sj.ingestion_batch_id::text
                         FROM control.stitching_job sj
                         JOIN control.ingestion_batch ib ON ib.batch_id=sj.ingestion_batch_id
                         JOIN quality.customer_validation_run vr ON vr.stitching_job_id=sj.job_id
                         WHERE sj.status='QUEUED' AND ib.status='completed'
                           AND sj.created_at >= %s::timestamptz
                         ORDER BY sj.created_at, sj.job_id LIMIT 1''', (not_before,))
            job = q.fetchone()
        conn.commit()
        if not job:
            return {'status': 'idle'}
        job_id, batch = job['job_id'], job['ingestion_batch_id']
        # Persist the exact request before network I/O. It is never reconstructed
        # after a crash or a configuration deployment (ECS clientToken contract).
        with conn.cursor(cursor_factory=RealDictCursor) as q:
            q.execute('SELECT * FROM control.stitching_admission WHERE job_id=%s', (job_id,))
            intent = q.fetchone()
        conn.commit()
        if not intent:
            request = ingestion._stitching_task_request(batch, job_id)
            with conn, conn.cursor() as q:
                q.execute('INSERT INTO control.stitching_admission(job_id, request) VALUES (%s,%s)', (job_id,Json(request)))
            with conn.cursor(cursor_factory=RealDictCursor) as q:
                q.execute('SELECT * FROM control.stitching_admission WHERE job_id=%s', (job_id,))
                intent = q.fetchone()
            conn.commit()
        now = datetime.now(timezone.utc)
        if intent['state'] == 'BLOCKED':
            raise RuntimeError('Stitching admission blocked; inspect durable intent')
        if intent['next_attempt_at'] > now:
            return {'status': 'retry_pending', 'job_id': job_id}
        if intent['dispatch_attempts'] >= 20 or (now-intent['prepared_at']).total_seconds() > 3600:
            # Do not replay an uncertain ECS launch after token retention expires.
            with conn, conn.cursor() as q:
                q.execute("UPDATE control.stitching_admission SET state='BLOCKED',last_error='Dispatch reconciliation budget exhausted',updated_at=clock_timestamp() WHERE job_id=%s", (job_id,))
            raise RuntimeError('Dispatch reconciliation budget exhausted; operator reconciliation required')
        with conn, conn.cursor() as q:
            q.execute('''UPDATE control.stitching_admission SET dispatch_attempts=dispatch_attempts+1,
                         next_attempt_at=clock_timestamp()+interval '60 seconds', updated_at=clock_timestamp()
                         WHERE job_id=%s''', (job_id,))
        try:
            task_arn = ingestion._start_stitching_task(batch, job_id, request=intent['request'])
            ingestion._record_stitching_dispatch(conn, batch, job_id, task_arn)
            with conn, conn.cursor() as q:
                q.execute("UPDATE control.stitching_admission SET state='LAUNCHED',task_arn=%s,last_error=NULL,updated_at=clock_timestamp() WHERE job_id=%s",(task_arn,job_id))
            return {'status': 'submitted', 'job_id': job_id, 'task_arn': task_arn}
        except Exception as exc:
            conn.rollback()
            code = exc.response.get('Error', {}).get('Code') if isinstance(exc, ClientError) else type(exc).__name__
            definitive = isinstance(exc, ingestion.StitchingDispatchRejected) or (isinstance(exc, ClientError) and code in {
                'AccessDeniedException', 'InvalidParameterException', 'ClientException',
                'ClusterNotFoundException', 'PlatformUnknownException',
            })
            if definitive:
                # These errors conclusively did not launch a task. Safe to fail
                # this job and let later independent work proceed next minute.
                ingestion._mark_stitching_dispatch_failed(conn, batch, job_id, RuntimeError('ECS RunTask rejected: '+str(code)))
            with conn, conn.cursor() as q:
                q.execute('UPDATE control.stitching_admission SET last_error=%s,updated_at=clock_timestamp() WHERE job_id=%s',(code,job_id))
            raise
    finally:
        conn.rollback()
        with conn.cursor() as q:
            if writer:
                q.execute('SELECT pg_advisory_unlock(%s)', (WRITER_LOCK,))
            if acquired:
                q.execute('SELECT pg_advisory_unlock(%s)', (ADMISSION_LOCK,))
        conn.commit()
        if acquired and locals().get('overdue'):
            raise RuntimeError('Stitching completion overdue; work retained and writer untouched')


def job_status(conn, batch_id, job_id):
    """Follow only same-batch, explicitly journaled retries; never fake success."""
    seen = set()
    while job_id not in seen and len(seen) <= 20:
        seen.add(job_id)
        with conn, conn.cursor() as q:
            q.execute('''SELECT sj.status, a.retry_job_id::text, a.state,
                                sj.error_code, vr.error_stage, a.job_id IS NOT NULL,
                                clock_timestamp()-sj.created_at > interval '24 hours'
                         FROM control.stitching_job sj
                         LEFT JOIN control.stitching_admission a USING(job_id)
                         LEFT JOIN quality.customer_validation_run vr ON vr.stitching_job_id=sj.job_id
                         WHERE sj.job_id=%s AND sj.ingestion_batch_id=%s''', (job_id,batch_id))
            row = q.fetchone()
        if row is None:
            raise ValueError('Stitching job lineage mismatch')
        status, replacement, admission, error, stage, managed, overdue = row
        if replacement:
            job_id = replacement
            continue
        if admission == 'BLOCKED':
            status = 'BLOCKED'
        elif status == 'FAILED' and error == 'TimeoutError' and stage == 'LOCK' and managed:
            status = 'RETRY_PENDING'
        return {'status': status, 'job_id': job_id, 'error_code': error, 'overdue': bool(overdue)}
    raise RuntimeError('Invalid or exhausted stitching retry chain')
