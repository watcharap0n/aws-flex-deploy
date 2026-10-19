"""Opt-in customer ingestion runtime; preserves existing Lambda/stitching stack."""
from __future__ import annotations

import re
from aws_cdk import CfnOutput, Duration, RemovalPolicy, Stack
from aws_cdk import aws_ec2 as ec2, aws_ecs as ecs, aws_events as events
from aws_cdk import aws_events_targets as targets, aws_iam as iam
from aws_cdk import aws_lambda as lambda_, aws_logs as logs, aws_secretsmanager as secrets
from constructs import Construct


class CustomerIngestionRuntimeStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, *, project_config: dict, **kwargs):
        super().__init__(scope,construct_id,**kwargs)
        c=project_config
        if not re.search(r'@sha256:[0-9a-f]{64}$',c['image_uri']):
            raise ValueError('Customer ingestion image must pin sha256 digest')
        warm=int(c.get('warm_count',0))
        if not 0<=warm<=8:
            raise ValueError('warm_count must be between 0 and 8; DB max_active is authoritative')
        if c.get('landing_enabled', False):
            for name in ('landing_max_parse_bytes', 'landing_max_xlsx_expanded_bytes'):
                if name not in c or int(c[name]) <= 0:
                    raise ValueError(name + ' must be explicitly sized for the dedicated worker')
            if 'landing_max_csv_bytes' not in c or not 0 < int(c['landing_max_csv_bytes']) <= 1024**3:
                raise ValueError('landing_max_csv_bytes must be explicit and at most 1 GiB for sized worker')
            if not c.get('landing_role_arn'):
                raise ValueError('landing_role_arn required for synchronous durable enqueue')
        v=c['vpc']
        vpc=ec2.Vpc.from_vpc_attributes(self,'Vpc',vpc_id=v['vpc_id'],
            availability_zones=v['availability_zones'],private_subnet_ids=v['private_subnet_ids'])
        groups=[ec2.SecurityGroup.from_security_group_id(self,f'Sg{i}',g) for i,g in enumerate(v['security_group_ids'])]
        cluster=ecs.Cluster.from_cluster_attributes(self,'Cluster',cluster_name=c['cluster_name'],vpc=vpc,security_groups=groups)
        role=iam.Role(self,'WorkerRole',assumed_by=iam.ServicePrincipal('ecs-tasks.amazonaws.com'))
        execution=iam.Role(self,'ExecutionRole',assumed_by=iam.ServicePrincipal('ecs-tasks.amazonaws.com'),
            managed_policies=[iam.ManagedPolicy.from_aws_managed_policy_name('service-role/AmazonECSTaskExecutionRolePolicy')])
        secret=secrets.Secret.from_secret_complete_arn(self,'Database',c['db_secret_arn'])
        secret.grant_read(role)
        for bucket in c['bucket_arns']:
            role.add_to_policy(iam.PolicyStatement(actions=['s3:GetObject','s3:GetObjectVersion'],resources=[bucket+'/raw/*']))
            if c.get('landing_enabled',False):
                role.add_to_policy(iam.PolicyStatement(actions=['s3:GetObject','s3:GetObjectVersion'],resources=[bucket+'/data_sources/*']))
                role.add_to_policy(iam.PolicyStatement(actions=['s3:PutObject'],resources=[bucket+'/raw/*']))
        if c.get('kms_key_arns'):
            role.add_to_policy(iam.PolicyStatement(actions=['kms:Decrypt'] + (['kms:GenerateDataKey'] if c.get('landing_enabled',False) else []),resources=c['kms_key_arns']))
        task=ecs.FargateTaskDefinition(self,'Task',cpu=int(c.get('cpu',4096)),memory_limit_mib=int(c.get('memory_mib',16384)),
            ephemeral_storage_gib=int(c.get('scratch_gib',40)),task_role=role,execution_role=execution,
            runtime_platform=ecs.RuntimePlatform(cpu_architecture=ecs.CpuArchitecture.X86_64,operating_system_family=ecs.OperatingSystemFamily.LINUX))
        log=logs.LogGroup(self,'Logs',retention=logs.RetentionDays.ONE_MONTH,removal_policy=RemovalPolicy.RETAIN)
        container=c.get('container_name','customer-ingestion')
        task.add_container('Worker',container_name=container,image=ecs.ContainerImage.from_registry(c['image_uri']),
            command=['--warm'],stop_timeout=Duration.seconds(120),logging=ecs.LogDrivers.aws_logs(stream_prefix='customer-ingestion',log_group=log),
            environment={'DB_SECRET_ID':c['db_secret_arn'],'AWS_REGION':self.region,
                'DB_SSLMODE':str(c.get('db_sslmode','require')),
                'CUSTOMER_BULK_ENABLED':str(c.get('bulk_enabled',False)).lower(),
                'CUSTOMER_FORWARD_SOURCE_NAMESPACE_ENABLED':str(c.get('forward_source_namespace_enabled',False)).lower(),
                'CUSTOMER_LANDING_ENABLED':str(c.get('landing_enabled',False)).lower(),
                'LANDING_MAX_PARSE_BYTES':str(c.get('landing_max_parse_bytes',64*1024**2)),
                'LANDING_MAX_CSV_BYTES':str(c.get('landing_max_csv_bytes',1024**3)),
                'LANDING_MAX_XLSX_EXPANDED_BYTES':str(c.get('landing_max_xlsx_expanded_bytes',256*1024**2)),
                'INGESTION_STATEMENT_TIMEOUT':str(c.get('statement_timeout','25min')),
                'INGESTION_WORK_MEM':str(c.get('work_mem','32MB')),
                'CUSTOMER_MAX_OBJECT_BYTES':str(c.get('max_object_bytes',10*1024**3)),
                'CUSTOMER_WARM_POLL_SECONDS':str(c.get('warm_poll_seconds',2))})
        function=lambda_.Function.from_function_arn(self,'Ingress',c['ingestion_function_arn'])
        if c.get('landing_enabled',False):
            landing_role=iam.Role.from_role_arn(self,'LandingRole',c['landing_role_arn'],mutable=True)
            landing_role.add_to_principal_policy(iam.PolicyStatement(actions=['lambda:InvokeFunction'],resources=[c['ingestion_function_arn']]))
        ingress_role=iam.Role.from_role_arn(self,'IngressRole',c['ingestion_role_arn'],mutable=True)
        # An ECS task-definition replacement increments its revision. Grant only this
        # stack's generated family across revisions, still scoped to the cluster.
        task_family_arn = self.format_arn(
            service='ecs', resource='task-definition', resource_name=f'{task.family}:*')
        ingress_role.add_to_principal_policy(iam.PolicyStatement(actions=['ecs:RunTask'],resources=[task_family_arn],conditions={'ArnEquals':{'ecs:cluster':cluster.cluster_arn}}))
        ingress_role.add_to_principal_policy(iam.PolicyStatement(actions=['ecs:DescribeTasks','ecs:StopTask'],resources=[self.format_arn(service='ecs',resource='task',resource_name=c['cluster_name']+'/*')]))
        ingress_role.add_to_principal_policy(iam.PolicyStatement(actions=['iam:PassRole'],resources=[role.role_arn,execution.role_arn],conditions={'StringEquals':{'iam:PassedToService':'ecs-tasks.amazonaws.com'}}))
        events.Rule(self,'Reconcile',schedule=events.Schedule.rate(Duration.minutes(1)),
            targets=[targets.LambdaFunction(function,event=events.RuleTargetInput.from_object({'action':'customer_ingestion_tick'}))])
        events.Rule(self,'Stopped',event_pattern=events.EventPattern(source=['aws.ecs'],detail_type=['ECS Task State Change'],
            detail={'lastStatus':['STOPPED']}),targets=[targets.LambdaFunction(function)])
        if warm:
            ecs.FargateService(self,'WarmService',cluster=cluster,task_definition=task,desired_count=warm,
                security_groups=groups,vpc_subnets=ec2.SubnetSelection(subnets=vpc.private_subnets),assign_public_ip=False,
                min_healthy_percent=0,max_healthy_percent=100,circuit_breaker=ecs.DeploymentCircuitBreaker(rollback=True))
        for name,value in {'TaskDefinition':task.task_definition_arn,'Cluster':cluster.cluster_arn,'Container':container,
                           'WorkerRole':role.role_arn,'ExecutionRole':execution.role_arn}.items():
            CfnOutput(self,name+'Output',value=value)
