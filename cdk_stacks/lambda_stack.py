from aws_cdk import (
    Stack,
    aws_lambda as _lambda,
    aws_iam as iam,
    aws_ec2 as ec2,
    aws_s3 as s3,
    aws_events as events,
    aws_events_targets as event_targets,
    aws_cloudwatch as cloudwatch,
    Duration,
    Size,
)
import re
from pathlib import Path
from constructs import Construct

class LambdaStack(Stack):
    def __init__(self, scope: Construct, construct_id: str, project_config: dict, **kwargs) -> None:
        super().__init__(scope, construct_id, **kwargs)

        # Load configuration from the provided dictionary
        self.vpc_id                 = project_config.get('vpc_id')
        self.bucket_layer_name      = project_config.get('bucket_layer_name')
        self.lambda_function_name   = project_config.get('lambda_function_name', 'DefaultLambdaFunction')
        self.code_from_asset        = project_config.get('code_from_asset')
        self.function_handler       = project_config.get('function_handler')
        self.memory_size            = int(project_config.get('memory_size', 128))
        self.timeout_seconds        = int(project_config.get('timeout', 30))
        reserved_concurrency        = project_config.get('reserved_concurrent_executions')
        self.reserved_concurrency   = (
            int(reserved_concurrency)
            if reserved_concurrency is not None
            else None
        )
        ephemeral_storage_size_mb   = int(project_config.get('ephemeral_storage_size', 1024))
        self.ephemeral_storage_size = Size.mebibytes(ephemeral_storage_size_mb)
        self.environments           = project_config.get('environments', {})
        self.lambda_runtime         = self.__resolve_python_runtime(project_config.get('python_version', '3.9'))

        # =================================================================================================================
        # Dynamically configure Lambda layers
        # =================================================================================================================
        layers = []
        layers_config = project_config.get('layers', [])
        if self.bucket_layer_name and layers_config:
            for index, layer_conf in enumerate(layers_config, start=1):
                if isinstance(layer_conf, str):
                    key = layer_conf.strip()
                    if not key:
                        raise ValueError('Each Lambda layer entry must include a non-empty "key" value')
                    layer_id = self.__generate_layer_id(key=key, index=index)
                elif isinstance(layer_conf, dict):
                    key_val = layer_conf.get('key')
                    key = key_val.strip() if isinstance(key_val, str) else None
                    if not key:
                        raise ValueError('Each Lambda layer entry must include a non-empty "key" value')
                    layer_id_val = layer_conf.get('id')
                    layer_id = (
                        layer_id_val.strip()
                        if isinstance(layer_id_val, str) and layer_id_val.strip()
                        else self.__generate_layer_id(key=key, index=index)
                    )
                else:
                    raise ValueError('Each Lambda layer entry must be a string or a mapping containing "key"')

                layers.append(self.__get_layer_from_bucket(layer_id, self.bucket_layer_name, key))

        # =================================================================================================================
        # Create IAM role with policies dynamically.
        # =================================================================================================================
        role_config = project_config.get('role', {})
        assumed_by_value = role_config.get('assumed_by', "lambda.amazonaws.com")
        assumed_by = iam.ServicePrincipal(assumed_by_value)
        role_name = role_config.get('role_name', f'IamRole{self.lambda_function_name}')
        ids = role_config.get('ids')
        managed_policy_arns = role_config.get('managed_policy_arns', [
            'arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole'
        ])
        role = self.__create_role_attach_policy(
            assumed_by=assumed_by,
            ids=ids,
            managed_policy_arns=managed_policy_arns,
            role_name=role_name
        )

        # Optional inline policies configuration
        inline_policies = role_config.get('inline_policies')
        if inline_policies:
            for policy_name, policy_statements in inline_policies.items():
                policy = iam.Policy(self, policy_name)
                for statement in policy_statements:
                    actions = statement.get('actions', [])
                    resources = statement.get('resources', ['*'])
                    effect = statement.get('effect', iam.Effect.ALLOW)
                    policy.add_statements(iam.PolicyStatement(
                        actions=actions,
                        resources=resources,
                        effect=effect
                    ))
                role.attach_inline_policy(policy)

        # =================================================================================================================
        # Network configuration: if a VPC is specified, set up security groups and subnet selection.
        # =================================================================================================================
        vpc = None
        security_groups = []
        vpc_subnets = None
        if self.vpc_id:
            vpc = ec2.Vpc.from_lookup(self, "VPC", vpc_id=self.vpc_id)

            # Dynamic Security Group configuration
            sg_config = project_config.get('security_group', {})
            existing_security_group_ids = self.__get_existing_security_group_ids(sg_config)
            if existing_security_group_ids:
                self.__validate_existing_security_group_config(sg_config)
                security_groups = [
                    ec2.SecurityGroup.from_security_group_id(
                        self,
                        self.__generate_security_group_import_id(index=index),
                        security_group_id=security_group_id,
                    )
                    for index, security_group_id in enumerate(existing_security_group_ids, start=1)
                ]
            else:
                sg_name = sg_config.get('name', f'{self.lambda_function_name}SecurityGroup')
                sg_description = sg_config.get('description', 'Default security group for Lambda')
                allow_all_outbound = sg_config.get('allow_all_outbound', True)
                security_group = ec2.SecurityGroup(
                    self,
                    'LambdaDynamicSecurityGroup',
                    vpc=vpc,
                    allow_all_outbound=allow_all_outbound,
                    security_group_name=sg_name,
                    description=sg_description,
                )
                # Add ingress rules dynamically if provided in the configuration
                ingress_rules = sg_config.get('ingress_rules', [])
                for rule in ingress_rules:
                    peer_val = rule.get('peer', 'any_ipv4')
                    port_val = rule.get('port', 5432)
                    description_val = rule.get('description', '')
                    if isinstance(peer_val, str) and peer_val.lower() == 'any_ipv4':
                        peer = ec2.Peer.any_ipv4()
                    else:
                        peer = ec2.Peer.ipv4(peer_val)
                    security_group.add_ingress_rule(
                        peer=peer,
                        connection=ec2.Port.tcp(port_val),
                        description=description_val
                    )
                security_groups = [security_group]
            # Subnet selection: use specific subnet IDs if provided, otherwise default to PRIVATE_WITH_EGRESS
            subnet_ids = project_config.get('subnet_ids', [])
            if isinstance(subnet_ids, str):
                subnet_ids = [subnet_ids]
            subnet_ids = [s.strip() for s in subnet_ids if isinstance(s, str) and s.strip()]

            if subnet_ids:
                vpc_subnets = ec2.SubnetSelection(
                    subnets=[
                        ec2.Subnet.from_subnet_id(self, f'Subnet{index}', subnet_id)
                        for index, subnet_id in enumerate(subnet_ids, start=1)
                    ]
                )
                print(f"Chosen VPC: {self.vpc_id}, Specific Subnets: {subnet_ids}")
            else:
                vpc_subnets = ec2.SubnetSelection(subnet_type=ec2.SubnetType.PRIVATE_WITH_EGRESS)
                print(f"Chosen VPC: {self.vpc_id}, Subnet Type: PRIVATE_WITH_EGRESS")

        # =================================================================================================================
        # Assemble Lambda function properties using the dynamic configuration
        # =================================================================================================================
        lambda_kwargs = {
            "function_name": self.lambda_function_name,
            "code": _lambda.Code.from_asset(self.code_from_asset, exclude=['**/__pycache__/**', '**/*.pyc', '**/.DS_Store']),
            "handler": self.function_handler,
            "runtime": self.lambda_runtime,
            "timeout": Duration.seconds(self.timeout_seconds),
            "environment": self.environments,
            "memory_size": self.memory_size,
            "ephemeral_storage_size": self.ephemeral_storage_size,
            "role": role,
            "layers": layers if layers else None
        }
        if vpc:
            lambda_kwargs["security_groups"] = security_groups
            lambda_kwargs["vpc"] = vpc
            lambda_kwargs["allow_public_subnet"] = project_config.get('allow_public_subnet', False)
            if vpc_subnets is not None:
                lambda_kwargs["vpc_subnets"] = vpc_subnets
        if self.reserved_concurrency is not None:
            lambda_kwargs["reserved_concurrent_executions"] = (
                self.reserved_concurrency
            )

        lambda_function = _lambda.Function(
            self,
            id='LambdaFunction',
            **lambda_kwargs
        )

        # =================================================================================================================
        # Optional: Configure S3 trigger if provided in the configuration
        # =================================================================================================================
        s3_trigger_config = project_config.get('s3_trigger')
        if s3_trigger_config:
            trigger_bucket_name = s3_trigger_config.get("bucket_name")
            trigger_prefix = s3_trigger_config.get("prefix", "")
            trigger_suffix = s3_trigger_config.get("suffix", "")
            from aws_cdk import aws_s3_notifications as s3n
            trigger_bucket = s3.Bucket.from_bucket_name(self, "S3TriggerBucket", trigger_bucket_name)
            notification = s3n.LambdaDestination(lambda_function)
            trigger_bucket.add_event_notification(
                s3.EventType.OBJECT_CREATED,
                notification,
                s3.NotificationKeyFilter(prefix=trigger_prefix, suffix=trigger_suffix)
            )

        if Path(self.code_from_asset or '').resolve() == (Path(__file__).resolve().parents[1] / '_lambda/ingestion_data'):
            # This asset uses a durable DB outbox; deploying it without its
            # reconciler would strand all new uploads. Keep this invariant in IaC.
            if not self.environments.get('STITCHING_ADMISSION_NOT_BEFORE'):
                raise ValueError('Stitching admission requires explicit STITCHING_ADMISSION_NOT_BEFORE')
            if self.environments.get('STITCHING_ADMISSION_ENABLED','false')=='true':
                import os
                # Activation must use the checked executor, not ordinary cdk deploy.
                if os.environ.get('STITCHING_CHECKED_ACTIVATION') != '1':
                    raise ValueError('Enabled admission requires the checked activation release executor')
            project_config = {**project_config, 'stitching_admission_enabled': True}

        if project_config.get('stitching_admission_enabled', False):
            cluster_name = self.environments.get('ECS_CLUSTER')
            if not cluster_name:
                raise ValueError('Stitching admission requires ECS_CLUSTER')
            cluster_arn = (cluster_name if cluster_name.startswith('arn:') else
                           self.format_arn(service='ecs', resource='cluster', resource_name=cluster_name))
            role.add_to_policy(iam.PolicyStatement(
                actions=['ecs:ListTasks', 'ecs:DescribeTasks'], resources=['*'],
                conditions={'ArnEquals': {'ecs:cluster': cluster_arn}},
            ))
            admission_rule = events.Rule(
                self, 'StitchingAdmissionSchedule',
                schedule=events.Schedule.rate(Duration.minutes(1)),
            )
            admission_rule.add_target(event_targets.LambdaFunction(
                lambda_function,
                event=events.RuleTargetInput.from_object({'action': 'stitching_admission_tick'}),
                retry_attempts=2,
            ))
            cloudwatch.Alarm(
                self, 'StitchingAdmissionErrors',
                metric=lambda_function.metric_errors(period=Duration.minutes(5)),
                threshold=1, evaluation_periods=1,
                treat_missing_data=cloudwatch.TreatMissingData.NOT_BREACHING,
                alarm_description='Inspect ingestion/admission errors and control.stitching_admission; never kill the writer to clear backlog.',
            )

        # Optional: reconcile terminal ECS task events back into the Lambda
        # that originally dispatched the task.
        ecs_task_state_rule = project_config.get('ecs_task_state_rule')
        if ecs_task_state_rule:
            cluster_arn = ecs_task_state_rule.get('cluster_arn')
            task_family = ecs_task_state_rule.get('task_family')
            if not cluster_arn or not task_family:
                raise ValueError(
                    'ecs_task_state_rule requires cluster_arn and task_family'
                )
            rule = events.Rule(
                self,
                'EcsTaskStoppedRule',
                event_pattern=events.EventPattern(
                    source=['aws.ecs'],
                    detail_type=['ECS Task State Change'],
                    detail={
                        'clusterArn': [cluster_arn],
                        'group': [f'family:{task_family}'],
                        'lastStatus': ['STOPPED'],
                    },
                ),
            )
            rule.add_target(event_targets.LambdaFunction(lambda_function))

        # =================================================================================================================
        # Optional: Configure EventBridge rule if provided in the configuration
        # =================================================================================================================
        if project_config.get('add_permission_eventbridge', False):
            lambda_function.add_permission(
                "EventBridgePermission",
                principal=iam.ServicePrincipal("events.amazonaws.com"),
                action="lambda:InvokeFunction",
            )


    # ==================================================================================================================
    # Helper function to load a Lambda layer from an S3 bucket dynamically.
    # ==================================================================================================================
    def __get_layer_from_bucket(self, id: str, bucket: str, key: str) -> _lambda.LayerVersion:
        bucket_obj = s3.Bucket.from_bucket_name(self, "BucketCoLayer" + id, bucket)
        return _lambda.LayerVersion(
            self,
            id,
            code=_lambda.Code.from_bucket(
                bucket=bucket_obj,
                key=key
            )
        )

    def __generate_layer_id(self, key: str, index: int) -> str:
        key_file_name = key.split('/')[-1]
        file_stem = key_file_name.rsplit('.', 1)[0]
        cleaned = re.sub(r'[^A-Za-z0-9]+', ' ', file_stem).strip()
        normalized = ''.join(part.capitalize() for part in cleaned.split()) or 'Layer'
        return f'{normalized}Layer{index}'

    def __get_existing_security_group_ids(self, sg_config: dict) -> list[str]:
        existing_ids = sg_config.get('existing_ids', [])
        if isinstance(existing_ids, str):
            existing_ids = [existing_ids]

        return [security_group_id.strip() for security_group_id in existing_ids if isinstance(security_group_id, str) and security_group_id.strip()]

    def __validate_existing_security_group_config(self, sg_config: dict) -> None:
        create_only_fields = ['name', 'description', 'allow_all_outbound', 'ingress_rules']
        conflicting_fields = [field for field in create_only_fields if field in sg_config]
        if conflicting_fields:
            raise ValueError(
                'security_group.existing_ids cannot be combined with create-only fields: '
                + ', '.join(conflicting_fields)
            )

    def __generate_security_group_import_id(self, index: int) -> str:
        return f'ExistingLambdaSecurityGroup{index}'

    def __resolve_python_runtime(self, python_version: str) -> _lambda.Runtime:
        runtime_mapping = {
            '3.8': _lambda.Runtime.PYTHON_3_8,
            '3.9': _lambda.Runtime.PYTHON_3_9,
            '3.10': _lambda.Runtime.PYTHON_3_10,
            '3.11': _lambda.Runtime.PYTHON_3_11,
        }

        normalized = str(python_version).strip().lower()
        if normalized.startswith('python'):
            normalized = normalized[len('python'):]
        normalized = normalized.replace('_', '.')

        if normalized in runtime_mapping:
            return runtime_mapping[normalized]

        allowed_versions = ', '.join(runtime_mapping.keys())
        raise ValueError(f'Unsupported lambda python_version: {python_version}. Supported versions: {allowed_versions}')

    # ==================================================================================================================
    # Helper function to create an IAM role and attach managed policies dynamically.
    # ==================================================================================================================
    def __create_role_attach_policy(self, assumed_by, ids: list | None, managed_policy_arns: list,
                                    role_name='IAM_ROLE_LAMBDA') -> iam.Role:
        role = iam.Role(
            self,
            'IAM_ROLE_LAMBDA',
            assumed_by=assumed_by,
            role_name=role_name
        )

        resolved_ids = ids
        if not resolved_ids:
            resolved_ids = [
                self.__generate_policy_id(managed_policy_arn=managed_policy_arn, index=index)
                for index, managed_policy_arn in enumerate(managed_policy_arns, start=1)
            ]

        if len(resolved_ids) != len(managed_policy_arns):
            raise ValueError('ids and managed_policy_arns must have the same length')

        for id, managed_policy_arn in zip(resolved_ids, managed_policy_arns):
            role.add_managed_policy(
                iam.ManagedPolicy.from_managed_policy_arn(
                    self,
                    id,
                    managed_policy_arn
                )
            )
        return role

    def __generate_policy_id(self, managed_policy_arn: str, index: int) -> str:
        policy_name = managed_policy_arn.split('/')[-1] if managed_policy_arn else 'ManagedPolicy'
        cleaned = re.sub(r'[^A-Za-z0-9]+', ' ', policy_name).strip()
        normalized = ''.join(part.capitalize() for part in cleaned.split()) or 'ManagedPolicy'
        return f'{normalized}Policy{index}'
