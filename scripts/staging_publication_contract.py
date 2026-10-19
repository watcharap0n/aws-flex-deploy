"""Use the deployed publisher's serving contract on staging ClickHouse."""
import json
import subprocess
from urllib.request import Request, urlopen


def main():
    data = json.loads(subprocess.check_output(['docker', 'inspect', 'staging-databases-clickhouse-1']))[0]
    env = dict(item.split('=', 1) for item in data['Config']['Env'])
    headers = {'X-ClickHouse-User': env['CLICKHOUSE_USER'], 'X-ClickHouse-Key': env['CLICKHOUSE_PASSWORD']}

    def query(sql):
        with urlopen(Request('http://127.0.0.1:8123/', data=sql.encode(), headers=headers), timeout=60) as response:
            return response.read().decode()

    query('ALTER USER identity_publication_definer SETTINGS enable_shared_storage_snapshot_in_query=1')
    backup = 'customer_profile_production_catalog_20261005'
    if query(f"SELECT count() FROM system.tables WHERE database='cdp_customer' AND name='{backup}'").strip() == '0':
        query(f'RENAME TABLE cdp_customer.customer_profile TO cdp_customer.{backup}')
    candidate = query("SELECT create_table_query FROM system.tables WHERE database='cdp_customer' "
                      "AND name='customer_profile_serving_candidate' FORMAT TabSeparatedRaw").strip()
    ddl = candidate.replace('CREATE VIEW cdp_customer.customer_profile_serving_candidate',
                            'CREATE OR REPLACE VIEW cdp_customer.customer_profile', 1)
    ddl = ddl.replace(' AS SELECT ', " DEFINER = identity_publication_definer SQL SECURITY DEFINER "
                      "COMMENT 'customer-identity-tfg:v2' AS SELECT ", 1)
    query(ddl)
    print('Staging publisher serving contract installed')


if __name__ == '__main__':
    main()
