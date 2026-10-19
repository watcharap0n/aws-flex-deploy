"""Prevent production database/cluster bindings in the staging worker stack."""
from copy import deepcopy
from pathlib import Path
import sys
import aws_cdk as cdk
from aws_cdk.assertions import Template
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from cdk_stacks.staging_customer_runtime_stack import StagingCustomerRuntimeStack


def config():
    return yaml.safe_load((ROOT / 'env.staging.yaml').read_text())['staging_customer_runtime_stack']['project_config']


def test_staging_services_use_separate_staging_database_bindings(tmp_path):
    c = deepcopy(config())
    c['desired_count'] = 1
    stack = StagingCustomerRuntimeStack(cdk.App(outdir=str(tmp_path)), 'CdpStgTest',
        project_config=c, env=cdk.Environment(account='538783577018', region='ap-southeast-7'))
    resources = Template.from_stack(stack).to_json()['Resources']
    services = [r['Properties'] for r in resources.values() if r['Type'] == 'AWS::ECS::Service']
    assert len(services) == 2
    assert all(s['DesiredCount'] == 1 for s in services)
    assert all(s['Cluster'] == 'cdp-stg-stitcher-cluster' for s in services)
    tasks = [r['Properties'] for r in resources.values() if r['Type'] == 'AWS::ECS::TaskDefinition']
    assert len(tasks) == 3
    expected = {
        'customer-identity-publication': c['publication_db_secret_arn'],
        'customer-upload-resolution-worker': c['upload_resolution_db_secret_arn'],
        'cdp-stitcher-container': c['db_secret_arn'],
    }
    for task in tasks:
        container = task['ContainerDefinitions'][0]
        env = {e['Name']: e['Value'] for e in container['Environment']}
        assert env['DB_SECRET_NAME'] == expected[container['Name']]
        assert env['DB_SSLMODE'] == 'disable'
        if container['Name'] == 'customer-identity-publication':
            assert all(s['ValueFrom'].startswith(c['publication_clickhouse_secret_arn'])
                       for s in container['Secrets'])


@pytest.mark.parametrize('field,value', [
    ('cluster_name', 'cdp-stitcher-cluster'),
    ('db_secret_arn', 'arn:aws:secretsmanager:ap-southeast-7:538783577018:secret:marketing-automation-secret-vWleGD'),
    ('clickhouse_url', 'https://172.31.44.89:8443'),
    ('publication_db_secret_arn', 'arn:aws:secretsmanager:ap-southeast-7:538783577018:secret:cdp/identity-publication/postgres-ou00Q5'),
    ('upload_resolution_db_secret_arn', 'arn:aws:secretsmanager:ap-southeast-7:538783577018:secret:cdp/customer-app-history-backfill/postgres-fmRbTF'),
    ('publication_clickhouse_secret_arn', 'arn:aws:secretsmanager:ap-southeast-7:538783577018:secret:cdp/identity-publication/clickhouse-fH73QH'),
])
def test_production_bindings_are_rejected(field, value, tmp_path):
    c = deepcopy(config()); c[field] = value
    with pytest.raises(ValueError):
        StagingCustomerRuntimeStack(cdk.App(outdir=str(tmp_path)), 'CdpStgTest',
            project_config=c, env=cdk.Environment(account='538783577018', region='ap-southeast-7'))
