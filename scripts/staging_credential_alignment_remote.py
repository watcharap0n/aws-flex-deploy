"""Staging-only credential alignment. Receives secret material over SSH stdin."""
import json
import os
import subprocess
from pathlib import Path
from urllib.request import Request, urlopen

PG = 'staging-databases-postgres-1'
CH = 'staging-databases-clickhouse-1'


def literal(value):
    return "'" + str(value).replace("'", "''") + "'"


def pg(sql, db='template1', user='postgres'):
    result = subprocess.run(['sudo', 'docker', 'exec', '-i', PG, 'psql', '-X',
        '-v', 'ON_ERROR_STOP=1', '-U', user, '-d', db, '-At'], input=sql,
        text=True, capture_output=True)
    if result.returncode:
        # Never forward SQL containing passwords to logs.
        safe_error = '\n'.join(line for line in result.stderr.splitlines()
                               if line.startswith(('ERROR:', 'DETAIL:', 'HINT:')))
        raise RuntimeError('PostgreSQL operation failed: ' + safe_error[:600])
    return result.stdout


def clickhouse(sql, user=None, password=None, database=None):
    data = json.loads(subprocess.check_output(['sudo', 'docker', 'inspect', CH]))[0]
    env = dict(e.split('=', 1) for e in data['Config']['Env'])
    url = 'http://127.0.0.1:8123/' + ('?database=' + database if database else '')
    with urlopen(Request(url, data=sql.encode(), headers={
        'X-ClickHouse-User': user or env['CLICKHOUSE_USER'],
        'X-ClickHouse-Key': password or env['CLICKHOUSE_PASSWORD']}), timeout=90) as r:
        return r.read().decode()


def prepare(payload):
    passwords = payload['passwords']
    for role in ['postgres', 'buzzcdp_app', 'identity_publication_worker',
                 'customer_app_history_backfill', 'journey_builder_dev']:
        exists = pg('SELECT 1 FROM pg_roles WHERE rolname=' + literal(role), user='staging').strip()
        if not exists:
            pg('CREATE ROLE ' + role + ' LOGIN ' + ('SUPERUSER ' if role == 'postgres' else '') +
               'PASSWORD ' + literal(passwords[role]), user='staging')
    pg("DO $$BEGIN IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname='journey_builder_full_access') THEN CREATE ROLE journey_builder_full_access NOLOGIN; END IF; END$$;" +
       'GRANT journey_builder_full_access,pg_read_all_data TO journey_builder_dev;')
    pg('ALTER ROLE buzzcdp_app SET default_transaction_read_only=on;')
    ch_password = passwords['clickhouse']
    clickhouse('CREATE USER IF NOT EXISTS identity_publication_worker IDENTIFIED WITH sha256_password BY ' + literal(ch_password))
    clickhouse('ALTER USER buzz_segment IDENTIFIED WITH sha256_password BY ' + literal(ch_password) + " HOST ANY DEFAULT DATABASE segment")
    clickhouse('REVOKE ALL ON *.* FROM buzz_segment')
    for user, statements in payload['clickhouse_grants'].items():
        for statement in statements:
            clickhouse(statement)
    clickhouse('ALTER USER identity_publication_worker DEFAULT DATABASE cdp_customer SETTINGS enable_shared_storage_snapshot_in_query=1')
    print('Production-named roles prepared; ClickHouse grants applied')


def migrate(payload):
    # Caller must have drained Lambda invocations and ECS writers before this step.
    assert payload['drained'] is True
    pg("SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname IN ('postgres','staging','journey_builder_staging') AND pid<>pg_backend_pid();")
    databases = pg('SELECT datname FROM pg_database').splitlines()
    if 'staging' in databases:
        count = pg("SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname NOT LIKE 'pg_%' AND n.nspname!='information_schema';", db='postgres').strip()
        assert count == '0', 'Existing postgres database is not empty'
        pg('ALTER DATABASE postgres RENAME TO postgres_before_credential_alignment_20261007;')
        pg('ALTER DATABASE staging RENAME TO postgres;')
    if 'journey_builder_staging' in databases:
        pg('ALTER DATABASE journey_builder_staging RENAME TO journey_builder;')
    dbs = [d for d in pg("SELECT datname FROM pg_database WHERE NOT datistemplate").splitlines()]
    # The original Docker POSTGRES_USER is initdb's bootstrap role (OID 10).
    # PostgreSQL forbids reassigning its system objects. Rename that role instead
    # of dropping it, preserving catalog ownership and all existing object OIDs.
    old_bootstrap = pg("SELECT oid FROM pg_roles WHERE rolname='staging'").strip()
    if old_bootstrap:
        assert old_bootstrap == '10', 'Unexpected bootstrap role identity'
        pg("CREATE ROLE credential_alignment_admin LOGIN SUPERUSER;")
        for db in dbs:
            pg('REASSIGN OWNED BY postgres TO staging; DROP OWNED BY postgres;',
               db=db, user='credential_alignment_admin')
        pg('DROP ROLE postgres; ALTER ROLE staging RENAME TO postgres;',
           user='credential_alignment_admin')
        pg('DROP ROLE credential_alignment_admin;')
    for db in ['journey_builder', 'temporal_staging', 'temporal_visibility_staging', 'cdp_jobs']:
        pg('ALTER DATABASE "' + db + '" OWNER TO journey_builder_full_access;')
        # DDL requires ownership, not just table grants. Mirror Journey's owner
        # role so its existing migrations can run through inherited membership.
        pg("""DO $$DECLARE r record; BEGIN
          FOR r IN SELECT n.nspname FROM pg_namespace n
            WHERE n.nspowner='postgres'::regrole AND n.nspname NOT LIKE 'pg_%'
              AND n.nspname NOT IN ('public','information_schema') LOOP
            EXECUTE format('ALTER SCHEMA %I OWNER TO journey_builder_full_access',r.nspname);
          END LOOP;
          FOR r IN SELECT n.nspname,c.relname,c.relkind FROM pg_class c
            JOIN pg_namespace n ON n.oid=c.relnamespace
            WHERE c.relowner='postgres'::regrole AND c.relkind IN ('r','p','S','v','m')
              AND n.nspname NOT LIKE 'pg_%' AND n.nspname!='information_schema'
            ORDER BY CASE c.relkind WHEN 'S' THEN 1 ELSE 0 END LOOP
            EXECUTE format('ALTER %s %I.%I OWNER TO journey_builder_full_access',
              CASE r.relkind WHEN 'S' THEN 'SEQUENCE' WHEN 'v' THEN 'VIEW'
                WHEN 'm' THEN 'MATERIALIZED VIEW' ELSE 'TABLE' END,r.nspname,r.relname);
          END LOOP;
          FOR r IN SELECT n.nspname,p.proname,pg_get_function_identity_arguments(p.oid) args
            FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
            WHERE p.proowner='postgres'::regrole AND p.prokind='f'
              AND n.nspname NOT LIKE 'pg_%' AND n.nspname!='information_schema' LOOP
            EXECUTE format('ALTER FUNCTION %I.%I(%s) OWNER TO journey_builder_full_access',r.nspname,r.proname,r.args);
          END LOOP;
        END$$;""", db=db)
    pg('CREATE SCHEMA IF NOT EXISTS dev_workspace AUTHORIZATION journey_builder_dev;', db='journey_builder')
    mapping = {'temporal': 'temporal_staging', 'temporal_visibility': 'temporal_visibility_staging'}
    for prod_db, statements in payload['postgres_grants'].items():
        db = mapping.get(prod_db, prod_db)
        if db not in dbs:
            raise RuntimeError('Required database missing: ' + db)
        if statements:
            pg('BEGIN;' + ';'.join(statements) + ';COMMIT;', db=db)
    pg('GRANT CONNECT ON DATABASE postgres TO buzzcdp_app,identity_publication_worker,customer_app_history_backfill; GRANT CONNECT ON DATABASE journey_builder,temporal_staging,temporal_visibility_staging,cdp_jobs TO journey_builder_dev; GRANT CREATE,TEMPORARY ON DATABASE cdp_jobs TO journey_builder_dev;')
    print('Database names migrated; production grants applied')


def configure(payload):
    path = Path('/home/ec2-user/staging-databases/.env')
    lines = path.read_text().splitlines()
    replacements = {'POSTGRES_USER': 'postgres', 'POSTGRES_DB': 'postgres',
                    'CLICKHOUSE_USER': 'app', 'CLICKHOUSE_DB': 'segment'}
    path.write_text('\n'.join(k + '=' + replacements[k] if '=' in line and
        (k := line.split('=', 1)[0]) in replacements else line for line in lines) + '\n')
    os.chmod(path, 0o600)
    compose = path.parent / 'compose.yaml'
    text = compose.read_text()
    for old, new in [('POSTGRES_DB: staging', 'POSTGRES_DB: postgres'),
                     ('POSTGRES_USER: staging', 'POSTGRES_USER: postgres'),
                     ('CLICKHOUSE_DB: staging', 'CLICKHOUSE_DB: segment'),
                     ('CLICKHOUSE_USER: staging', 'CLICKHOUSE_USER: app')]:
        text = text.replace(old, new)
    text = text.replace('pg_isready -U staging -d staging',
                        'pg_isready -U postgres -d postgres')
    text = text.replace('clickhouse-client --user staging',
                        'clickhouse-client --user app')
    compose.write_text(text)
    print('Compose database environment updated')


if __name__ == '__main__':
    raise RuntimeError('Run with a staging SSH payload; never put credentials in argv')
