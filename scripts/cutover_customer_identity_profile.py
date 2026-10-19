#!/usr/bin/env python3
"""Opt-in, offline compatibility cutover. Never called by the transfer daemon.

Stop EVERY old profile writer and publication worker before calling. A single
EXCHANGE makes the overlay live; original rows remain in a rollback table.
This does not manufacture mapping rows for legacy commerce-profile data.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import urllib.error
import urllib.request
from urllib.parse import urlsplit


class CutoverError(RuntimeError):
    pass


def identifier(value: str) -> str:
    if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', value):
        raise CutoverError('INVALID_IDENTIFIER')
    return value


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never resend credentials or SQL to a redirect target, even same-origin.
        return None


class Client:
    def __init__(self, url: str, user: str | None = None, password: str | None = None):
        parsed = urlsplit(url)
        if parsed.scheme not in {'http', 'https'} or parsed.username or parsed.password:
            raise CutoverError('INVALID_ENDPOINT')
        if parsed.scheme == 'http' and parsed.hostname not in {'127.0.0.1', 'localhost'}:
            raise CutoverError('REMOTE_TLS_REQUIRED')
        self.url = url
        self.headers = {'Content-Type': 'text/plain; charset=utf-8'}
        if user is not None:
            auth = base64.b64encode(f'{user}:{password or ""}'.encode()).decode()
            self.headers['Authorization'] = 'Basic ' + auth

    def open(self, sql: str):
        request = urllib.request.Request(self.url, data=sql.encode(), headers=self.headers, method='POST')
        try:
            return urllib.request.build_opener(NoRedirect()).open(request, timeout=300)
        except (urllib.error.URLError, TimeoutError, OSError):
            # Server bodies and URLs can contain PII/credentials. Do not echo.
            raise CutoverError('CLICKHOUSE_REQUEST_FAILED_RECONCILE_BEFORE_RETRY') from None

    def query(self, sql: str) -> str:
        with self.open(sql) as response:
            return response.read().decode()

    def rows(self, sql: str) -> list[dict]:
        return [json.loads(line) for line in self.query(sql + ' FORMAT JSONEachRow').splitlines()]

    def fingerprint(self, table: str) -> str:
        digest = hashlib.sha256()
        with self.open(f'SELECT * FROM {table} ORDER BY app_id,customer_id FORMAT JSONEachRow') as response:
            while chunk := response.read(1024 * 1024):
                digest.update(chunk)
        return digest.hexdigest()


def type_literal(value: str) -> str:
    return "'" + value.replace('\\', '\\\\').replace("'", "\\'") + "'"


def overlay_select(resolved: str, legacy: str, original_columns: list[dict], resolved_columns: list[dict]) -> str:
    original = {r['name']: r['type'] for r in original_columns}
    if not {'app_id', 'customer_id', 'customer_key_id', 'profile_source_record_id'} <= original.keys():
        raise CutoverError('UNSUPPORTED_LEGACY_COLUMNS')
    if not original.keys() <= {r['name'] for r in resolved_columns}:
        raise CutoverError('RESOLVED_CONTRACT_REMOVES_LEGACY_COLUMNS')
    live_expr, legacy_expr = [], []
    for col in resolved_columns:
        name, kind = col['name'], col['type']
        identifier(name)
        if name in original and kind != original[name]:
            if name not in {'customer_key_id', 'profile_source_record_id'} or kind != f'Nullable({original[name]})':
                raise CutoverError('UNSUPPORTED_COLUMN_TYPE_CHANGE')
        elif name not in original and not re.fullmatch(r'(?:UUID|U?Int64|String|Nullable\((?:UUID|U?Int64|String)\))', kind):
            raise CutoverError('UNSUPPORTED_LINEAGE_TYPE')
        if name in original:
            common_type = kind
            legacy_expr.append(f'CAST(`{name}`,{type_literal(common_type)}) AS `{name}`')
        else:
            # Legacy has no resolution/entity lineage, never pretend zero UUID.
            common_type = kind if kind.startswith('Nullable(') else f'Nullable({kind})'
            legacy_expr.append(f'CAST(NULL,{type_literal(common_type)}) AS `{name}`')
        live_expr.append(f'CAST(`{name}`,{type_literal(common_type)}) AS `{name}`')
    # One resolved scan per statement: two marker reads could straddle publication.
    names = [col['name'] for col in resolved_columns]
    chosen = ','.join(f'tupleElement(selected,{i}) AS `{name}`' for i, name in enumerate(names, 1))
    values = ','.join(f'`{name}`' for name in names)
    return f'SELECT {chosen} FROM (SELECT argMax(tuple({values}), _identity_priority) AS selected FROM (SELECT {",".join(live_expr)},toUInt8(1) AS _identity_priority FROM {resolved} UNION ALL SELECT {",".join(legacy_expr)},toUInt8(0) AS _identity_priority FROM {legacy}) GROUP BY app_id,customer_id)'


def cutover(client: Client, database: str = 'cdp_customer', *, writers_stopped: bool = False) -> dict:
    db = identifier(database)
    if not writers_stopped:
        raise CutoverError('WRITERS_NOT_STOPPED')
    live = f'{db}.customer_profile'
    resolved = f'{db}.customer_profile_resolved'
    legacy = f'{db}.customer_profile_legacy_snapshot'
    candidate = f'{db}.customer_profile_identity_candidate'
    rollback = f'{db}.customer_profile_pre_identity'
    tables = {r['name']: r for r in client.rows(f"SELECT name,engine,uuid,comment,create_table_query FROM system.tables WHERE database='{db}'")}
    if 'customer_profile' not in tables or 'customer_profile_resolved' not in tables:
        raise CutoverError('MISSING_PROFILE_CONTRACT')
    if tables['customer_profile_resolved']['engine'] not in {'View', 'MergeTree'}:
        raise CutoverError('UNSUPPORTED_RESOLVED_ENGINE')
    if 'customer_profile_legacy_snapshot' in tables and tables['customer_profile_legacy_snapshot']['engine'] != 'MergeTree':
        raise CutoverError('INVALID_LEGACY_SNAPSHOT_ENGINE')
    invalid = client.query(f"SELECT count()-uniqExact(tuple(app_id,customer_id))+countIf(empty(trimBoth(app_id)) OR empty(trimBoth(customer_id))) FROM {resolved}").strip()
    if invalid != '0':
        raise CutoverError('RESOLVED_GRAIN_INVALID')
    if tables['customer_profile']['engine'] == 'View':
        if 'customer_profile_legacy_snapshot' not in tables:
            raise CutoverError('MISSING_LEGACY_SNAPSHOT')
        # The view travels through EXCHANGE with durable original-table identity.
        identity = re.fullmatch(r'customer-identity-cutover:v1:([0-9a-f-]{36}):([0-9a-f]{64})', tables['customer_profile']['comment'])
        source_name = 'customer_profile_pre_identity' if 'customer_profile_pre_identity' in tables else 'customer_profile_identity_candidate'
        source = tables.get(source_name, {})
        if not identity or source.get('engine') != 'MergeTree' or source.get('uuid') != identity[1]:
            raise CutoverError('INVALID_ROLLBACK_IDENTITY')
        if client.fingerprint(f'{db}.{source_name}') != identity[2]:
            raise CutoverError('INVALID_ROLLBACK_IDENTITY')
        original_columns = client.rows(f"SELECT name,type FROM system.columns WHERE database='{db}' AND table='{source_name}' ORDER BY position")
        resolved_columns = client.rows(f"SELECT name,type FROM system.columns WHERE database='{db}' AND table='customer_profile_resolved' ORDER BY position")
        legacy_columns = client.rows(f"SELECT name,type FROM system.columns WHERE database='{db}' AND table='customer_profile_legacy_snapshot' ORDER BY position")
        if legacy_columns != original_columns:
            raise CutoverError('INVALID_LEGACY_SNAPSHOT_COLUMNS')
        if client.fingerprint(legacy) != identity[2]:
            raise CutoverError('LEGACY_COPY_MISMATCH_OR_WRITER_ACTIVE')
        expected = overlay_select(resolved, legacy, original_columns, resolved_columns)
        original_names = {r['name'] for r in original_columns}
        expected_columns = [{'name': r['name'], 'type': r['type'] if r['name'] in original_names or r['type'].startswith('Nullable(') else f"Nullable({r['type']})"} for r in resolved_columns]
        live_columns = client.rows(f"SELECT name,type FROM system.columns WHERE database='{db}' AND table='customer_profile' ORDER BY position")
        if live_columns != expected_columns:
            raise CutoverError('UNEXPECTED_EXISTING_VIEW_COLUMNS')
        # Compare the whole trusted CREATE, including defaults and the fixed
        # security principal; a matching SELECT alone does not protect readers.
        defs = ','.join(f'`{c["name"]}` {c["type"]}' for c in expected_columns)
        expected_ddl = (f'CREATE VIEW {live} ({defs}) '
                        'DEFINER = identity_publication_definer SQL SECURITY DEFINER '
                        f'COMMENT {type_literal(identity[0])} AS {expected}')
        actual_ddl = tables['customer_profile']['create_table_query']
        equal = client.rows(f'SELECT formatQuerySingleLine({type_literal(expected_ddl)}) = '
                            f'formatQuerySingleLine({type_literal(actual_ddl)}) AS ok')
        if equal != [{'ok': 1}]:
            raise CutoverError('UNEXPECTED_EXISTING_VIEW')
        # count() may optimize away expressions; read every column and discard
        # only the output so dangling dependencies and runtime errors fail closed.
        client.query(f'SELECT * FROM {resolved} FORMAT Null')
        client.query(f'SELECT * FROM {live} FORMAT Null')
        # Recover exchange acknowledgement loss before the rollback rename.
        if source_name == 'customer_profile_identity_candidate':
            client.query(f'RENAME TABLE {candidate} TO {rollback}')
            renamed = client.rows(f"SELECT engine,uuid FROM system.tables WHERE database='{db}' AND name='customer_profile_pre_identity'")
            if renamed != [{'engine': 'MergeTree', 'uuid': identity[1]}] or client.fingerprint(rollback) != identity[2]:
                raise CutoverError('INVALID_ROLLBACK_IDENTITY')
        return {'status': 'ACTIVE', 'profile': live, 'rollback': rollback}
    if tables['customer_profile']['engine'] != 'MergeTree':
        raise CutoverError('UNSUPPORTED_LEGACY_ENGINE')
    if 'customer_profile_pre_identity' in tables:
        raise CutoverError('ROLLBACK_NAME_ALREADY_EXISTS')
    invalid = client.query(f"SELECT count()-uniqExact(tuple(app_id,customer_id))+countIf(empty(trimBoth(app_id)) OR empty(trimBoth(customer_id))) FROM {live}").strip()
    if invalid != '0':
        raise CutoverError('LEGACY_GRAIN_INVALID')
    original_columns = client.rows(f"SELECT name,type FROM system.columns WHERE database='{db}' AND table='customer_profile' ORDER BY position")
    resolved_columns = client.rows(f"SELECT name,type FROM system.columns WHERE database='{db}' AND table='customer_profile_resolved' ORDER BY position")
    expected = overlay_select(resolved, legacy, original_columns, resolved_columns)
    before = client.fingerprint(live)
    if 'customer_profile_legacy_snapshot' not in tables:
        client.query(f'CREATE TABLE {legacy} AS {live}')
        client.query(f'INSERT INTO {legacy} SELECT * FROM {live}')
    legacy_columns = client.rows(f"SELECT name,type FROM system.columns WHERE database='{db}' AND table='customer_profile_legacy_snapshot' ORDER BY position")
    if legacy_columns != original_columns:
        raise CutoverError('INVALID_LEGACY_SNAPSHOT_COLUMNS')
    if client.fingerprint(legacy) != before or client.fingerprint(live) != before:
        raise CutoverError('LEGACY_COPY_MISMATCH_OR_WRITER_ACTIVE')

    if 'customer_profile_identity_candidate' in tables:
        if tables['customer_profile_identity_candidate']['engine'] != 'View':
            raise CutoverError('UNEXPECTED_CANDIDATE_TABLE')
        client.query(f'DROP VIEW {candidate}')
    identity = f"customer-identity-cutover:v1:{tables['customer_profile']['uuid']}:{before}"
    client.query(f'CREATE VIEW {candidate} DEFINER = identity_publication_definer SQL SECURITY DEFINER COMMENT {type_literal(identity)} AS {expected}')
    # Read every dependency/overlay column before changing the public table.
    client.query(f'SELECT * FROM {resolved} FORMAT Null')
    client.query(f'SELECT * FROM {candidate} FORMAT Null')
    client.query(f'EXCHANGE TABLES {live} AND {candidate}')
    engine = client.query(f"SELECT engine FROM system.tables WHERE database='{db}' AND name='customer_profile'").strip()
    if engine != 'View':
        raise CutoverError('CUTOVER_NOT_VISIBLE')
    # Reuse the retry validation before reporting success or finalizing rollback.
    report = cutover(client, db, writers_stopped=writers_stopped)
    return {**report, 'legacy_sha256': before}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', required=True)
    parser.add_argument('--database', default='cdp_customer')
    parser.add_argument('--writers-stopped', action='store_true', help='Operator attests ALL old and new profile writers are stopped')
    args = parser.parse_args()
    try:
        result = cutover(Client(args.url, os.environ.get('CLICKHOUSE_USER'), os.environ.get('CLICKHOUSE_PASSWORD')), args.database, writers_stopped=args.writers_stopped)
    except CutoverError as exc:
        parser.exit(1, str(exc) + '\n')
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    main()
