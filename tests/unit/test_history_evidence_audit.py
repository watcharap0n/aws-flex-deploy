"""Offline, synthetic evidence only: never print connector customer payloads."""
import hashlib
import json
import sys
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))


def fixture(tmp_path):
    from history_parquet_parser_v1 import PARQUET_SCHEMA
    rows = []
    for number, email in [(1, 'old@example.com'), (2, 'new@example.com')]:
        row = {field.name: None for field in PARQUET_SCHEMA}
        row.update(source_row_number=number, source_customer_id='same', email=email,
                   external_ids='{}', attributes='{}', raw_payload='{"private":"do-not-print"}')
        rows.append(row)
    sink = pa.BufferOutputStream()
    pq.write_table(pa.Table.from_pylist(rows, schema=PARQUET_SCHEMA), sink)
    body = sink.getvalue().to_pybytes()
    part_hash = hashlib.sha256(body).hexdigest()
    manifest = json.dumps({'manifest_version': 1, 'format': 'parquet',
                           'records_published': 2, 'bytes_written': len(body),
                           'parts': [{'part_number': 1, 'bucket': 'private', 'key': 'part',
                                     'checksum_sha256': part_hash, 'row_count': 2,
                                     'byte_count': len(body)}]}).encode()
    manifest_hash = hashlib.sha256(manifest).hexdigest()
    (tmp_path / part_hash).write_bytes(body)
    (tmp_path / manifest_hash).write_bytes(manifest)
    batch = '00000000-0000-4000-8000-000000000001'
    original = {'at': '2026-09-18T07:00:00+00:00', 'batches': [{
        'batch_id': batch, 'landing_batch_id': '00000000-0000-4000-8000-000000000002',
        'status': 'completed', 'records_loaded': 1, 'checksum': manifest_hash, 'object_name': 'connector/manifest.json',
        'output_key': 'connector/manifest.json', 'output_bucket': 'private',
        'output_sha256': manifest_hash, 'output_status': 'PUBLISHED',
        'output_row_count': 1, 'membership_rows': 1, 'history_rows': 0,
        'classification': 'UNSUPPORTED_ARTIFACT_FORMAT'}]}
    latest = {'at': '2026-09-18T09:00:00+00:00', 'batches': [{
        **original['batches'][0], 'ledger_status': None, 'expected_rows': None}]}
    # The original production preflight does not capture ingestion status.
    del original['batches'][0]['status']
    return original, latest, batch


def test_verified_parquet_is_pending_typed_db_comparison_not_recovered(tmp_path):
    from audit_customer_history_evidence import audit
    original, latest, batch = fixture(tmp_path)
    result = audit(original, latest, tmp_path)
    assert result['batches'] == [{'batch_id': batch, 'status': 'ARTIFACT_VERIFIED_TYPED_RECOVERY_PENDING',
                                  'expected_rows': 1, 'observed_history_rows': 0,
                                  'missing_typed_rows_at_snapshot': 1,
                                  'evidence_sha256': original['batches'][0]['output_sha256'],
                                  'selected_rows': 1, 'published_rows': 2}]
    assert result['summary']['statuses'] == {'ARTIFACT_VERIFIED_TYPED_RECOVERY_PENDING': 1}
    assert 'do-not-print' not in json.dumps(result)


def test_existing_parquet_history_requires_exact_typed_comparison(tmp_path):
    from audit_customer_history_evidence import audit
    original, latest, _ = fixture(tmp_path)
    latest['batches'][0]['history_rows'] = 1
    result = audit(original, latest, tmp_path)['batches'][0]
    assert result['status'] == 'ARTIFACT_VERIFIED_EXISTING_TYPED_COMPARISON_PENDING'
    assert result['missing_typed_rows_at_snapshot'] == 0


def test_missing_lineage_precedes_missing_membership(tmp_path):
    from audit_customer_history_evidence import audit
    original, latest, _ = fixture(tmp_path)
    original['batches'][0]['landing_batch_id'] = None
    latest['batches'][0]['landing_batch_id'] = None
    original['batches'][0]['membership_rows'] = 0
    latest['batches'][0]['membership_rows'] = 0
    assert audit(original, latest, tmp_path)['batches'][0]['status'] == 'BLOCKED_LINEAGE'


def test_missing_membership_never_becomes_recoverable_from_current_keys(tmp_path):
    from audit_customer_history_evidence import audit
    original, latest, _ = fixture(tmp_path)
    latest['batches'][0]['membership_rows'] = 0
    assert audit(original, latest, tmp_path)['batches'][0]['status'] == 'BLOCKED_MISSING_HISTORICAL_MEMBERSHIP'


def test_snapshot_does_not_reclassify_mismatched_original_membership(tmp_path):
    from audit_customer_history_evidence import audit
    original, latest, _ = fixture(tmp_path)
    original['batches'][0]['membership_rows'] = 0
    assert audit(original, latest, tmp_path)['batches'][0]['status'] == 'BLOCKED_MISSING_HISTORICAL_MEMBERSHIP'


def test_corrupt_part_fails_closed_without_customer_data(tmp_path):
    from audit_customer_history_evidence import audit
    original, latest, _ = fixture(tmp_path)
    for path in tmp_path.iterdir():
        if path.stat().st_size > 1000:
            path.write_bytes(b'PRIVATE-CORRUPT')
    result = audit(original, latest, tmp_path)
    assert result['batches'][0]['status'] == 'BLOCKED_PART_CHECKSUM'
    assert 'PRIVATE-CORRUPT' not in json.dumps(result)


def test_ledger_snapshot_and_unversioned_csv_are_not_reported_as_verified_recovery(tmp_path):
    from audit_customer_history_evidence import audit
    original, latest, batch = fixture(tmp_path)
    old = original['batches'][0]
    old.update(object_name='landing/customer.csv', output_key='landing/customer.csv')
    new = latest['batches'][0]
    new.update(object_name='landing/customer.csv', output_key='landing/customer.csv')
    result = audit(original, latest, tmp_path)
    assert result['batches'][0]['status'] == 'BLOCKED_CSV_EVIDENCE_NOT_AUDITED'
    new.update(history_rows=1, ledger_status='VERIFIED', expected_rows=1)
    result = audit(original, latest, tmp_path)
    assert result['batches'][0]['status'] == 'LEDGER_REPORTED_LIVE_RECHECK_REQUIRED'


def test_stale_missing_duplicate_or_contradictory_snapshots_fail_closed(tmp_path):
    from audit_customer_history_evidence import audit, AuditError
    original, latest, _ = fixture(tmp_path)
    latest['at'] = '2026-09-17T00:00:00+00:00'
    with pytest.raises(AuditError, match='SNAPSHOT_ORDER'):
        audit(original, latest, tmp_path)
    latest['at'] = '2026-09-18T09:00:00+00:00'
    latest['batches'] = []
    with pytest.raises(AuditError, match='MISSING_BATCH'):
        audit(original, latest, tmp_path)
    latest['batches'] = [original['batches'][0]] * 2
    with pytest.raises(AuditError, match='DUPLICATE_BATCH'):
        audit(original, latest, tmp_path)
    latest['batches'] = [{**original['batches'][0], 'checksum': 'other'}]
    with pytest.raises(AuditError, match='LINEAGE_CHANGED'):
        audit(original, latest, tmp_path)
