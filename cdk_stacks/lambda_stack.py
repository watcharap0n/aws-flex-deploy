from aws_cdk import (
    Stack,
    aws_lambda as _lambda,
    aws_iam as iam,
    aws_ec2 as ec2,
    aws_s3 as s3,
    Duration,
    Size,
)
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
        ephemeral_storage_size_mb   = int(project_config.get('ephemeral_storage_size', 1024))
        self.ephemeral_storage_size = Size.mebibytes(ephemeral_storage_size_mb)
        self.environments           = project_config.get('environments', {})

        # =================================================================================================================
        # Dynamically configure Lambda layers
        # =================================================================================================================
        layers = []
        layers_config = project_config.get('layers', [])
        if self.bucket_layer_name and layers_config:
            for layer_conf in layers_config:
                layer_id = layer_conf.get('id')
                key = layer_conf.get('key')
                if layer_id and key:
                    layers.append(self.__get_layer_from_bucket(layer_id, self.bucket_layer_name, key))

        # =================================================================================================================
        # Create IAM role with policies dynamically.
        # =================================================================================================================
        role_config = project_config.get('role', {})
        assumed_by_value = role_config.get('assumed_by', "lambda.amazonaws.com")
        assumed_by = iam.ServicePrincipal(assumed_by_value)
        role_name = role_config.get('role_name', f'IamRole{self.lambda_function_name}')
        ids = role_config.get('ids', [
            'PolicyBasic'
        ])
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
        security_group = None
        subnet_type = None
        if self.vpc_id:
            vpc = ec2.Vpc.from_lookup(self, "VPC", vpc_id=self.vpc_id)

            # Dynamic Security Group configuration
            sg_config = project_config.get('security_group', {})
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
            subnet_type = ec2.SubnetType.PRIVATE_WITH_EGRESS
            print(f"Chosen VPC: {self.vpc_id}, Subnet Type: {subnet_type}")

        # =================================================================================================================
        # Assemble Lambda function properties using the dynamic configuration
        # =================================================================================================================
        lambda_kwargs = {
            "function_name": self.lambda_function_name,
            "code": _lambda.Code.from_asset(self.code_from_asset),
            "handler": self.function_handler,
            "runtime": _lambda.Runtime.PYTHON_3_9,
            "timeout": Duration.seconds(30),
            "environment": self.environments,
            "memory_size": self.memory_size,
            "ephemeral_storage_size": self.ephemeral_storage_size,
            "role": role,
            "layers": layers if layers else None
        }
        if vpc:
            lambda_kwargs["security_groups"] = [security_group]
            lambda_kwargs["vpc"] = vpc
            lambda_kwargs["allow_public_subnet"] = project_config.get('allow_public_subnet', False)
            lambda_kwargs["vpc_subnets"] = ec2.SubnetSelection(subnet_type=subnet_type)

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

    # ==================================================================================================================
    # Helper function to create an IAM role and attach managed policies dynamically.
    # ==================================================================================================================
    def __create_role_attach_policy(self, assumed_by, ids: list, managed_policy_arns: list,
                                    role_name='IAM_ROLE_LAMBDA') -> iam.Role:
        role = iam.Role(
            self,
            'IAM_ROLE_LAMBDA',
            assumed_by=assumed_by,
            role_name=role_name
        )
        if len(ids) != len(managed_policy_arns):
            raise ValueError('ids and managed_policy_arns must have the same length')
        for id, managed_policy_arn in zip(ids, managed_policy_arns):
            role.add_managed_policy(
                iam.ManagedPolicy.from_managed_policy_arn(
                    self,
                    id,
                    managed_policy_arn
                )
            )
        return role