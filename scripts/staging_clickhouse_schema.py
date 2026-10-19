"""Install a data-free production ClickHouse catalog on the staging EC2 only."""
import argparse
import json
import re
import subprocess
from pathlib import Path
from urllib.request import Request, urlopen
from urllib.error import HTTPError

CATALOG_QUERY = """SELECT database,name,engine,create_table_query,sorting_key,partition_key
FROM system.tables WHERE database NOT IN ('system','information_schema','INFORMATION_SCHEMA')
ORDER BY database,name FORMAT JSON"""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('catalog')
    parser.add_argument('--output', required=True)
    args = parser.parse_args()
    # This utility must never accept a production endpoint or container.
    inspect = json.loads(subprocess.check_output([
        'docker', 'inspect', 'staging-databases-clickhouse-1']))[0]
    env = dict(item.split('=', 1) for item in inspect['Config']['Env'])
    headers = {'X-ClickHouse-User': env['CLICKHOUSE_USER'],
               'X-ClickHouse-Key': env['CLICKHOUSE_PASSWORD']}

    def query(sql):
        with urlopen(Request('http://127.0.0.1:8123/', data=sql.encode(),
                             headers=headers), timeout=60) as response:
            return response.read().decode()

    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    before = json.loads(query(CATALOG_QUERY))['data']
    (output / 'staging-before.json').write_text(json.dumps(before, indent=2))
    source = json.loads(Path(args.catalog).read_text())
    source = [t for t in source if not t['name'].startswith('.')]
    for database in sorted({t['database'] for t in source}):
        query(f'CREATE DATABASE IF NOT EXISTS `{database}` ENGINE = Atomic')
    for user in ('identity_publication_definer', 'buzz_segment'):
        query(f'CREATE USER IF NOT EXISTS {user} HOST NONE')
        query(f'GRANT SELECT ON *.* TO {user}')
    query('GRANT INSERT, CREATE TABLE, DROP TABLE ON segment.* TO buzz_segment')
    existing = {(t['database'], t['name']): t for t in before}
    profile = existing.get(('cdp_customer', 'customer_profile'))
    if profile and 'customer_profile_stage' in profile['create_table_query']:
        query('RENAME TABLE cdp_customer.customer_profile TO '
              'cdp_customer.customer_profile_manual_staging_20261005')
        del existing[('cdp_customer', 'customer_profile')]
    pending = [t for t in source if (t['database'], t['name']) not in existing]
    failures = {}
    while pending:
        remaining = []
        for table in pending:
            name = table['database'] + '.' + table['name']
            ddl = table['create_table_query']
            if re.search(r'\b(remote|remoteSecure|s3|mysql|postgresql|url)\s*\(', ddl, re.I):
                raise RuntimeError('external source in schema: ' + name)
            try:
                query(ddl)
                failures.pop(name, None)
            except HTTPError as error:
                # DDL-only response; retain code, never arbitrary server text.
                body = error.read().decode(errors='replace')
                code = re.search(r'Code: (\d+)', body)
                failures[name] = code.group(1) if code else str(error.code)
                remaining.append(table)
        print(json.dumps({'remaining': len(remaining)}), flush=True)
        if len(remaining) == len(pending):
            (output / 'failures.json').write_text(json.dumps(failures, indent=2))
            raise RuntimeError('schema dependency pass stalled: ' + json.dumps(failures))
        pending = remaining
    after = json.loads(query(CATALOG_QUERY))['data']
    (output / 'staging-after.json').write_text(json.dumps(after, indent=2))
    target = {(t['database'], t['name']): t for t in after}
    mismatches = []
    for t in source:
        other = target.get((t['database'], t['name']))
        if not other or any(other[k] != t[k] for k in
                            ('engine', 'create_table_query', 'sorting_key', 'partition_key')):
            mismatches.append(t['database'] + '.' + t['name'])
    report = {'source_objects': len(source), 'target_objects': len(after),
              'ddl_mismatches': mismatches, 'version': query('SELECT version()').strip()}
    (output / 'comparison.json').write_text(json.dumps(report, indent=2))
    print(json.dumps(report), flush=True)


if __name__ == '__main__':
    main()
