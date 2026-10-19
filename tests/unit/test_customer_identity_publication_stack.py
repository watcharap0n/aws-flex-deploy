"""Offline publication-only synthesis; never launch a worker by default."""
import json
from pathlib import Path
import sys

import aws_cdk as cdk
import pytest
from aws_cdk.assertions import Template

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
ACCOUNT = '123456789012'
REGION = 'ap-southeast-7'


def config():
    return {
        'cluster_name': 'existing-customer',
        'image_uri': f'{ACCOUNT}.dkr.ecr.{REGION}.amazonaws.com/publication@sha256:' + 'a' * 64,
        'db_secret_arn': f'arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:publication-pg-abcdef',
        'clickhouse_secret_arn': f'arn:aws:secretsmanager:{REGION}:{ACCOUNT}:secret:publication-ch-abcdef',
        'clickhouse_url': 'https://clickhouse-01.internal:8443',
        'clickhouse_database': 'cdp_customer',
        'clickhouse_single_server_confirmed': True,
        'vpc': {
            'vpc_id': 'vpc-0123456789abcdef0',
            'availability_zones': [REGION + 'a', REGION + 'b'],
            'private_subnet_ids': ['subnet-0123456789abcdef0', 'subnet-1123456789abcdef0'],
            'security_group_ids': ['sg-0123456789abcdef0'],
        },
    }


def synth(tmp_path, changes=None):
    module = ROOT / 'cdk_stacks/customer_identity_publication_stack.py'
    assert module.exists(), 'publication stack missing'
    from cdk_stacks.customer_identity_publication_stack import CustomerIdentityPublicationStack

    app = cdk.App(outdir=str(tmp_path / 'cdk.out'))
    stack = CustomerIdentityPublicationStack(
        app, 'Publication', project_config=dict(config(), **(changes or {})),
        env=cdk.Environment(account=ACCOUNT, region=REGION),
    )
    template = Template.from_stack(stack).to_json()
    app.synth()
    manifest = json.loads((tmp_path / 'cdk.out/manifest.json').read_text())
    assert not manifest.get('missing'), 'synth must not need AWS context lookups'
    return template


def resources(template, resource_type):
    return [r['Properties'] for r in template['Resources'].values() if r['Type'] == resource_type]


def test_default_service_is_staged_with_no_running_tasks(tmp_path):
    template = synth(tmp_path)
    services = resources(template, 'AWS::ECS::Service')
    assert len(services) == 1, 'create even at zero so activation is a CFN update'
    service = services[0]
    assert service['DesiredCount'] == 0
    assert service['CapacityProviderStrategy'] == [{'CapacityProvider': 'FARGATE', 'Weight': 1}]
    assert service['DeploymentConfiguration']['MinimumHealthyPercent'] == 0
    assert service['DeploymentConfiguration']['MaximumPercent'] == 100
    assert service['EnableExecuteCommand'] is False
    network = service['NetworkConfiguration']['AwsvpcConfiguration']
    assert network == {
        'AssignPublicIp': 'DISABLED',
        'Subnets': config()['vpc']['private_subnet_ids'],
        'SecurityGroups': config()['vpc']['security_group_ids'],
    }
    assert resources(template, 'AWS::EC2::SecurityGroupIngress') == []
    assert resources(template, 'AWS::EC2::SecurityGroupEgress') == []
    assert resources(template, 'AWS::ApplicationAutoScaling::ScalableTarget') == []
    assert resources(template, 'AWS::Lambda::Function') == []
    assert resources(template, 'AWS::Events::Rule') == []


def test_publication_worker_uses_bounded_daemon_and_dedicated_credentials(tmp_path):
    template = synth(tmp_path)
    definition, = resources(template, 'AWS::ECS::TaskDefinition')
    assert definition['Cpu'] == '1024'
    assert definition['Memory'] == '2048'
    assert definition['RuntimePlatform'] == {'CpuArchitecture': 'X86_64', 'OperatingSystemFamily': 'LINUX'}
    worker, = definition['ContainerDefinitions']
    assert worker['Image'] == config()['image_uri']
    assert worker['EntryPoint'] == ['python', '-m', 'identity_stitcher.publication']
    assert worker['Command'] == [
        '--daemon', '--batch-rows', '10000', '--batch-bytes', '8388608',
        '--retry-seconds', '60', '--poll-seconds', '10',
    ]
    assert worker['ReadonlyRootFilesystem'] is True
    assert definition['Volumes'] == [{'Name': 'publication-scratch'}]
    assert worker['MountPoints'] == [{'ContainerPath': '/var/tmp/identity-publication', 'ReadOnly': False, 'SourceVolume': 'publication-scratch'}]
    assert worker['LinuxParameters']['InitProcessEnabled'] is True
    assert worker['StopTimeout'] == 120
    assert 'HealthCheck' not in worker
    assert 'PortMappings' not in worker
    environment = {e['Name']: e['Value'] for e in worker['Environment']}
    assert environment == {
        'AWS_REGION': REGION,
        'DB_SECRET_NAME': config()['db_secret_arn'],
        'DB_SSLMODE': 'require',
        'CLICKHOUSE_URL': config()['clickhouse_url'],
        'CLICKHOUSE_DATABASE': 'cdp_customer',
        'PYTHONDONTWRITEBYTECODE': '1',
        'PYTHONUNBUFFERED': '1',
        'TMPDIR': '/var/tmp/identity-publication',
    }
    assert {s['Name']: s['ValueFrom'] for s in worker['Secrets']} == {
        'CLICKHOUSE_USER': config()['clickhouse_secret_arn'] + ':username::',
        'CLICKHOUSE_PASSWORD': config()['clickhouse_secret_arn'] + ':password::',
    }
    assert worker['LogConfiguration']['LogDriver'] == 'awslogs'
    log, = [r for r in template['Resources'].values() if r['Type'] == 'AWS::Logs::LogGroup']
    assert log['Properties']['RetentionInDays'] == 30
    assert log['DeletionPolicy'] == log['UpdateReplacePolicy'] == 'Retain'
    task_role = definition['TaskRoleArn']['Fn::GetAtt'][0]
    execution_role = definition['ExecutionRoleArn']['Fn::GetAtt'][0]
    statements = {task_role: [], execution_role: []}
    for policy in resources(template, 'AWS::IAM::Policy'):
        for role in policy['Roles']:
            statements[role['Ref']].extend(policy['PolicyDocument']['Statement'])
    assert statements[task_role] == [{
        'Effect': 'Allow', 'Action': 'secretsmanager:GetSecretValue',
        'Resource': config()['db_secret_arn'],
    }]
    ch_statement, = [s for s in statements[execution_role] if 'secretsmanager:GetSecretValue' in s['Action']]
    assert ch_statement['Resource'] == config()['clickhouse_secret_arn']
    execution = template['Resources'][execution_role]['Properties']
    assert 'service-role/AmazonECSTaskExecutionRolePolicy' in json.dumps(execution['ManagedPolicyArns'])
    text = json.dumps(template)
    assert all(denied not in text for denied in ('s3:', 'ecs:RunTask', 'iam:PassRole', 'FARGATE_SPOT'))
    for role_statements in statements.values():
        assert all(s['Resource'] != '*' for s in role_statements)


def test_activation_requires_explicit_parent_readiness_attestation(tmp_path):
    import pytest
    with pytest.raises(ValueError, match='activation_ready'):
        synth(tmp_path / 'not-ready', {'desired_count': 1})
    template = synth(tmp_path / 'ready', {'desired_count': 1, 'activation_ready': True})
    service, = resources(template, 'AWS::ECS::Service')
    assert service['DesiredCount'] == 1
    assert service['DeploymentConfiguration']['MaximumPercent'] == 100
    assert service['DeploymentConfiguration']['MinimumHealthyPercent'] == 0
    staged = synth(tmp_path / 'still-staged', {'activation_ready': True})
    assert resources(staged, 'AWS::ECS::Service')[0]['DesiredCount'] == 0
    for bad in (-1, 2, 100, True, '1'):
        with pytest.raises(ValueError, match='desired_count'):
            synth(tmp_path / f'bad-{bad}', {'desired_count': bad, 'activation_ready': True})


@pytest.mark.parametrize('changes, field', [
    ({'image_uri': 'repository:latest'}, 'image_uri'),
    ({'image_uri': config()['image_uri'].replace(ACCOUNT, '999999999999')}, 'image_uri'),
    ({'image_uri': config()['image_uri'].replace(REGION, 'us-east-1')}, 'image_uri'),
    ({'db_secret_arn': config()['db_secret_arn'].replace(ACCOUNT, '999999999999')}, 'db_secret_arn'),
    ({'clickhouse_secret_arn': config()['clickhouse_secret_arn'].replace(REGION, 'us-east-1')}, 'clickhouse_secret_arn'),
    ({'db_secret_arn': config()['db_secret_arn'] + ':password::'}, 'db_secret_arn'),
    ({'clickhouse_secret_arn': config()['db_secret_arn']}, 'dedicated'),
    ({'clickhouse_url': 'http://10.0.1.10:8123'}, 'allow_private_http'),
    ({'clickhouse_url': 'https://user:password@clickhouse.internal'}, 'clickhouse_url'),
    ({'clickhouse_url': 'https://clickhouse.internal/?password=x'}, 'clickhouse_url'),
    ({'clickhouse_url': 'https://clickhouse.internal/#fragment'}, 'clickhouse_url'),
    ({'clickhouse_url': 'https://clickhouse.internal/path'}, 'clickhouse_url'),
    ({'clickhouse_url': 'https://'}, 'clickhouse_url'),
    ({'clickhouse_single_server_confirmed': False}, 'single_server'),
    ({'clickhouse_database': 'cdp;DROP'}, 'clickhouse_database'),
    ({'vpc': dict(config()['vpc'], security_group_ids=[])}, 'security_group_ids'),
    ({'vpc': dict(config()['vpc'], private_subnet_ids=[])}, 'private_subnet_ids'),
    ({'vpc': dict(config()['vpc'], availability_zones=['us-east-1a', 'us-east-1b'])}, 'availability_zones'),
])
def test_config_rejects_unsafe_identity_or_transport(tmp_path, changes, field):
    with pytest.raises(ValueError, match=field):
        synth(tmp_path, changes)


def test_approved_private_http_is_explicit_not_inferred(tmp_path):
    url = 'http://10.0.1.10:8123'
    template = synth(tmp_path, {'clickhouse_url': url, 'allow_private_http': True})
    worker = resources(template, 'AWS::ECS::TaskDefinition')[0]['ContainerDefinitions'][0]
    assert {'Name': 'CLICKHOUSE_URL', 'Value': url} in worker['Environment']


def test_resource_and_batch_limits_can_be_sized_explicitly(tmp_path):
    template = synth(tmp_path, {
        'cpu': 2048, 'memory_mib': 4096, 'batch_rows': 5000, 'batch_bytes': 4194304,
        'retry_seconds': 120, 'poll_seconds': 20,
    })
    definition, = resources(template, 'AWS::ECS::TaskDefinition')
    assert (definition['Cpu'], definition['Memory']) == ('2048', '4096')
    assert definition['ContainerDefinitions'][0]['Command'] == [
        '--daemon', '--batch-rows', '5000', '--batch-bytes', '4194304',
        '--retry-seconds', '120', '--poll-seconds', '20',
    ]


@pytest.mark.parametrize('changes', [
    {'cpu': 8192}, {'cpu': True}, {'memory_mib': 32768}, {'cpu': 2048, 'memory_mib': 2048},
    {'batch_rows': 0}, {'batch_rows': 100001}, {'batch_bytes': 0}, {'batch_bytes': 67108865},
    {'retry_seconds': 0}, {'poll_seconds': 0}, {'poll_seconds': '10'},
])
def test_unbounded_or_invalid_resource_limits_rejected(tmp_path, changes):
    with pytest.raises(ValueError):
        synth(tmp_path, changes)


def test_custom_kms_permissions_follow_each_secret_reader(tmp_path):
    db_key = f'arn:aws:kms:{REGION}:{ACCOUNT}:key/11111111-1111-1111-1111-111111111111'
    ch_key = f'arn:aws:kms:{REGION}:{ACCOUNT}:key/22222222-2222-2222-2222-222222222222'
    template = synth(tmp_path, {'db_secret_kms_key_arn': db_key, 'clickhouse_secret_kms_key_arn': ch_key})
    definition, = resources(template, 'AWS::ECS::TaskDefinition')
    expected = {
        definition['TaskRoleArn']['Fn::GetAtt'][0]: (db_key, config()['db_secret_arn']),
        definition['ExecutionRoleArn']['Fn::GetAtt'][0]: (ch_key, config()['clickhouse_secret_arn']),
    }
    seen = set()
    for policy in resources(template, 'AWS::IAM::Policy'):
        for statement in policy['PolicyDocument']['Statement']:
            if statement['Action'] == 'kms:Decrypt':
                role, = policy['Roles']
                key, secret = expected[role['Ref']]
                assert statement['Resource'] == key
                assert statement['Condition']['StringEquals'] == {
                    'kms:ViaService': f'secretsmanager.{REGION}.amazonaws.com',
                    'kms:EncryptionContext:SecretARN': secret,
                }
                seen.add(role['Ref'])
    assert seen == set(expected)


@pytest.mark.parametrize('field', ['db_secret_kms_key_arn', 'clickhouse_secret_kms_key_arn'])
def test_kms_keys_must_be_explicit_same_region_account_arns(tmp_path, field):
    with pytest.raises(ValueError, match=field):
        synth(tmp_path, {field: '*'})


def test_standalone_synth_never_loads_the_shared_app(tmp_path):
    import os
    import subprocess

    script = ROOT / 'scripts/synth_customer_identity_publication.py'
    assert script.exists(), 'standalone synth entrypoint missing'
    cfg = tmp_path / 'publication.json'
    cfg.write_text(json.dumps({
        'aws_account_id': ACCOUNT, 'aws_region': REGION,
        'stack_name': 'CustomerIdentityPublication', 'project_config': config(),
    }))
    outdir = tmp_path / 'assembly'
    env = dict(os.environ, AWS_EC2_METADATA_DISABLED='true',
               AWS_CONFIG_FILE=str(tmp_path / 'no-aws-config'),
               AWS_SHARED_CREDENTIALS_FILE=str(tmp_path / 'no-credentials'),
               CONFIG_FILE=str(tmp_path / 'missing-shared-app-config'))
    result = subprocess.run(
        [sys.executable, str(script), '--config', str(cfg), '--outdir', str(outdir)],
        cwd=tmp_path, env=env, text=True, capture_output=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    manifest = json.loads((outdir / 'manifest.json').read_text())
    stacks = [a for a in manifest['artifacts'].values() if a['type'] == 'aws:cloudformation:stack']
    assert len(stacks) == 1
    assert not manifest.get('missing')
    assert list(outdir.glob('*.template.json')) == [outdir / 'CustomerIdentityPublication.template.json']
    template = json.loads((outdir / 'CustomerIdentityPublication.template.json').read_text())
    assert resources(template, 'AWS::ECS::Service')[0]['DesiredCount'] == 0
    assert set(template['Outputs']) == {
        'TaskDefinitionOutput', 'ClusterOutput', 'ServiceOutput', 'WorkerRoleOutput',
        'ExecutionRoleOutput', 'LogGroupOutput',
    }
    assert resources(template, 'AWS::CloudFormation::CustomResource') == []
    assert resources(template, 'AWS::SecretsManager::Secret') == []
