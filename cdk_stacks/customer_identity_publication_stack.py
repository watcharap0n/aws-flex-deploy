"""Separate publication runtime; imports network/cluster without AWS lookups."""
from __future__ import annotations

import re
from urllib.parse import urlsplit

from aws_cdk import CfnOutput, Duration, RemovalPolicy, Stack, Token
from aws_cdk import aws_ec2 as ec2, aws_ecs as ecs
from aws_cdk import aws_iam as iam, aws_logs as logs, aws_secretsmanager as secrets
from constructs import Construct


class CustomerIdentityPublicationStack(Stack):
    """Stage at zero; activation_ready is operator attestation, not a DB probe.

    Parent must verify migrations, least-privilege grants, CH DDL/cutover,
    query-log fencing and direct single-server routing before activation.
    DB secret JSON: hostname/port/dbname/username/password/sslmode (require).
    CH secret JSON: username/password. Never put secret values in this config.
    allow_private_http=true attests an approved private route/encrypted tunnel;
    importing subnet IDs does not prove routing, security groups or transport.
    """

    def __init__(self, scope: Construct, construct_id: str, *, project_config: dict, **kwargs):
        super().__init__(scope, construct_id, **kwargs)
        c = project_config
        desired = c.get('desired_count', 0)
        if type(desired) is not int or desired not in (0, 1):
            raise ValueError('desired_count must be integer 0 or 1')
        if desired and c.get('activation_ready') is not True:
            raise ValueError('activation_ready=true requires parent verification of DB/ClickHouse readiness')
        if Token.is_unresolved(self.account) or Token.is_unresolved(self.region):
            raise ValueError('explicit stack account and region required')
        account, region = re.escape(self.account), re.escape(self.region)
        if not re.fullmatch(
                rf'{account}\.dkr\.ecr\.{region}\.amazonaws\.com/[a-z0-9][a-z0-9._/-]*@sha256:[0-9a-f]{{64}}',
                c.get('image_uri', '')):
            raise ValueError('image_uri must pin a sha256 digest in this account/region ECR')
        for field in ('db_secret_arn', 'clickhouse_secret_arn'):
            if not re.fullmatch(
                    rf'arn:aws:secretsmanager:{region}:{account}:secret:[A-Za-z0-9/_+=.@-]+-[A-Za-z0-9]{{6}}',
                    c.get(field, '')):
                raise ValueError(f'{field} must be a complete secret ARN in this account/region')
        if c['db_secret_arn'] == c['clickhouse_secret_arn']:
            raise ValueError('use separate dedicated DB and ClickHouse secrets')
        try:
            url = c['clickhouse_url']
            parsed = urlsplit(url)
            valid = (parsed.scheme in ('https', 'http') and parsed.hostname
                     and not parsed.username and not parsed.password
                     and not parsed.query and not parsed.fragment and parsed.path in ('', '/')
                     and not any(ch.isspace() for ch in url) and (parsed.port is None or parsed.port > 0))
        except (KeyError, TypeError, ValueError):
            raise ValueError('clickhouse_url must be a valid server origin') from None
        if not valid:
            raise ValueError('clickhouse_url must be one server origin without credentials/query/path/fragment')
        if parsed.scheme == 'http' and c.get('allow_private_http') is not True:
            raise ValueError('HTTP requires allow_private_http=true for an explicitly approved private route/tunnel')
        if c.get('clickhouse_single_server_confirmed') is not True:
            raise ValueError('clickhouse_single_server_confirmed=true required; no multi-server load balancer')
        if not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', c.get('clickhouse_database', '')):
            raise ValueError('clickhouse_database must be an explicit SQL identifier')
        cpu, memory = c.get('cpu', 1024), c.get('memory_mib', 2048)
        sizes = {256: (512, 1024, 2048), 512: range(1024, 4097, 1024),
                 1024: range(2048, 8193, 1024), 2048: range(4096, 16385, 1024),
                 4096: range(8192, 16385, 1024)}
        if type(cpu) is not int or type(memory) is not int or cpu not in sizes or memory not in sizes[cpu]:
            raise ValueError('cpu/memory_mib must be a supported Fargate pair capped at 4 vCPU/16 GiB')
        command = ['--daemon']
        for field, default, maximum in (
                ('batch_rows', 10000, 100000), ('batch_bytes', 8388608, 67108864),
                ('retry_seconds', 60, 86400), ('poll_seconds', 10, 3600)):
            value = c.get(field, default)
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError(f'{field} must be an integer between 1 and {maximum}')
            command.extend(['--' + field.replace('_', '-'), str(value)])
        v = c['vpc']
        for field in ('private_subnet_ids', 'security_group_ids', 'availability_zones'):
            if not isinstance(v.get(field), list) or not v[field] or any(not x for x in v[field]):
                raise ValueError(f'vpc.{field} must be an explicit nonempty list')
        if (len(v['availability_zones']) != len(v['private_subnet_ids'])
                or any(not re.fullmatch(region + r'[a-z]', zone) for zone in v['availability_zones'])):
            raise ValueError('vpc.availability_zones must match region and private_subnet_ids one-to-one')
        vpc = ec2.Vpc.from_vpc_attributes(
            self, 'Vpc', vpc_id=v['vpc_id'], availability_zones=v['availability_zones'],
            private_subnet_ids=v['private_subnet_ids'],
        )
        groups = [ec2.SecurityGroup.from_security_group_id(self, f'Sg{i}', group, mutable=False)
                  for i, group in enumerate(v['security_group_ids'])]
        cluster = ecs.Cluster.from_cluster_attributes(
            self, 'Cluster', cluster_name=c['cluster_name'], vpc=vpc, security_groups=groups,
        )
        role = iam.Role(self, 'WorkerRole', assumed_by=iam.ServicePrincipal('ecs-tasks.amazonaws.com'))
        role.add_to_policy(iam.PolicyStatement(
            actions=['secretsmanager:GetSecretValue'], resources=[c['db_secret_arn']],
        ))
        execution = iam.Role(
            self, 'ExecutionRole', assumed_by=iam.ServicePrincipal('ecs-tasks.amazonaws.com'),
            managed_policies=[iam.ManagedPolicy.from_aws_managed_policy_name(
                'service-role/AmazonECSTaskExecutionRolePolicy')],
        )
        for prefix, reader in (('db', role), ('clickhouse', execution)):
            field = prefix + '_secret_kms_key_arn'
            if field in c:
                if not re.fullmatch(rf'arn:aws:kms:{region}:{account}:key/[0-9a-f-]{{36}}', c[field]):
                    raise ValueError(f'{field} must be an explicit key ARN in this account/region')
                reader.add_to_policy(iam.PolicyStatement(
                    actions=['kms:Decrypt'], resources=[c[field]],
                    conditions={'StringEquals': {
                        'kms:ViaService': f'secretsmanager.{self.region}.amazonaws.com',
                        'kms:EncryptionContext:SecretARN': c[prefix + '_secret_arn'],
                    }},
                ))
        ch_secret = secrets.Secret.from_secret_complete_arn(self, 'ClickHouseSecret', c['clickhouse_secret_arn'])
        task = ecs.FargateTaskDefinition(
            self, 'Task', cpu=cpu, memory_limit_mib=memory, task_role=role, execution_role=execution,
            runtime_platform=ecs.RuntimePlatform(
                cpu_architecture=ecs.CpuArchitecture.X86_64, operating_system_family=ecs.OperatingSystemFamily.LINUX),
        )
        log = logs.LogGroup(self, 'Logs', retention=logs.RetentionDays.ONE_MONTH, removal_policy=RemovalPolicy.RETAIN)
        task.add_volume(name='publication-scratch')
        worker = task.add_container(
            'Worker', container_name='customer-identity-publication',
            image=ecs.ContainerImage.from_registry(c['image_uri']),
            entry_point=['python', '-m', 'identity_stitcher.publication'],
            command=command,
            readonly_root_filesystem=True,
            linux_parameters=ecs.LinuxParameters(self, 'Linux', init_process_enabled=True),
            stop_timeout=Duration.seconds(120),
            logging=ecs.LogDrivers.aws_logs(stream_prefix='customer-identity-publication', log_group=log),
            environment={
                'AWS_REGION': self.region, 'DB_SECRET_NAME': c['db_secret_arn'], 'DB_SSLMODE': 'require',
                'CLICKHOUSE_URL': c['clickhouse_url'], 'CLICKHOUSE_DATABASE': c['clickhouse_database'],
                'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONUNBUFFERED': '1',
                'TMPDIR': '/var/tmp/identity-publication',
            },
            secrets={
                'CLICKHOUSE_USER': ecs.Secret.from_secrets_manager(ch_secret, 'username'),
                'CLICKHOUSE_PASSWORD': ecs.Secret.from_secrets_manager(ch_secret, 'password'),
            },
        )
        worker.add_mount_points(ecs.MountPoint(
            source_volume='publication-scratch', container_path='/var/tmp/identity-publication', read_only=False,
        ))
        service = ecs.FargateService(
            self, 'Service', cluster=cluster, task_definition=task, desired_count=desired,
            capacity_provider_strategies=[ecs.CapacityProviderStrategy(capacity_provider='FARGATE', weight=1)],
            security_groups=groups, vpc_subnets=ec2.SubnetSelection(subnets=vpc.private_subnets),
            assign_public_ip=False, min_healthy_percent=0, max_healthy_percent=100,
            enable_execute_command=False,
        )
        for name, value in {
            'TaskDefinition': task.task_definition_arn, 'Cluster': cluster.cluster_arn,
            'Service': service.service_arn, 'WorkerRole': role.role_arn,
            'ExecutionRole': execution.role_arn, 'LogGroup': log.log_group_name,
        }.items():
            CfnOutput(self, name + 'Output', value=value)
