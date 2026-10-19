"""LOCAL ONLY committed bulk-core comparison, not ingestion SLA acceptance.

Runs baseline/bulk/bulk/baseline on the SAME wide canonical fixture and pinned
live raw-table indexes/FKs, resetting ONLY cdp_bulk_regression on 127.0.0.1:55441.
A is distinct-new; B has 50% A overlap and changed typed values; B exact replay.
Includes real current/history/membership COMMIT/readback and parser timings.
Excludes S3, queue, startup, existing-source snapshot and validation seeding.
Never extrapolate this microbenchmark to 1M/SLA/Aurora. Use parent cloud harness.
"""
from __future__ import annotations
import argparse
import hashlib
import importlib.util
import json
import logging
from pathlib import Path
import resource
import shutil
import sys
import tempfile
from time import perf_counter
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / '_lambda/ingestion_data'))
sys.path.insert(0, str(ROOT / 'tests/integration'))
from generate_fixture import generate
from test_customer_bulk import bulk_module, connect, counts, reset_schema


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class Timings(logging.Handler):
    def __init__(self):
        super().__init__()
        self.messages = []

    def emit(self, record):
        try:
            self.messages.append(json.loads(record.getMessage()))
        except (ValueError, TypeError):
            pass


def canonical_digest(q):
    digest = hashlib.sha256()
    for table, order in [('raw.customer_current', 'source_customer_id'),
                         ('raw.customer', 'batch_id,source_customer_id')]:
        q.execute("SELECT to_jsonb(t)-ARRAY['raw_customer_id','snapshot_id','snapshot_recorded_at',"
                  "'created_at','updated_at','ingested_at','batch_id'] FROM " + table + ' t ORDER BY ' + order)
        for (item,) in q:
            digest.update(json.dumps(item, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode())
    return digest.hexdigest()


def run(rows, output, padding):
    # Conservative budget (CSV+parsed Python+tables+WAL/temp); no million fixture
    # generated locally. This guard does not apply to parent isolated cloud runner.
    disk = shutil.disk_usage(ROOT)
    estimated_bytes = rows * 40000
    if disk.free < 512 * 1024**2 + estimated_bytes:
        raise RuntimeError(f'Insufficient verified local disk: {disk.free} free; reserve + fixture needs {512*1024**2+estimated_bytes}')
    baseline_path = ROOT / 'tests/integration/fixtures/customer_bulk_baseline.py'
    baseline = load('bulk_benchmark_baseline', baseline_path)
    parser = load('bulk_benchmark_parser', ROOT / '_lambda/ingestion_data/lambda_function.py')
    writer = bulk_module()
    handler = Timings()
    writer.LOGGER.setLevel(logging.INFO)
    writer.LOGGER.addHandler(handler)
    result: dict[str, Any] = dict(scope='local committed raw core only; not full pipeline',
                  canonical_s3_clock_measured=False, acceptance_1m_under_600s=False,
                  exclusions=['S3', 'queue/startup', 'existing_source_snapshot', 'validation seeding', 'landing'],
                  rows_per_batch=rows, overlap_fraction=0.5, disk_free_before=disk.free,
                  hashes={str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in [
                      baseline_path, ROOT/'_lambda/ingestion_data/customer_bulk.py',
                      ROOT/'database/migrations/019_customer_typed_history.sql',
                      ROOT/'database/migrations/022_customer_bulk_ingestion.sql',
                      ROOT/'tests/integration/fixtures/customer_bulk_live_base.sql']}, runs=[])
    with tempfile.TemporaryDirectory(prefix='customer-bulk-fixture-') as folder:
        paths = {rev: Path(folder)/f'{rev}.csv' for rev in ['A','B']}
        result['fixtures'] = {rev: generate(path, rows=rows, start=0 if rev=='A' else rows//2,
                                           revision=rev, padding=padding) for rev,path in paths.items()}
        for run_no, mode in enumerate(['baseline','bulk','bulk','baseline']):
            c = connect()
            try:
                with c.cursor() as q:
                    reset_schema(q, optimized=mode=='bulk')
                    # Pin batch IDs across comparisons, preserving immutable order.
                    q.execute("INSERT INTO control.ingestion_batch VALUES "
                              "('00000000-0000-4000-8000-000000000001',1,'2025-01-01'),"
                              "('00000000-0000-4000-8000-000000000002',1,'2025-01-02')")
                c.commit()
                with c.cursor() as q:
                    q.execute('SELECT version(),pg_current_wal_insert_lsn()::text')
                    version, wal_start = q.fetchone()
                    q.execute('SHOW synchronous_commit')
                    sync = q.fetchone()[0]
                current: dict[str, Any] = dict(mode=mode, run=run_no, phases={}, server=version, synchronous_commit=sync)
                run_start = perf_counter()
                for phase,rev in [('A','A'),('B','B'),('replay_B','B')]:
                    t = perf_counter()
                    with paths[rev].open('rb') as stream:
                        customers, stats = parser.prepare_customer_stream(stream)
                    parse_seconds = perf_counter()-t
                    assert len(customers)==rows and stats['records_rejected']==0
                    bid = '00000000-0000-4000-8000-00000000000' + ('1' if rev=='A' else '2')
                    handler.messages.clear()
                    c.notices.clear()
                    with c.cursor() as q:
                        t=perf_counter()
                        if mode=='baseline':
                            baseline._upsert_customers(q, customers, bid, 1)
                        else:
                            writer.upsert_customers(q, iter(customers), bid, 1)
                        write_seconds=perf_counter()-t
                        t=perf_counter()
                        # Same compatibility membership call as the current Lambda.
                        q.execute('INSERT INTO raw.customer_batch_member(ingestion_batch_id,raw_customer_id,source_system_id,source_customer_id) '
                                  'SELECT batch_id,raw_customer_id,source_system_id,source_customer_id FROM raw.customer WHERE batch_id=%s ON CONFLICT DO NOTHING', (bid,))
                        member_seconds=perf_counter()-t
                    t=perf_counter(); c.commit(); commit_seconds=perf_counter()-t
                    with c.cursor() as q:
                        q.execute('SELECT source_customer_id FROM raw.customer WHERE batch_id=%s ORDER BY source_customer_id',(bid,))
                        assert [r[0] for r in q] == sorted(v['source_customer_id'] for v in customers)
                        q.execute('SELECT source_customer_id FROM raw.customer_batch_member WHERE ingestion_batch_id=%s ORDER BY source_customer_id',(bid,))
                        assert [r[0] for r in q] == sorted(v['source_customer_id'] for v in customers)
                    current['phases'][phase] = dict(parse_seconds=parse_seconds,write_seconds=write_seconds,
                        compatibility_membership_seconds=member_seconds,commit_seconds=commit_seconds,
                        bulk_python=list(handler.messages),sql_notices=[n.strip() for n in c.notices if 'customer_bulk_sql' in n])
                    del customers
                current['wall_seconds']=perf_counter()-run_start
                current['commit_seconds']=sum(p['commit_seconds'] for p in current['phases'].values())
                with c.cursor() as q:
                    current['counts']=list(counts(q))
                    assert current['counts']==[rows+rows//2,2*rows,2*rows]
                    current['typed_digest']=canonical_digest(q)
                    q.execute('SELECT pg_wal_lsn_diff(pg_current_wal_insert_lsn(),%s)::bigint,pg_database_size(current_database())',(wal_start,))
                    current['cluster_wal_bytes_in_window'],current['database_bytes']=q.fetchone()
                current['process_peak_rss']=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
                result['runs'].append(current)
                output.parent.mkdir(parents=True,exist_ok=True)
                output.write_text(json.dumps(result,indent=2))
                print(json.dumps(dict(mode=mode,wall_seconds=current['wall_seconds'],counts=current['counts'],phases=current['phases'])),flush=True)
            finally:
                c.close()
    assert len({r['typed_digest'] for r in result['runs']})==1
    result['disk_free_after']=shutil.disk_usage(ROOT).free
    output.write_text(json.dumps(result,indent=2))
    return result


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--rows',type=int,default=1000)
    p.add_argument('--padding',type=int,default=256)
    p.add_argument('--output',type=Path,required=True)
    args=p.parse_args()
    if args.rows < 2 or args.rows % 2:
        p.error('--rows must be positive and even (50% overlap)')
    run(args.rows,args.output,args.padding)
