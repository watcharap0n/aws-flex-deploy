"""Read-only offline reconciliation of typed-history *evidence*, not recovery.

Inputs are previously captured metadata snapshots and a private SHA256-addressed
connector Parquet archive. Never emit customer rows, source keys, or DB errors.
No DB/AWS client, profile, write path, or inferred app ownership is used.
"""
import argparse
from collections import Counter
from datetime import datetime
import json
from pathlib import Path
import re

from history_parquet_parser_v1 import parse_manifest


class AuditError(ValueError):
    """A fixed metadata/audit failure code, never the underlying payload."""


_SHA256 = re.compile(r'[0-9a-f]{64}\Z')
_IMMUTABLE = ('landing_batch_id', 'source_system_id', 'records_loaded', 'checksum',
              'object_name', 'output_bucket', 'output_key', 'output_sha256',
              'output_row_count', 'output_status')
_PARSER_CODES = frozenset(('MANIFEST_CHECKSUM', 'MANIFEST_JSON', 'MANIFEST_FORMAT',
                           'MANIFEST_PARTS', 'PART_METADATA', 'PART_NUMBER',
                           'PART_LOCATION', 'PART_CHECKSUM', 'PART_BYTES', 'PART_PARSE',
                           'PART_SCHEMA', 'PART_ROWS', 'SOURCE_ROW_NUMBER',
                           'SOURCE_KEY', 'JSON_FIELD', 'JSON_OBJECT',
                           'MANIFEST_ROWS', 'MANIFEST_BYTES', 'SELECTED_ROWS'))


def _indexed(snapshot):
    batches = snapshot.get('batches')
    if not isinstance(batches, list):
        raise AuditError('SNAPSHOT_FORMAT')
    indexed = {}
    for batch in batches:
        if not isinstance(batch, dict) or not isinstance(batch.get('batch_id'), str):
            raise AuditError('SNAPSHOT_FORMAT')
        key = batch['batch_id']
        if key in indexed:
            raise AuditError('DUPLICATE_BATCH')
        indexed[key] = batch
    return indexed


def _private_blob(archive, digest):
    if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
        raise AuditError('ARCHIVE_DIGEST')
    path = archive / digest
    if path.is_symlink() or not path.is_file():
        raise AuditError('ARCHIVE_MISSING')
    return path.read_bytes()


def _parquet_evidence(batch, archive):
    checksum = batch['output_sha256']
    try:
        manifest = _private_blob(archive, checksum)
        _, proof = parse_manifest(manifest, checksum, batch['records_loaded'],
                                  lambda part: _private_blob(archive, part['checksum_sha256']))
    except AuditError as error:
        return 'BLOCKED_' + str(error), {}
    except ValueError as error:
        code = str(error)
        return 'BLOCKED_' + (code if code in _PARSER_CODES else 'PARSER_FAILED'), {}
    except Exception:
        return 'BLOCKED_PARSER_FAILED', {}
    # Selection/checksum/schema alone do not compare PostgreSQL typed values or
    # prove the identity mapping for individual historical members.
    return 'ARTIFACT_VERIFIED_TYPED_RECOVERY_PENDING', {
        'evidence_sha256': checksum, 'selected_rows': proof['selected_rows'],
        'published_rows': proof['published_rows']}


def audit(original, latest, archive_dir):
    """Reconcile a bounded original scope against a later metadata snapshot.

    Snapshot counters are not proofs of exact typed equality. A VERIFIED ledger
    seen in metadata is only reported, never accepted as a fresh DB readback.
    """
    try:
        original_at = datetime.fromisoformat(original['at'])
        latest_at = datetime.fromisoformat(latest['at'])
        if (original_at.tzinfo is None or latest_at.tzinfo is None
                or latest_at < original_at):
            raise AuditError('SNAPSHOT_ORDER')
    except (KeyError, TypeError, ValueError) as error:
        if isinstance(error, AuditError):
            raise
        raise AuditError('SNAPSHOT_ORDER') from None
    old, new = _indexed(original), _indexed(latest)
    archive = Path(archive_dir)
    result = []
    for key in sorted(old):
        if key not in new:
            raise AuditError('MISSING_BATCH')
        before, after = old[key], new[key]
        if any(before.get(name) != after.get(name) for name in _IMMUTABLE):
            raise AuditError('LINEAGE_CHANGED')
        expected = before.get('records_loaded')
        members = after.get('membership_rows')
        history = after.get('history_rows')
        item = {'batch_id': key}
        if (before.get('status') not in (None, 'completed')
                or after.get('status') != 'completed'
                or before.get('output_status') != 'PUBLISHED'):
            status = 'BLOCKED_INCOMPLETE_INGESTION'
        elif (type(expected) is not int or expected <= 0
              or type(history) is not int or history < 0 or history > expected
              or type(members) is not int or members < 0 or members > expected):
            status = 'BLOCKED_COUNT_CONTRACT'
        elif after.get('ledger_status') == 'VERIFIED':
            # This is a historical metadata observation, not live DB verification.
            status = ('LEDGER_REPORTED_LIVE_RECHECK_REQUIRED'
                      if history == expected and members == expected
                      and after.get('expected_rows') == expected
                      else 'BLOCKED_LEDGER_COUNT_CONFLICT')
        elif after.get('ledger_status') is not None:
            status = 'BLOCKED_LEDGER_STATUS'
        elif (not before.get('landing_batch_id') or not before.get('output_bucket')
              or before.get('object_name') != before.get('output_key')
              or before.get('checksum') != before.get('output_sha256')
              or before.get('output_row_count') != expected):
            status = 'BLOCKED_LINEAGE'
        elif members != expected or before.get('membership_rows') != expected:
            status = 'BLOCKED_MISSING_HISTORICAL_MEMBERSHIP'
        elif before.get('output_key', '').endswith('/manifest.json'):
            status, proof = _parquet_evidence(before, archive)
            if status == 'ARTIFACT_VERIFIED_TYPED_RECOVERY_PENDING' and history == expected:
                status = 'ARTIFACT_VERIFIED_EXISTING_TYPED_COMPARISON_PENDING'
            item.update(proof)
        elif before.get('output_key', '').lower().endswith('.csv'):
            status = 'BLOCKED_CSV_EVIDENCE_NOT_AUDITED'
        else:
            status = 'BLOCKED_UNSUPPORTED_FORMAT'
        item.update(status=status, expected_rows=expected, observed_history_rows=history,
                    missing_typed_rows_at_snapshot=expected-history if type(expected) is int
                    and type(history) is int and 0 <= history <= expected else None)
        result.append(item)
    counts = Counter(item['status'] for item in result)
    return {'scope_at': original['at'], 'latest_snapshot_at': latest['at'],
            'batches': result, 'summary': {'batches': len(result),
                                           'statuses': dict(sorted(counts.items())),
                                           'missing_typed_rows_at_snapshot': sum(
                                               item['missing_typed_rows_at_snapshot'] or 0
                                               for item in result)}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scope', type=Path, required=True)
    parser.add_argument('--latest-snapshot', type=Path, required=True)
    parser.add_argument('--private-parquet-archive', type=Path, required=True)
    args = parser.parse_args()
    try:
        report = audit(json.loads(args.scope.read_text()),
                       json.loads(args.latest_snapshot.read_text()),
                       args.private_parquet_archive)
    except Exception as error:
        code = str(error) if isinstance(error, AuditError) else 'INPUT_FAILED'
        print(json.dumps({'status': 'FAILED', 'error_code': code}))
        return 1
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
