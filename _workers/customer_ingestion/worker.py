"""Bounded ECS ingestion worker. No payload work runs in the ingress Lambda."""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
from pathlib import Path
import signal
import tempfile
import threading
import time
import urllib.request
import uuid

import boto3
import customer_ingestion_runtime as runtime
import lambda_function as ingestion

LOGGER = logging.getLogger(__name__)


def log_failure(exc):
    """Log only PostgreSQL diagnostic identifiers, never row-bearing error text."""
    diag = getattr(exc, 'diag', None)
    LOGGER.error('customer_ingestion_failed error_type=%s schema=%s table=%s constraint=%s',
                 type(exc).__name__, getattr(diag, 'schema_name', None),
                 getattr(diag, 'table_name', None), getattr(diag, 'constraint_name', None))


class Heartbeat:
    def __init__(self, connect, attempt_id, owner, payload, interval):
        self.connect, self.attempt_id, self.owner = connect, attempt_id, owner
        self.payload, self.interval = payload, interval
        self.stop_event = threading.Event()
        self.lost = threading.Event()
        self.thread = threading.Thread(target=self._run,daemon=True)

    def _run(self):
        connection = None
        try:
            connection = self.connect()
            while not self.stop_event.wait(self.interval):
                if not runtime.heartbeat(connection,self.attempt_id,self.owner):
                    raise runtime.LeaseLost('Heartbeat rejected')
        except Exception:
            self.lost.set()
            self.payload.cancel()
        finally:
            if connection:
                connection.close()

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self,*args):
        self.stop_event.set()
        self.thread.join(timeout=10)

    def check(self):
        if self.lost.is_set():
            raise runtime.LeaseLost('Heartbeat lost')


def prepare(record,s3,directory,heartbeat):
    """Chunked verified download + reviewed parser into disk-backed dedupe."""
    obj = record['s3']['object']
    from urllib.parse import unquote_plus
    kwargs = dict(Bucket=record['s3']['bucket']['name'],Key=unquote_plus(obj['key']),IfMatch=obj['eTag'].strip('"'))
    if obj.get('versionId'):
        kwargs['VersionId'] = obj['versionId']
    started = time.perf_counter()
    response = s3.get_object(**kwargs)
    digest, size = hashlib.sha256(), 0
    maximum = int(os.getenv('CUSTOMER_MAX_OBJECT_BYTES',str(10*1024**3)))
    path = directory/'canonical.csv'
    try:
        if str(response.get('ETag') or '').strip('"') != kwargs['IfMatch']:
            raise ValueError('Canonical ETag does not match event')
        if obj.get('versionId') and response.get('VersionId') != obj['versionId']:
            raise ValueError('Canonical VersionId does not match event')
        if int(response.get('ContentLength',0)) > maximum:
            raise ValueError('Canonical object exceeds configured scratch bound')
        with path.open('wb') as f:
            while True:
                heartbeat.check()
                block = response['Body'].read(1024*1024)
                if not block:
                    break
                size += len(block)
                if size > maximum:
                    raise ValueError('Canonical object exceeds configured scratch bound')
                digest.update(block)
                f.write(block)
        if response.get('ContentLength') is not None and size != int(response['ContentLength']):
            raise ValueError('Canonical object length mismatch')
    finally:
        response['Body'].close()
    ingestion._log_stage('s3_download',started,bytes=size)
    return path,digest.hexdigest(),ingestion._landing_metadata(response)


def load_landing():
    import importlib.util
    import sys
    name = 'customer_ingestion_landing'
    if name not in sys.modules:
        path = Path(os.getenv('CUSTOMER_LANDING_MODULE','/app/landing/lambda_function.py'))
        spec = importlib.util.spec_from_file_location(name,path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    return sys.modules[name]


def run_landing(record,connection,s3,heartbeat,attempt_id,owner):
    landing = load_landing()
    config = landing._load_config()
    source = landing._source_from_record(record,config)
    if source is None:
        raise ValueError('Unsupported landing source')
    finalized = False
    with landing._download_object(s3,source) as downloaded:
        def before_commit(q):
            nonlocal finalized
            heartbeat.check()
            # A callback runs before COMMIT, which can still fail. Never use an
            # in-memory success flag to bypass a later transaction's lease fence.
            q.execute("SELECT status FROM control.customer_ingestion_attempt WHERE attempt_id=%s AND owner=%s",
                      (attempt_id,owner))
            committed = q.fetchone()
            if committed and committed[0] == 'SUCCEEDED':
                finalized = True
                return
            finalized = False
            runtime.fence(q,attempt_id,owner)
            q.execute('SELECT batch_id::text,output_status FROM control.source_landing_batch '
                      'WHERE source_code=%s AND batch_ref=%s', (source.source_code,source.batch_ref))
            batch = q.fetchone()
            if batch and batch[1]=='PUBLISHED':
                runtime.complete(q,attempt_id,owner,batch[0],stage='landing')
                finalized = True
        result = landing._process_source(source,downloaded,connection,s3,config,before_commit=before_commit)
        if not finalized:
            raise ValueError('Landing output was not published')
        return result


def run_attempt(attempt_id, *, connect=ingestion._connect, s3=None, task_arn=None, heartbeat_seconds=20):
    owner = str(uuid.uuid4())
    control = connect()
    payload = None
    started = time.perf_counter()
    try:
        job = runtime.claim(control,attempt_id,owner,task_arn)
        if job is None:
            return {'status':'not_claimed'}
        payload = connect()
        with Heartbeat(connect,attempt_id,owner,payload,heartbeat_seconds) as heartbeat:
            if job['stage']=='landing':
                if os.getenv('CUSTOMER_LANDING_ENABLED','false').lower() != 'true':
                    raise ValueError('Landing runtime adapter is disabled')
                return run_landing(job['event'],payload,s3 or boto3.client('s3'),heartbeat,attempt_id,owner)
            with tempfile.TemporaryDirectory(prefix='customer-ingestion-') as directory:
                directory = Path(directory)
                path,checksum,metadata = prepare(job['event'],s3 or boto3.client('s3'),directory,heartbeat)
                parse_started = time.perf_counter()
                with runtime.CustomerStore(directory/'customers.sqlite') as store:
                    with path.open('rb') as source:
                        customers,stats = ingestion.prepare_customer_stream(source,store)
                    heartbeat.check()
                    ingestion._log_stage('prepare_customers',parse_started,records_loaded=len(customers),**stats)
                    def finalize(cursor,batch_id):
                        heartbeat.check()
                        runtime.complete(cursor,attempt_id,owner,batch_id)
                    result = ingestion._process_record(job['event'],None,payload,
                        prepared=(customers,stats,checksum,metadata),before_commit=finalize)
        # Ingestion success and the existing QUEUED stitching outbox were committed
        # together. No direct RunTask, no second success/ack transaction.
        ingestion._log_stage('worker_to_commit',started,attempt_id=attempt_id,job_id=str(job['job_id']))
        return result
    except BaseException as exc:
        if payload:
            payload.rollback()
        log_failure(exc)
        runtime.fail(control,attempt_id,owner,type(exc).__name__)
        if isinstance(exc, (SystemExit, KeyboardInterrupt)):
            raise
        raise RuntimeError('Customer worker failed: '+type(exc).__name__) from None
    finally:
        if payload:
            payload.close()
        control.close()


def task_identity():
    endpoint = os.environ['ECS_CONTAINER_METADATA_URI_V4']
    with urllib.request.urlopen(endpoint+'/task',timeout=5) as response:
        return json.load(response)['TaskARN']


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--attempt-id')
    parser.add_argument('--warm',action='store_true')
    args = parser.parse_args()
    if bool(args.attempt_id) == bool(args.warm):
        parser.error('Specify exactly one of --attempt-id or --warm')
    logging.basicConfig(level=logging.INFO)
    task_arn = task_identity()
    signal.signal(signal.SIGTERM,lambda *_: (_ for _ in ()).throw(SystemExit('SIGTERM')))
    if args.attempt_id:
        run_attempt(str(uuid.UUID(args.attempt_id)),task_arn=task_arn)
        return
    control = ingestion._connect()
    try:
        while True:
            attempt = runtime.reserve(control,None,mode='warm')
            if attempt:
                try:
                    run_attempt(str(attempt['attempt_id']),task_arn=task_arn)
                except RuntimeError:
                    LOGGER.error('customer_ingestion_attempt_failed attempt_id=%s',attempt['attempt_id'])
            else:
                time.sleep(float(os.getenv('CUSTOMER_WARM_POLL_SECONDS','2')))
    finally:
        control.close()


if __name__ == '__main__':
    main()
