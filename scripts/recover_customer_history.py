"""Fail-closed historical recovery. No AWS clients or implicit credentials."""
import hashlib
from pathlib import Path
import re
from history_csv_parser_v1 import prepare_customers

PARSER_VERSION = 'canonical-csv-reconstruction-v1'
PARSER_SHA256 = hashlib.sha256(Path(__file__).with_name('history_csv_parser_v1.py').read_bytes()).hexdigest()

class RecoveryError(ValueError):
    """Only fixed error codes, never customer values."""


def parse_artifact(body, checksum, expected_rows):
    if not re.fullmatch('[0-9a-f]{64}', checksum or '') or hashlib.sha256(body).hexdigest() != checksum:
        raise RecoveryError('ARTIFACT_CHECKSUM')
    import csv
    import io
    try:
        header=next(csv.reader(io.StringIO(body.decode('utf-8-sig'))), [])
    except Exception:
        raise RecoveryError('ARTIFACT_HEADER') from None
    if not header or len(header)!=len(set(header)):
        raise RecoveryError('ARTIFACT_HEADER')
    try:
        rows, stats = prepare_customers(body)
    except Exception:
        raise RecoveryError('ARTIFACT_PARSE') from None
    if len(rows) != expected_rows or stats['records_rejected'] or stats['duplicate_rows']:
        raise RecoveryError('ARTIFACT_ROW_COUNT')
    return rows


def archive_artifact(body, checksum, archive_dir):
    """Preserve verified bytes privately; never overwrite conflicting evidence."""
    import os
    if archive_dir is None:
        raise RecoveryError('ARCHIVE_REQUIRED')
    directory=Path(archive_dir).resolve()
    directory.mkdir(mode=0o700,parents=True,exist_ok=True)
    target=directory/checksum
    try:
        fd=os.open(target,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    except FileExistsError:
        if target.is_symlink() or not target.is_file() or hashlib.sha256(target.read_bytes()).hexdigest()!=checksum:
            raise RecoveryError('ARCHIVE_CHECKSUM')
    else:
        with os.fdopen(fd,'wb') as archive:
            archive.write(body)
            archive.flush()
            os.fsync(archive.fileno())
    if hashlib.sha256(target.read_bytes()).hexdigest()!=checksum:
        raise RecoveryError('ARCHIVE_CHECKSUM')
    return str(target)


def recover_batch(connection, batch_id, body, version_id, *, apply=False,
                  checksum_bound_unversioned=False, archive_dir=None):
    """Own one transaction; rollback all inserts on any mismatch or audit mode.

    Default requires the recorded S3 VersionId. Explicit unversioned mode instead
    binds bytes to matching locked ingestion/landing checksums and preserves a
    private archive. Neither mode proves the original historical transformation.
    """
    import json
    from psycopg2 import sql, extensions
    from psycopg2.extras import Json, RealDictCursor, execute_values

    if connection.autocommit or connection.get_transaction_status() != extensions.TRANSACTION_STATUS_IDLE:
        raise RecoveryError('IDLE_TRANSACTION_REQUIRED')
    try:
        with connection.cursor(cursor_factory=RealDictCursor) as q:
            q.execute("SET LOCAL lock_timeout='5s'; SET LOCAL statement_timeout='120s'; SET LOCAL timezone='UTC'")
            # Serialize with capture, membership insertion, lineage edits and other
            # recoveries. Operationally requires paused writers; timeout fails closed.
            q.execute('LOCK TABLE control.ingestion_batch, control.source_landing_batch, raw.customer_current, raw.customer_batch_member, raw.customer, control.customer_history_recovery IN SHARE ROW EXCLUSIVE MODE')
            q.execute('''SELECT ib.batch_id::text, ib.landing_batch_id::text,
                ib.source_system_id, ib.status, ib.records_loaded, ib.checksum,
                ib.object_name, ib.metadata, ib.created_at, sl.output_status,
                sl.output_bucket, sl.output_key, sl.output_sha256, sl.output_row_count
                FROM control.ingestion_batch ib JOIN control.source_landing_batch sl
                ON sl.batch_id=ib.landing_batch_id WHERE ib.batch_id=%s''', (batch_id,))
            b=q.fetchone()
            if not b or b['status'] != 'completed' or b['output_status'] != 'PUBLISHED':
                raise RecoveryError('COMPLETED_LINEAGE_REQUIRED')
            manifest=(b['metadata'] or {}).get('customer_history_artifact', {})
            if checksum_bound_unversioned:
                if version_id not in (None,'null') or manifest.get('version_id') not in (None,'null'):
                    raise RecoveryError('UNVERSIONED_MODE_CONFLICT')
                if (manifest.get('format') not in (None,'canonical-csv-v1')
                        or not (b['output_key'] or '').lower().endswith('.csv')):
                    raise RecoveryError('UNSUPPORTED_ARTIFACT_FORMAT')
                artifact_format='canonical-csv-v1'
                version_id=None
            else:
                if manifest.get('format') != 'canonical-csv-v1':
                    raise RecoveryError('UNSUPPORTED_ARTIFACT_FORMAT')
                if not version_id or version_id == 'null' or manifest.get('version_id') != version_id:
                    raise RecoveryError('ARTIFACT_VERSION_REQUIRED')
                artifact_format=manifest['format']
            if (not b['output_bucket'] or not b['output_key'] or b['object_name'] != b['output_key']
                    or b['checksum'] != b['output_sha256']
                    or b['records_loaded'] is None or b['records_loaded'] < 0
                    or b['records_loaded'] != b['output_row_count']):
                raise RecoveryError('LINEAGE_MISMATCH')
            rows=parse_artifact(body,b['output_sha256'],b['records_loaded'])
            archive_path=archive_artifact(body,b['output_sha256'],archive_dir) if checksum_bound_unversioned else None
            q.execute('SELECT source_customer_id,raw_customer_id::text,source_system_id FROM raw.customer_batch_member WHERE ingestion_batch_id=%s',(batch_id,))
            members=q.fetchall()
            keys={r['source_customer_id'] for r in rows}
            if (len(members)!=len(rows) or {r['source_customer_id'] for r in members} != keys
                    or any(r['source_system_id'] != b['source_system_id'] for r in members)):
                raise RecoveryError('MEMBERSHIP_MISMATCH')
            identities={r['source_customer_id']:r['raw_customer_id'] for r in members}
            q.execute('SELECT source_customer_id,raw_customer_id::text FROM raw.customer_current WHERE source_system_id=%s AND source_customer_id=ANY(%s)',(b['source_system_id'],list(keys)))
            current=q.fetchall()
            if len(current)!=len(rows) or {r['source_customer_id']:r['raw_customer_id'] for r in current} != identities:
                raise RecoveryError('IDENTITY_MISMATCH')
            q.execute('CREATE TEMP TABLE recovery_expected (LIKE raw.customer INCLUDING DEFAULTS) ON COMMIT DROP')
            excluded=['snapshot_id','snapshot_origin','snapshot_recorded_at','created_at','updated_at','ingested_at']
            q.execute("SELECT attname FROM pg_attribute WHERE attrelid='raw.customer'::regclass AND attnum>0 AND NOT attisdropped ORDER BY attnum")
            all_columns=[r['attname'] for r in q.fetchall()]
            # Never silently ignore a newly added typed field or invoke a default
            # pretending it is reconstructed historical evidence.
            typed_columns=[c for c in all_columns if c not in excluded]
            parser_columns=list(prepare_customers(b'source_customer_id\nprobe\n')[0][0])
            # Legacy nullable alias is not written by canonical ingestion. Reconstruct
            # explicit NULL, still comparing existing evidence (never ignore it).
            if 'customer_id' in typed_columns and 'customer_id' not in parser_columns:
                parser_columns.append('customer_id')
                for row in rows:
                    row['customer_id'] = None
            # 026 adds provenance, not a CSV business field. LIKE does not copy
            # its derivation trigger: fill the expected stage from exact batch
            # lineage explicitly, never from the latest customer or CSV payload.
            q.execute('''SELECT attrelid::regclass::text AS relation,
                atttypid='text'::regtype AND atttypmod=-1 AND NOT attnotnull
                AND NOT atthasdef AND attgenerated='' AND attidentity='' AS compatible
                FROM pg_attribute WHERE attrelid IN ('raw.customer'::regclass,
                    'raw.customer_current'::regclass,'control.ingestion_batch'::regclass)
                AND attname='app_id' AND attnum>0 AND NOT attisdropped''')
            app_columns=q.fetchall()
            if app_columns and (len(app_columns)!=3 or not all(r['compatible'] for r in app_columns)):
                raise RecoveryError('TYPED_SCHEMA_MISMATCH')
            if 'app_id' in typed_columns:
                # Freeze every relation consulted by 026's lineage diagnostic,
                # including a connector registration racing this recovery.
                q.execute('LOCK TABLE control.source_system, control.stitching_job IN SHARE MODE')
                q.execute("SELECT to_regclass('connector.sync_run') IS NOT NULL AS present")
                if q.fetchone()['present']:
                    q.execute('LOCK TABLE connector.sync_run IN SHARE MODE')
                q.execute('SELECT app_id FROM control.customer_app_scope_status(%s)', (batch_id,))
                app_id=q.fetchone()['app_id']
                parser_columns.append('app_id')
                for row in rows:
                    row['app_id']=app_id
            if set(typed_columns) != set(parser_columns) | {'batch_id','raw_customer_id','source_system_id'}:
                raise RecoveryError('TYPED_SCHEMA_MISMATCH')
            columns=['raw_customer_id','batch_id','source_system_id',*parser_columns,'snapshot_origin','created_at','updated_at','ingested_at']
            values=[]
            for row in rows:
                values.append((identities[row['source_customer_id']],batch_id,b['source_system_id'],
                    *[Json(row[c]) if isinstance(row[c],dict) else row[c] for c in parser_columns],
                    'CANONICAL_RECONSTRUCTION',b['created_at'],b['created_at'],b['created_at']))
            if values:
                execute_values(q,sql.SQL('INSERT INTO recovery_expected ({}) VALUES %s').format(sql.SQL(',').join(map(sql.Identifier,columns))).as_string(q),values)
            # Compare actual PostgreSQL typed values, NULL-safe, including raw_payload,
            # nested JSON, identity and source keys. Clocks/provenance only excluded.
            q.execute('''SELECT EXISTS(SELECT 1 FROM raw.customer h
                LEFT JOIN recovery_expected e USING(batch_id,raw_customer_id)
                WHERE h.batch_id=%s AND (e.raw_customer_id IS NULL OR
                (to_jsonb(h)-%s::text[]) IS DISTINCT FROM (to_jsonb(e)-%s::text[]))) AS mismatch''',(batch_id,excluded,excluded))
            if q.fetchone()['mismatch']:
                raise RecoveryError('TYPED_HISTORY_MISMATCH')
            q.execute(sql.SQL('''INSERT INTO raw.customer ({cols}) SELECT {cols} FROM recovery_expected e
                WHERE NOT EXISTS(SELECT 1 FROM raw.customer h WHERE h.batch_id=e.batch_id AND h.raw_customer_id=e.raw_customer_id)''').format(cols=sql.SQL(',').join(map(sql.Identifier,columns))))
            inserted=q.rowcount
            q.execute('''SELECT EXISTS(
                (SELECT to_jsonb(h)-%s::text[] AS v FROM raw.customer h WHERE h.batch_id=%s
                 EXCEPT SELECT to_jsonb(e)-%s::text[] FROM recovery_expected e)
                UNION ALL
                (SELECT to_jsonb(e)-%s::text[] FROM recovery_expected e
                 EXCEPT SELECT to_jsonb(h)-%s::text[] FROM raw.customer h WHERE h.batch_id=%s)
                ) AS mismatch''',(excluded,batch_id,excluded,excluded,excluded,batch_id))
            if q.fetchone()['mismatch']:
                raise RecoveryError('POST_INSERT_MISMATCH')
            q.execute('SELECT (to_jsonb(e)-%s::text[])::text AS value FROM recovery_expected e ORDER BY source_customer_id COLLATE "C",raw_customer_id',(excluded,))
            typed_digest=hashlib.sha256(json.dumps([r['value'] for r in q.fetchall()],ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
            membership_digest=hashlib.sha256(json.dumps(sorted((r['source_customer_id'],r['raw_customer_id'],r['source_system_id']) for r in members),ensure_ascii=False,separators=(',',':')).encode()).hexdigest()
            provenance={'bucket':b['output_bucket'],'key':b['output_key'],'version_id':version_id,
                'format':artifact_format,'parser_version':PARSER_VERSION,'parser_sha256':PARSER_SHA256,
                'recovery_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                'evidence_kind':'CANONICAL_RECONSTRUCTION','source_system_id':b['source_system_id'],
                'postgres_version':connection.server_version}
            if checksum_bound_unversioned:
                provenance.update(evidence_kind='CHECKSUM_BOUND_UNVERSIONED_RECONSTRUCTION',
                    archive_path=archive_path,archive_sha256=b['output_sha256'],
                    original_snapshot_proven=False)
            expected={'landing_batch_id':b['landing_batch_id'],'expected_rows':len(rows),
                'artifact_sha256':b['output_sha256'],'typed_sha256':typed_digest,
                'membership_sha256':membership_digest,'provenance':provenance}
            q.execute('SELECT landing_batch_id::text,expected_rows,artifact_sha256,typed_sha256,membership_sha256,provenance FROM control.customer_history_recovery WHERE ingestion_batch_id=%s',(batch_id,))
            ledger=q.fetchone()
            if ledger is not None and (dict(ledger)!=expected or inserted):
                raise RecoveryError('LEDGER_MISMATCH')
            if ledger is None:
                q.execute('''INSERT INTO control.customer_history_recovery(ingestion_batch_id,landing_batch_id,status,expected_rows,inserted_rows,artifact_sha256,typed_sha256,membership_sha256,provenance)
                    VALUES (%s,%s,'VERIFIED',%s,%s,%s,%s,%s,%s)''',
                    (batch_id,b['landing_batch_id'],len(rows),inserted,b['output_sha256'],typed_digest,membership_digest,Json(provenance)))
        if apply:
            connection.commit()
        else:
            connection.rollback()
        return {'batch_id':batch_id,'status':'VERIFIED' if apply else 'AUDIT_PASS','rows':len(rows),'inserted_rows':inserted,'artifact_sha256':b['output_sha256']}
    except Exception as error:
        connection.rollback()
        if isinstance(error,RecoveryError):
            raise
        raise RecoveryError('RECOVERY_FAILED_'+type(error).__name__) from None


def main():
    """Operator supplies downloaded evidence; libpq service auth only."""
    import argparse
    import json
    import psycopg2
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--service',required=True,help='Configured libpq service name; never pass credentials on command line')
    parser.add_argument('--batch-id',required=True)
    parser.add_argument('--artifact',required=True,type=Path)
    parser.add_argument('--artifact-version-id')
    parser.add_argument('--checksum-bound-unversioned',action='store_true')
    parser.add_argument('--archive-dir',type=Path,help='Required private content-addressed archive for unversioned reconstruction')
    parser.add_argument('--apply',action='store_true',help='Commit history and ledger; default exercises then rolls back')
    args=parser.parse_args()
    try:
        with psycopg2.connect(service=args.service,application_name='customer-history-recovery') as conn:
            result=recover_batch(conn,args.batch_id,args.artifact.read_bytes(),args.artifact_version_id,apply=args.apply,
                checksum_bound_unversioned=args.checksum_bound_unversioned,archive_dir=args.archive_dir)
        print(json.dumps(result,sort_keys=True))
        return 0
    except Exception as error:
        # No traceback, server DETAIL, DSN or artifact payload in operator logs.
        code=str(error) if isinstance(error,RecoveryError) else type(error).__name__
        print(json.dumps({'status':'FAILED','error_code':code},sort_keys=True))
        return 1


if __name__=='__main__':
    raise SystemExit(main())
