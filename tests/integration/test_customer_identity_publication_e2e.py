"""Parent-owned real migration -> capture -> cutover -> worker -> status loop."""
from __future__ import annotations

import importlib.util
import os
import sys
from uuid import uuid4

import psycopg2
import pytest

from test_customer_identity_publication import (
    ROOT, batch, execute, finish, member, register, scalar, setup_database, transfer,
)

STITCHER = ROOT.parent / 'fargate-user-stitching'
sys.path.insert(0, str(STITCHER / 'src'))
from identity_stitcher.publication import ClickHouse, PublicationWorker  # noqa: E402
from identity_stitcher.db import apply_migrations  # noqa: E402


def load_cutover():
    spec = importlib.util.spec_from_file_location('e2e_cutover', ROOT / 'scripts/cutover_customer_identity_profile.py')
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def stores():
    if os.getenv('CUSTOMER_PUBLICATION_E2E') != '1':
        pytest.skip('CUSTOMER_PUBLICATION_E2E=1 uses owned fixed local fixtures only')
    dsn = os.getenv('CUSTOMER_PUBLICATION_E2E_PG_DSN', 'host=127.0.0.1 port=61577 user=postgres dbname=poststitch_test')
    params = psycopg2.extensions.parse_dsn(dsn)
    assert params.get('host') == '127.0.0.1' and params.get('port') == '61577'
    assert params.get('dbname') in {'poststitch_test', 'poststitch_upload_scope_test'}
    conn = psycopg2.connect(dsn)
    setup_database(conn)
    apply_migrations(conn)  # Real startup must not run ClickHouse DDL on PostgreSQL.
    conn.autocommit = True
    execute(conn, (ROOT / 'database/migrations/025_customer_publication_status.sql').read_text())
    conn.autocommit = False
    db = 'poststitch_e2e_' + uuid4().hex
    ch = ClickHouse('http://127.0.0.1:61584', database=db)
    ch.install()
    # Actual security contract: business readers get only the outer view.
    ch.execute('CREATE USER IF NOT EXISTS identity_publication_definer IDENTIFIED WITH no_password HOST NONE')
    ch.execute('ALTER USER identity_publication_definer HOST NONE')
    for name in ('identity_profile_versions', 'identity_publications', 'identity_profile_committed',
                 'identity_profile_current', 'customer_profile_resolved', 'customer_profile_legacy_snapshot'):
        ch.execute(f'GRANT SELECT ON {db}.{name} TO identity_publication_definer')
    old_ddl = (ROOT.parent / 'identity-resolution/design/standardized-clickhouse-transfer/006_customer_profile_final.sql').read_text()
    ddl = old_ddl[old_ddl.index('CREATE TABLE IF NOT EXISTS cdp_customer.customer_profile'):].split(';')[0]
    ch.execute(ddl.replace('cdp_customer.', db + '.'))
    legacy_rows = int(os.getenv('CUSTOMER_PUBLICATION_LEGACY_ROWS', '1'))
    assert 1 <= legacy_rows <= 500000
    ch.execute(f"INSERT INTO {db}.customer_profile (app_id,customer_id,customer_key_id,profile_source_record_id) VALUES ('legacy-app','untouched-user',41,42)")
    if legacy_rows > 1:
        ch.execute(f"INSERT INTO {db}.customer_profile (app_id,customer_id,customer_key_id,profile_source_record_id) SELECT 'legacy-app',concat('legacy-',toString(number)),number,number FROM numbers({legacy_rows - 1})")
    cutover = load_cutover()
    assert cutover.cutover(cutover.Client(ch.url), db, writers_stopped=True)['status'] == 'ACTIVE'
    try:
        yield conn, ch
    finally:
        conn.close()
        ch.execute(f'DROP DATABASE {db} SYNC')


def public_status(conn, b):
    return scalar(conn, 'SELECT overall_status FROM control.customer_publication_status WHERE ingestion_batch_id=%s', (b['batch'],))


def test_real_capture_transfer_final_status_and_physical_replay(stores):
    conn, ch = stores
    b = batch(conn)
    first = member(conn, b, 'source-a')
    member(conn, b, 'source-b', entity=first['entity'])
    finish(conn, b)
    assert transfer(conn, b)['status'] == 'PENDING'
    assert public_status(conn, b) == 'TRANSFER_PENDING'
    conn.commit()
    result = PublicationWorker(conn, ch).run_once()
    assert result.status == 'PUBLISHED', result
    assert public_status(conn, b) == 'COMPLETED'
    mapping = ch.rows(f'SELECT user_id,customer_entity_id,resolution_run_id FROM {ch.database}.identity_mapping ORDER BY user_id')
    assert [r['user_id'] for r in mapping] == ['source-a', 'source-b']
    assert {r['customer_entity_id'] for r in mapping} == {first['entity']}
    assert {r['resolution_run_id'] for r in mapping} == {b['run']}
    profiles = ch.rows(f'SELECT customer_id,customer_entity_id,customer_key_id,first_name FROM {ch.database}.customer_profile ORDER BY customer_id')
    assert [r['customer_id'] for r in profiles] == ['source-a', 'source-b', 'untouched-user']
    assert all(r['first_name'] == 'Canonical' and r['customer_key_id'] is None for r in profiles[:2])
    assert profiles[2]['customer_key_id'] == 41 and profiles[2]['customer_entity_id'] is None
    assert PublicationWorker(conn, ch).run_once(transfer_id=b['run']).status == 'PUBLISHED'
    assert ch.rows(f'SELECT count() AS n FROM {ch.database}.identity_profile_versions') == [{'n': 2}]
    assert ch.rows(f'SELECT count() AS n FROM {ch.database}.identity_publications') == [{'n': 1}]
    queries = (ROOT / 'database/queries/verify_customer_identity_publication_clickhouse.sql').read_text()
    queries = '\n'.join(line for line in queries.splitlines() if not line.lstrip().startswith('--'))
    reports = [ch.rows(statement.replace('cdp_customer.', ch.database + '.'))
               for statement in queries.split(';') if statement.strip()]
    assert reports[0][0]['count_and_grain_match'] == 1
    assert reports[1] == []
    assert reports[2] == [{'mismatched_current_rows': 0, 'missing_current_rows': 0}]
    assert reports[3] == []


def test_real_connector_is_excluded_from_transfer_and_status_gating(stores):
    from test_customer_identity_publication import connector_batch
    conn, ch = stores
    setup_database(conn, with_connector=True)
    apply_migrations(conn)
    conn.autocommit = True
    execute(conn, (ROOT / 'database/migrations/025_customer_publication_status.sql').read_text())
    conn.autocommit = False
    workspace = str(uuid4())
    execute(conn, "INSERT INTO platform.workspace(workspace_id,slug,name) VALUES (%s,'publication-e2e','Synthetic')", (workspace,))

    b = connector_batch(conn, workspace)
    member(conn, b, 'connector-user')
    finish(conn, b)
    conn.commit()
    result = PublicationWorker(conn, ch).run_once()
    assert result.status == 'IDLE', result
    assert transfer(conn, b) is None
    assert scalar(conn, 'SELECT count(*) FROM control.customer_publication_status') == 0
    assert scalar(conn, 'SELECT overall_status FROM control.customer_pipeline_status') == 'COMPLETED'
    assert ch.rows(f'SELECT count() AS n FROM {ch.database}.identity_mapping') == [{'n': 0}]
    assert ch.rows(f'SELECT count() AS n FROM {ch.database}.identity_publications') == [{'n': 0}]


def test_real_mixed_workspace_never_reaches_clickhouse(stores):
    conn, ch = stores
    register(conn)
    register(conn, app='222')
    b = batch(conn)
    first = member(conn, b)
    other = batch(conn, app='222', source=2)
    member(conn, other, 'outside-workspace', entity=first['entity'], run=b['run'])
    finish(conn, b)
    conn.commit()
    assert public_status(conn, b) == 'TRANSFER_BLOCKED'
    assert PublicationWorker(conn, ch).run_once(transfer_id=b['run']).status == 'BLOCKED'
    assert ch.rows(f'SELECT count() AS n FROM {ch.database}.identity_profile_versions') == [{'n': 0}]
    assert ch.rows(f'SELECT count() AS n FROM {ch.database}.customer_profile') == [{'n': 1}]


def test_real_100k_capture_transfer_with_legacy_overlay(stores):
    if os.getenv('CUSTOMER_PUBLICATION_E2E_BENCHMARK') != '1':
        pytest.skip('CUSTOMER_PUBLICATION_E2E_BENCHMARK=1')
    import json
    import time

    conn, ch = stores
    legacy_rows = int(os.getenv('CUSTOMER_PUBLICATION_LEGACY_ROWS', '1'))
    # Exercise the actual capture migration, not a synthetic outbox substitute.
    from test_customer_identity_publication import test_capture_is_set_based_at_ten_thousand_users
    start = time.monotonic()
    test_capture_is_set_based_at_ten_thousand_users(conn, 100000)
    seed_and_capture = time.monotonic() - start
    start = time.monotonic()
    result = PublicationWorker(conn, ch).run_once()
    wall = time.monotonic() - start
    assert result.status == 'PUBLISHED', result
    assert scalar(conn, "SELECT overall_status FROM control.customer_publication_status") == 'COMPLETED'
    assert ch.rows(f'SELECT count() AS n FROM {ch.database}.customer_profile') == [{'n': 100000 + legacy_rows}]
    assert ch.rows(f'SELECT count() AS n FROM {ch.database}.identity_mapping') == [{'n': 100000}]
    start = time.monotonic()
    assert sum(1 for _ in ch.iter_rows(f'SELECT * FROM {ch.database}.customer_profile')) == 100000 + legacy_rows
    read_seconds = time.monotonic() - start
    insert_sizes = list(ch.insert_sizes)
    start = time.monotonic()
    assert PublicationWorker(conn, ch).run_once(transfer_id=result.transfer_id).status == 'PUBLISHED'
    replay_seconds = time.monotonic() - start
    assert ch.insert_sizes == insert_sizes
    assert ch.rows(f'SELECT count() AS n FROM {ch.database}.identity_profile_versions') == [{'n': 100000}]
    assert ch.rows(f'SELECT count() AS n FROM {ch.database}.identity_publications') == [{'n': 1}]
    report = {'kind': 'real-migration024-025-cutover-worker-local-synthetic', 'users': 100000,
              'legacy_rows': legacy_rows, 'seed_and_capture_seconds': seed_and_capture,
              'transfer_wall_seconds': wall, 'timings': result.timings,
              'overlay_full_read_seconds': read_seconds, 'source_sha256': result.source_sha256,
              'replay_seconds': replay_seconds, 'physical_rows': 100000, 'marker_rows': 1}
    path = ROOT / f'benchmarks/poststitch-parent-e2e-100k-legacy-{legacy_rows}.json'
    path.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, sort_keys=True))


def test_real_transfer_failure_does_not_rewrite_successful_stitch(stores):
    conn, ch = stores
    register(conn)
    b = batch(conn)
    member(conn, b)
    finish(conn, b)
    conn.commit()
    def fail(stage):
        if stage == 'before_marker':
            raise RuntimeError('injected')
    result = PublicationWorker(conn, ch, hook=fail).run_once()
    assert result.status == 'FAILED'
    assert public_status(conn, b) == 'TRANSFER_FAILED'
    assert scalar(conn, 'SELECT status FROM control.stitching_job WHERE job_id=%s', (b['job'],)) == 'SUCCEEDED'
    assert scalar(conn, 'SELECT status FROM identity.resolution_run WHERE run_id=%s', (b['run'],)) == 'SUCCEEDED'
    assert ch.rows(f'SELECT count() AS n FROM {ch.database}.identity_mapping') == [{'n': 0}]
    assert ch.rows(f'SELECT count() AS n FROM {ch.database}.customer_profile') == [{'n': 1}]
    assert PublicationWorker(conn, ch).run_once(transfer_id=b['run']).status == 'PUBLISHED'
    assert public_status(conn, b) == 'COMPLETED'
