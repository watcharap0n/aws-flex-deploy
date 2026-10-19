"""Production worker images with isolated staging databases and modest sizing."""
import re
from aws_cdk import CfnOutput, Duration, RemovalPolicy, Stack
from aws_cdk import aws_ec2 as ec2, aws_ecs as ecs, aws_iam as iam
from aws_cdk import aws_logs as logs, aws_secretsmanager as secrets
from constructs import Construct


class StagingCustomerRuntimeStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, *, project_config: dict, **kwargs):
        super().__init__(scope, construct_id, **kwargs)
        if not construct_id.startswith('CdpStg'):
            raise ValueError('This stack is staging-only')
        c = project_config
        if c['cluster_name'] != 'cdp-stg-stitcher-cluster':
            raise ValueError('Staging cluster required')
        for field, name in (('db_secret_arn', 'postgres-marketing-automation-dev'),
                            ('clickhouse_secret_arn', 'clickhouse-marketing-automation-dev')):
            if not re.fullmatch(rf'arn:aws:secretsmanager:{self.region}:{self.account}:secret:{name}-[A-Za-z0-9]{{6}}', c[field]):
                raise ValueError('Staging database secret required')
        worker_secrets = {
            'publication_db_secret_arn': 'cdp/staging/identity-publication/postgres',
            'upload_resolution_db_secret_arn': 'cdp/staging/customer-app-history-backfill/postgres',
            'publication_clickhouse_secret_arn': 'cdp/staging/identity-publication/clickhouse',
        }
        for field, name in worker_secrets.items():
            if field in c and not re.fullmatch(
                    rf'arn:aws:secretsmanager:{self.region}:{self.account}:secret:{name}-[A-Za-z0-9]{{6}}', c[field]):
                raise ValueError('Staging worker database secret required')
        if c['clickhouse_url'] != 'http://172.31.38.158:8123':
            raise ValueError('Staging ClickHouse endpoint required')
        if c.get('desired_count', 0) not in (0, 1):
            raise ValueError('Staging daemon desired_count must be zero or one')
        v = c['vpc']
        vpc = ec2.Vpc.from_vpc_attributes(self, 'Vpc', vpc_id=v['vpc_id'],
            availability_zones=v['availability_zones'], private_subnet_ids=v['private_subnet_ids'])
        groups = [ec2.SecurityGroup.from_security_group_id(self, f'Sg{i}', group, mutable=False)
                  for i, group in enumerate(v['security_group_ids'])]
        cluster = ecs.Cluster.from_cluster_attributes(self, 'Cluster',
            cluster_name=c['cluster_name'], vpc=vpc, security_groups=groups)
        role = iam.Role(self, 'Role', assumed_by=iam.ServicePrincipal('ecs-tasks.amazonaws.com'))
        role.add_to_policy(iam.PolicyStatement(actions=['secretsmanager:GetSecretValue'],
            resources=list(dict.fromkeys(c.get(k, c['db_secret_arn']) for k in (
                'publication_db_secret_arn', 'upload_resolution_db_secret_arn')))))
        execution = iam.Role(self, 'ExecutionRole', assumed_by=iam.ServicePrincipal('ecs-tasks.amazonaws.com'),
            managed_policies=[iam.ManagedPolicy.from_aws_managed_policy_name(
                'service-role/AmazonECSTaskExecutionRolePolicy')])
        ch = secrets.Secret.from_secret_complete_arn(self, 'ClickHouseSecret',
            c.get('publication_clickhouse_secret_arn', c['clickhouse_secret_arn']))
        common = {'AWS_REGION': self.region, 'DB_SECRET_NAME': c['db_secret_arn'],
                  'DB_SSLMODE': 'disable', 'PYTHONDONTWRITEBYTECODE': '1', 'PYTHONUNBUFFERED': '1'}
        platform = ecs.RuntimePlatform(cpu_architecture=ecs.CpuArchitecture.X86_64,
                                       operating_system_family=ecs.OperatingSystemFamily.LINUX)
        for label, cfg in (('Publication', c['publication']), ('UploadResolution', c['upload_resolution'])):
            if not re.fullmatch(rf'{self.account}\.dkr\.ecr\.{self.region}\.amazonaws\.com/[a-z0-9._/-]+@sha256:[0-9a-f]{{64}}', cfg['image_uri']):
                raise ValueError('Pin the production image digest')
            task = ecs.FargateTaskDefinition(self, label + 'Task', family=cfg['family'],
                cpu=cfg['cpu'], memory_limit_mib=cfg['memory_mib'], runtime_platform=platform,
                task_role=role, execution_role=execution)
            log = logs.LogGroup(self, label + 'Logs', log_group_name=cfg['log_group'],
                retention=logs.RetentionDays.ONE_WEEK, removal_policy=RemovalPolicy.RETAIN)
            env = {**common, **cfg.get('environment', {})}
            db_field = 'publication_db_secret_arn' if label == 'Publication' else 'upload_resolution_db_secret_arn'
            env['DB_SECRET_NAME'] = c.get(db_field, c['db_secret_arn'])
            secret_values = {}
            if label == 'Publication':
                env.update(CLICKHOUSE_URL=c['clickhouse_url'], CLICKHOUSE_DATABASE='cdp_customer',
                           IDENTITY_SERVING_ENABLED='1')
                secret_values = {'CLICKHOUSE_USER': ecs.Secret.from_secrets_manager(ch, 'username'),
                                 'CLICKHOUSE_PASSWORD': ecs.Secret.from_secrets_manager(ch, 'password')}
            task.add_container(label, container_name=cfg['container_name'],
                image=ecs.ContainerImage.from_registry(cfg['image_uri']),
                entry_point=cfg['entry_point'], command=cfg['command'], environment=env,
                secrets=secret_values, stop_timeout=Duration.seconds(120),
                logging=ecs.LogDrivers.aws_logs(stream_prefix=label.lower(), log_group=log))
            service = ecs.FargateService(self, label + 'Service', service_name=cfg['service_name'],
                cluster=cluster, task_definition=task, desired_count=c.get('desired_count', 0),
                security_groups=groups, vpc_subnets=ec2.SubnetSelection(subnets=vpc.private_subnets),
                assign_public_ip=False, min_healthy_percent=0, max_healthy_percent=100,
                capacity_provider_strategies=[ecs.CapacityProviderStrategy(capacity_provider='FARGATE', weight=1)],
                circuit_breaker=ecs.DeploymentCircuitBreaker(rollback=True))
            CfnOutput(self, label + 'TaskDefinition', value=task.task_definition_arn)
            CfnOutput(self, label + 'ServiceName', value=service.service_name)
        stitcher = c['stitcher']
        task = ecs.FargateTaskDefinition(self, 'StitcherTask', family='cdp-stg-stitcher-task',
            cpu=stitcher['cpu'], memory_limit_mib=stitcher['memory_mib'], runtime_platform=platform,
            task_role=iam.Role.from_role_arn(self, 'StitcherRole', c['stitcher_task_role_arn']),
            execution_role=iam.Role.from_role_arn(self, 'StitcherExecutionRole', c['stitcher_execution_role_arn']))
        log = logs.LogGroup.from_log_group_name(self, 'StitcherLogs', '/ecs/cdp-stg-stitcher')
        task.add_container('Stitcher', container_name='cdp-stitcher-container',
            image=ecs.ContainerImage.from_registry(stitcher['image_uri']),
            environment={**common, **stitcher['environment'], 'RUN_MODE': 'once'},
            logging=ecs.LogDrivers.aws_logs(stream_prefix='ecs', log_group=log))
        CfnOutput(self, 'StitcherTaskDefinition', value=task.task_definition_arn)
