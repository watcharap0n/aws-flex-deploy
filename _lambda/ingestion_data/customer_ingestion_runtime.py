"""Durable customer ingestion control plane (also packaged with the worker)."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from urllib.parse import unquote_plus
from psycopg2.extras import Json, RealDictCursor
from datetime import date, datetime


def enqueue(connection, record, *, stage='canonical'):
    """Durably accept an event without downloading or parsing its payload."""
    if stage not in ('canonical','landing'):
        raise ValueError('Invalid customer ingestion stage')
    obj = record['s3']['object']
    bucket, key = record['s3']['bucket']['name'], unquote_plus(obj['key'])
    etag, version = obj['eTag'].strip('"'), obj.get('versionId')
    prefix = 'raw/' if stage == 'canonical' else 'data_sources/'
    if not bucket or not key or not etag or not key.startswith(prefix):
        raise ValueError('Invalid customer S3 identity for stage')
    identity = hashlib.sha256(json.dumps([stage, bucket, key, etag, version], separators=(',', ':')).encode()).hexdigest()
    with connection, connection.cursor(cursor_factory=RealDictCursor) as q:
        q.execute("INSERT INTO control.customer_ingestion_job(event_identity,stage,bucket,object_key,etag,version_id,event) "
                  "VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(event_identity) DO UPDATE SET event_identity=EXCLUDED.event_identity "
                  "RETURNING job_id::text,status", (identity,stage,bucket,key,etag,version,Json(record)))
        return dict(q.fetchone())


def job_status(connection, job_id):
    """Private Lambda control API; SUCCEEDED means this stage, not stitching."""
    job_id = str(uuid.UUID(str(job_id)))
    with connection, connection.cursor(cursor_factory=RealDictCursor) as q:
        q.execute('SELECT job_id::text,stage,status,attempt_count,error_code,'
                  'ingestion_batch_id::text,landing_batch_id::text '
                  'FROM control.customer_ingestion_job WHERE job_id=%s', (job_id,))
        row = q.fetchone()
        if row is None:
            raise ValueError('Customer ingestion job not found')
        return dict(row)


def reserve(connection, request_factory, *, mode='one_shot'):
    """Reserve a DB-limited slot, then COMMIT the exact idempotent launch intent."""
    with connection, connection.cursor(cursor_factory=RealDictCursor) as q:
        q.execute('SELECT * FROM control.customer_ingestion_runtime WHERE singleton FOR UPDATE')
        cfg = q.fetchone()
        if not cfg or cfg['schema_version'] != 1:
            raise RuntimeError('Customer ingestion runtime schema v1 required')
        if not cfg['enabled'] or cfg['mode'] != mode:
            return None
        q.execute("SELECT slot FROM control.customer_ingestion_attempt WHERE status NOT IN ('SUCCEEDED','FAILED')")
        used = {r['slot'] for r in q.fetchall()}
        if len(used) >= cfg['max_active']:
            return None
        slot = next((s for s in range(1,cfg['max_active']+1) if s not in used), None)
        if slot is None:
            return None
        q.execute("SELECT * FROM control.customer_ingestion_job WHERE status='QUEUED' AND available_at<=now() "
                  "ORDER BY created_at,job_id LIMIT 1 FOR UPDATE SKIP LOCKED")
        job = q.fetchone()
        if not job:
            return None
        attempt_id = str(uuid.uuid4())
        request = request_factory(attempt_id) if request_factory else None
        q.execute("UPDATE control.customer_ingestion_job SET status='ACTIVE',attempt_count=attempt_count+1,updated_at=now() WHERE job_id=%s", (job['job_id'],))
        q.execute("INSERT INTO control.customer_ingestion_attempt(attempt_id,job_id,attempt,status,slot,request,lease_until) "
                  "VALUES (%s,%s,%s,'RESERVED',%s,%s,now()+interval '10 minutes') RETURNING *",
                  (attempt_id,job['job_id'],job['attempt_count']+1,slot,Json(request)))
        result = dict(q.fetchone())
        result.update(job_id=str(job['job_id']), attempt_id=attempt_id)
        return result


class LeaseLost(RuntimeError):
    """A stale process must roll back, never finalize payload or release a slot."""


def claim(connection, attempt_id, owner, task_arn=None):
    with connection, connection.cursor(cursor_factory=RealDictCursor) as q:
        q.execute("UPDATE control.customer_ingestion_attempt a SET status='RUNNING',owner=%s,"
                  "task_arn=COALESCE(task_arn,%s),started_at=now(),heartbeat_at=now(),"
                  "lease_until=now()+make_interval(secs=>c.lease_seconds) "
                  "FROM control.customer_ingestion_runtime c WHERE a.attempt_id=%s "
                  "AND a.status IN ('RESERVED','SUBMITTED') AND a.owner IS NULL "
                  "AND (a.task_arn IS NULL OR a.task_arn=%s) RETURNING a.job_id",
                  (owner,task_arn,attempt_id,task_arn))
        row = q.fetchone()
        if not row:
            return None
        q.execute('SELECT * FROM control.customer_ingestion_job WHERE job_id=%s', (row['job_id'],))
        return dict(q.fetchone())


def heartbeat(connection, attempt_id, owner):
    with connection, connection.cursor() as q:
        # Evaluate lease time only AFTER obtaining the row lock: a heartbeat
        # waiting behind finalization must not resurrect an expired owner.
        q.execute('SELECT status FROM control.customer_ingestion_attempt WHERE attempt_id=%s AND owner=%s FOR UPDATE',
                  (attempt_id,owner))
        row = q.fetchone()
        if not row:
            return False
        if row[0] == 'SUCCEEDED':
            return True
        q.execute("UPDATE control.customer_ingestion_attempt a SET heartbeat_at=clock_timestamp(),"
                  "lease_until=clock_timestamp()+make_interval(secs=>c.lease_seconds) "
                  "FROM control.customer_ingestion_runtime c WHERE a.attempt_id=%s AND a.owner=%s "
                  "AND a.status='RUNNING' AND a.lease_until>clock_timestamp()", (attempt_id,owner))
        return q.rowcount == 1


def fence(cursor, attempt_id, owner):
    """Lock only at transaction end; never block the independent heartbeat during work."""
    cursor.execute('SELECT attempt_id FROM control.customer_ingestion_attempt WHERE attempt_id=%s FOR UPDATE', (attempt_id,))
    cursor.execute("SELECT attempt_id FROM control.customer_ingestion_attempt WHERE attempt_id=%s AND owner=%s "
                   "AND status='RUNNING' AND lease_until>clock_timestamp()", (attempt_id,owner))
    if not cursor.fetchone():
        raise LeaseLost('Ingestion owner or lease was revoked')


def complete(cursor, attempt_id, owner, batch_id, *, stage='canonical'):
    """Called INSIDE the payload transaction immediately before its commit."""
    fence(cursor,attempt_id,owner)
    cursor.execute("UPDATE control.customer_ingestion_attempt SET status='SUCCEEDED',completed_at=now() "
                   "WHERE attempt_id=%s AND owner=%s AND status='RUNNING' AND lease_until>clock_timestamp() "
                   "RETURNING job_id", (attempt_id,owner))
    row = cursor.fetchone()
    if not row:
        raise LeaseLost('Ingestion owner or lease was revoked')
    cursor.execute("UPDATE control.customer_ingestion_job SET status='SUCCEEDED',completed_at=now(),"
                   "updated_at=now(),ingestion_batch_id=%s,landing_batch_id=%s,error_code=NULL WHERE job_id=%s",
                   (batch_id if stage=='canonical' else None,batch_id if stage=='landing' else None,row[0]))


def _fail_locked(q, attempt, code, detail=None):
    q.execute('SELECT max_attempts,retry_seconds FROM control.customer_ingestion_runtime WHERE singleton')
    cfg = q.fetchone()
    status = 'QUEUED' if attempt['attempt'] < cfg['max_attempts'] else 'FAILED'
    q.execute("UPDATE control.customer_ingestion_attempt SET status='FAILED',completed_at=now(),"
              "error_code=%s,stop_detail=%s WHERE attempt_id=%s", (code[:100],Json(detail),attempt['attempt_id']))
    q.execute("UPDATE control.customer_ingestion_job SET status=%s,error_code=%s,updated_at=now(),"
              "available_at=now()+make_interval(secs=>%s),completed_at=CASE WHEN %s='FAILED' THEN now() ELSE NULL END "
              "WHERE job_id=%s", (status,code[:100],cfg['retry_seconds'],status,attempt['job_id']))
    return {'status':status, 'job_id':str(attempt['job_id'])}


def fail(connection, attempt_id, owner, code):
    """Only after rollback: retain error classes, never row-bearing DB messages."""
    with connection, connection.cursor(cursor_factory=RealDictCursor) as q:
        q.execute("SELECT * FROM control.customer_ingestion_attempt WHERE attempt_id=%s AND owner=%s "
                  "AND status='RUNNING' FOR UPDATE", (attempt_id,owner))
        attempt = q.fetchone()
        return _fail_locked(q,attempt,code) if attempt else {'status':'unchanged'}


def stopped(connection, detail):
    if detail.get('lastStatus') != 'STOPPED':
        return {'status':'ignored'}
    task, started = detail.get('taskArn') or '', detail.get('startedBy') or ''
    # Match only the exact persisted ARN, or the pre-RunTask intent when its ARN
    # was never received. Old attempt events cannot fence a newer attempt.
    with connection, connection.cursor(cursor_factory=RealDictCursor) as q:
        q.execute("SELECT * FROM control.customer_ingestion_attempt WHERE "
                  "(%s<>'' AND task_arn=%s) OR (task_arn IS NULL AND attempt_id::text=%s) "
                  "ORDER BY created_at DESC LIMIT 1 FOR UPDATE", (task,task,started))
        attempt = q.fetchone()
        if not attempt:
            return {'status':'ignored'}
        if attempt['status'] in ('SUCCEEDED','FAILED'):
            return {'status':'unchanged'}
        safe_detail = {k:detail[k] for k in ('taskArn','stopCode','stoppedAt') if k in detail}
        # Keep exit codes; arbitrary reason strings can include customer content.
        safe_detail['exit_codes'] = [c.get('exitCode') for c in detail.get('containers',[])]
        return _fail_locked(q, attempt, 'ECS_TASK_STOPPED', safe_detail)


def tick(connection, ecs, request_factory, *, cluster):
    """Recover launches and stopped workers before admitting one bounded task.

    The dispatch mutex spans AWS, but no transaction/row locks do. A lost launch
    response replays the persisted token/request only within ten minutes (well
    inside ECS's stopped-task token retention). Beyond that, UNKNOWN holds slot.
    """
    with connection, connection.cursor() as q:
        q.execute("SELECT pg_try_advisory_lock(724013,23)")
        acquired = q.fetchone()[0]
    if not acquired:
        return {'status':'busy'}
    try:
        with connection, connection.cursor(cursor_factory=RealDictCursor) as q:
            q.execute("SELECT *,created_at<now()-interval '10 minutes' AS dispatch_old,"
                      "lease_until<now() AS expired FROM control.customer_ingestion_attempt "
                      "WHERE status NOT IN ('SUCCEEDED','FAILED') ORDER BY created_at LIMIT 8")
            active = [dict(a) for a in q.fetchall()]
        new = reserve(connection,request_factory)
        if new:
            active.append(dict(new,dispatch_old=False,expired=False))
        for a in active:
            if a['task_arn']:
                response = ecs.describe_tasks(cluster=cluster,tasks=[a['task_arn']])
                tasks = response.get('tasks') or []
                task = next((t for t in tasks if t.get('taskArn')==a['task_arn']),None)
                if task and task.get('lastStatus')=='STOPPED':
                    stopped(connection, task)
                    continue
                if a['expired'] and a['status'] in ('RUNNING','SUBMITTED','STOPPING'):
                    with connection, connection.cursor() as q:
                        q.execute("UPDATE control.customer_ingestion_attempt SET status='STOPPING',error_code='LEASE_EXPIRED' "
                                  "WHERE attempt_id=%s AND status IN ('RUNNING','SUBMITTED','STOPPING') AND lease_until<now()", (a['attempt_id'],))
                        stop = q.rowcount == 1
                    if stop:
                        # Never free a slot on StopTask ACK: wait for STOPPED.
                        ecs.stop_task(cluster=cluster,task=a['task_arn'],reason='Customer ingestion lease expired')
                continue
            if a['status'] != 'RESERVED':
                continue
            if not a['request']:
                if a['expired']:
                    with connection, connection.cursor(cursor_factory=RealDictCursor) as q:
                        q.execute("SELECT * FROM control.customer_ingestion_attempt WHERE attempt_id=%s AND status='RESERVED' FOR UPDATE", (a['attempt_id'],))
                        still = q.fetchone()
                        if still:
                            _fail_locked(q,still,'WARM_CLAIM_EXPIRED')
                continue
            if a['dispatch_old']:
                with connection, connection.cursor() as q:
                    q.execute("UPDATE control.customer_ingestion_attempt SET status='UNKNOWN',error_code='LAUNCH_RECONCILIATION_REQUIRED' "
                              "WHERE attempt_id=%s AND status='RESERVED'", (a['attempt_id'],))
                continue
            try:
                response = ecs.run_task(**a['request'])
            except Exception:
                # Unknown external effect; persist intent, do not free/requeue.
                with connection, connection.cursor() as q:
                    q.execute("UPDATE control.customer_ingestion_attempt SET error_code='ECS_LAUNCH_AMBIGUOUS' "
                              "WHERE attempt_id=%s AND status='RESERVED'",(a['attempt_id'],))
                continue
            tasks, failures = response.get('tasks') or [], response.get('failures') or []
            if failures and not tasks:
                with connection, connection.cursor(cursor_factory=RealDictCursor) as q:
                    q.execute("SELECT * FROM control.customer_ingestion_attempt WHERE attempt_id=%s AND status='RESERVED' FOR UPDATE", (a['attempt_id'],))
                    still = q.fetchone()
                    if still:
                        _fail_locked(q,still,'ECS_DISPATCH_REJECTED')
            elif len(tasks)==1 and tasks[0].get('taskArn') and not failures:
                with connection, connection.cursor() as q:
                    q.execute("UPDATE control.customer_ingestion_attempt SET task_arn=COALESCE(task_arn,%s),submitted_at=COALESCE(submitted_at,now()),"
                              "status=CASE WHEN status='RESERVED' THEN 'SUBMITTED' ELSE status END "
                              "WHERE attempt_id=%s AND (task_arn IS NULL OR task_arn=%s)",
                              (tasks[0]['taskArn'],a['attempt_id'],tasks[0]['taskArn']))
        return {'status':'reconciled','examined':len(active)}
    finally:
        with connection, connection.cursor() as q:
            q.execute('SELECT pg_advisory_unlock(724013,23)')


class CustomerStore:
    """Bounded scratch storage, not durable state. First-key order, last valid row.

    Date tags are applied only to known typed fields; arbitrary customer JSON is
    never interpreted as a serialization tag. No pickle/untrusted deserialization.
    """
    def __init__(self, path):
        self.db = sqlite3.connect(str(path))
        self.db.execute('PRAGMA cache_size=-16384')
        self.db.execute('CREATE TABLE customers (id INTEGER PRIMARY KEY, key TEXT UNIQUE, payload TEXT)')

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.db.close()

    def __setitem__(self, key, value):
        self.db.execute('INSERT INTO customers(key,payload) VALUES (?,?) '
                        'ON CONFLICT(key) DO UPDATE SET payload=excluded.payload',
                        (key, json.dumps(value, default=self._encode, ensure_ascii=False)))

    @staticmethod
    def _encode(value):
        if isinstance(value, (date, datetime)):
            return value.isoformat()
        raise TypeError(type(value).__name__)

    @staticmethod
    def _decode(payload):
        row = json.loads(payload)
        for key in ('date_of_birth', 'birthday'):
            if row.get(key):
                row[key] = date.fromisoformat(row[key])
        for key in ('source_created_at', 'source_updated_at'):
            if row.get(key):
                row[key] = datetime.fromisoformat(row[key])
        return row

    def __len__(self):
        return self.db.execute('SELECT count(*) FROM customers').fetchone()[0]

    def __iter__(self):
        for row in self.db.execute('SELECT payload FROM customers ORDER BY id'):
            yield self._decode(row[0])

    def __getitem__(self, item):
        if not isinstance(item, slice) or item.step not in (None, 1):
            raise TypeError('CustomerStore supports contiguous slices only')
        start, stop, _ = item.indices(len(self))
        return [self._decode(row[0]) for row in self.db.execute(
            'SELECT payload FROM customers ORDER BY id LIMIT ? OFFSET ?',
            (max(0, stop-start), start))]
