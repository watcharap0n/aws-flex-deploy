"""Opt-in small wide fixture benchmark; no million-row projection.

LANDING_TEST_DSN=... python tests/integration/landing_bulk_benchmark.py
Exercises original CSV -> parse/evidence -> committed PostgreSQL -> canonical CSV.
No AWS requests. Output is JSON on stdout; source and canonical checksums must match.
"""
import csv
import hashlib
import importlib.util
import io
import json
import subprocess
import sys
import time
import tracemalloc
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location('landing_regressions', HERE / 'test_landing_bulk.py')
assert spec and spec.loader
fixture = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fixture)
root = fixture.ROOT
original = subprocess.check_output(['git', '-C', str(root), 'show', 'HEAD:_lambda/landing_data/lambda_function.py'])
legacy = types.ModuleType('landing_legacy_benchmark')
sys.modules[legacy.__name__] = legacy
exec(compile(original, 'landing_legacy_benchmark.py', 'exec'), legacy.__dict__)

ROW_COUNT = 2000
buffer = io.StringIO(newline='')
columns = fixture.landing.CSV_COLUMNS + [f'extra_{i}' for i in range(40)]
writer = csv.DictWriter(buffer, fieldnames=columns, lineterminator='\n')
writer.writeheader()
for i in range(ROW_COUNT):
    row = {name: '' for name in columns}
    row.update(source_customer_id=f'{i:012d}', first_name='สมชาย', last_name='ทดสอบ',
               email=f'customer-{i}@example.com', phone=f'08{i:08d}', date_of_birth='2000-01-02',
               source_created_at='2026-09-18T10:00:00Z', status='ACTIVE',
               attributes=json.dumps({'nullable': None, 'name': 'ไทย'}, ensure_ascii=False),
               external_ids=json.dumps({'device': f'00{i}'}))
    row.update({f'extra_{j}': f'รายละเอียด-{j}-' + 'x' * 32 for j in range(40)})
    writer.writerow(row)
content = buffer.getvalue().encode('utf-8-sig')
results = []
for label, module in [('original', legacy), ('copy_spooled', fixture.landing)]:
    setup = fixture.db.__wrapped__()
    connection = next(setup)
    _, _, batch, file_id, _ = fixture.register(connection, content)
    tracemalloc.start()
    started = time.perf_counter()
    parsed = None
    try:
        if label == 'original':
            parsed = module.parse_csv_evidence(content, 'source_customer_id')
        else:
            parsed = module.parse_source_evidence_spooled(content, 'csv', 'source_customer_id')
        parsed_at = time.perf_counter()
        with connection, connection.cursor() as q:
            count = module._replace_evidence_rows(q, 'customers', batch, file_id, parsed.rows, 1000)
            module._complete_file(q, file_id, parsed)
            module._refresh_batch_status(q, batch)
        committed_at = time.perf_counter()
        if label == 'original':
            canonical, exported = module._build_customer_csv(connection, batch, 'crm')
            digest, size = hashlib.sha256(canonical).hexdigest(), len(canonical)
        else:
            with module._build_customer_csv_spooled(connection, batch, 'crm', spool_limit=1024) as output:
                exported, digest, size = output.row_count, output.sha256, output.size
        finished = time.perf_counter()
        _, peak = tracemalloc.get_traced_memory()
        assert count == exported == ROW_COUNT
        with connection, connection.cursor() as q:
            q.execute('SELECT count(*), count(DISTINCT source_customer_id) FROM raw.customer_landing_source WHERE file_id=%s', (file_id,))
            assert q.fetchone() == (ROW_COUNT, ROW_COUNT)
        results.append({'variant': label, 'rows': ROW_COUNT, 'columns': len(columns),
                        'source_bytes': len(content), 'source_sha256': hashlib.sha256(content).hexdigest(),
                        'canonical_bytes': size, 'canonical_sha256': digest,
                        'parse_seconds': parsed_at-started, 'db_commit_seconds': committed_at-parsed_at,
                        'canonical_seconds': finished-committed_at, 'total_seconds': finished-started,
                        'python_tracemalloc_peak_bytes': peak})
    finally:
        tracemalloc.stop()
        if hasattr(parsed, '__exit__'):
            parsed.__exit__(None, None, None)
        try:
            next(setup)
        except StopIteration:
            pass
assert results[0]['canonical_sha256'] == results[1]['canonical_sha256']
print(json.dumps({'scope': 'Local 2,000-row wide fixture; tracemalloc enabled; no S3/network or million-scale projection', 'results': results}, indent=2))
