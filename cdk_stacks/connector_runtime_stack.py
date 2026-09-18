from __future__ import annotations

import re
from typing import Any

from aws_cdk import (
    Aws,
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
    aws_cloudwatch as cloudwatch,
    aws_ec2 as ec2,
    aws_ecr as ecr,
    aws_ecs as ecs,
    aws_iam as iam,
    aws_kms as kms,
    aws_lambda as lambda_,
    aws_lambda_event_sources as event_sources,
    aws_logs as logs,
    aws_s3 as s3,
    aws_scheduler as scheduler,
    aws_sqs as sqs,
    aws_stepfunctions as sfn,
)
from constructs import Construct, IDependable


_ECR_DIGEST_URI_PATTERN = re.compile(
    r"^(?P<account>\d{12})\.dkr\.ecr\.(?P<region>[a-z0-9-]+)\."
    r"amazonaws\.com(?:\.cn)?/"
    r"(?P<repository>[a-z0-9]+(?:[._-][a-z0-9]+)*"
    r"(?:/[a-z0-9]+(?:[._-][a-z0-9]+)*)*)@"
    r"(?P<digest>sha256:[0-9a-f]{64})$"
)


class ConnectorRuntimeStack(Stack):
    """Connector orchestration with imported batch tasks and a managed warm worker."""

    def __init__(
        self,
        scope: Construct,
        construct_id: str,
        *,
        project_config: dict[str, Any],
        **kwargs: Any,
    ) -> None:
        super().__init__(scope, construct_id, **kwargs)
        config = _validate_config(project_config)
        if not config.get('stitching_admission_function_arn'):
            raise ValueError('connector runtime requires stitching_admission_function_arn; direct stitching dispatch is unsafe')

        key = kms.Key(
            self,
            "ConnectorRuntimeKey",
            enable_key_rotation=True,
            alias=config.get("kms_alias"),
            removal_policy=RemovalPolicy.RETAIN,
        )
        key.add_to_resource_policy(
            iam.PolicyStatement(
                sid="AllowRegionalCloudWatchLogsEncryption",
                principals=[
                    iam.ServicePrincipal(f"logs.{Aws.REGION}.{Aws.URL_SUFFIX}")
                ],
                actions=[
                    "kms:Encrypt*",
                    "kms:Decrypt*",
                    "kms:ReEncrypt*",
                    "kms:GenerateDataKey*",
                    "kms:Describe*",
                ],
                resources=["*"],
                conditions={
                    "ArnLike": {
                        "kms:EncryptionContext:aws:logs:arn": (
                            f"arn:{Aws.PARTITION}:logs:{Aws.REGION}:"
                            f"{Aws.ACCOUNT_ID}:log-group:{self.stack_name}-*"
                        )
                    }
                },
            )
        )
        queues = self._create_queues(key, config)

        vpc_config = config["vpc"]
        vpc = ec2.Vpc.from_vpc_attributes(
            self,
            "ImportedVpc",
            vpc_id=vpc_config["vpc_id"],
            availability_zones=vpc_config["availability_zones"],
            private_subnet_ids=vpc_config["private_subnet_ids"],
            private_subnet_route_table_ids=vpc_config.get(
                "private_subnet_route_table_ids"
            ),
        )
        security_groups = [
            ec2.SecurityGroup.from_security_group_id(
                self, f"ImportedConnectorSecurityGroup{index}", security_group_id
            )
            for index, security_group_id in enumerate(
                vpc_config["security_group_ids"], start=1
            )
        ]
        ecs_config = config["ecs"]
        cluster = ecs.Cluster.from_cluster_attributes(
            self,
            "ImportedConnectorCluster",
            cluster_name=ecs_config["cluster_name"],
            vpc=vpc,
            security_groups=security_groups,
        )
        connector_task = ecs.FargateTaskDefinition.from_fargate_task_definition_arn(
            self,
            "ImportedConnectorTaskDefinition",
            ecs_config["connector_task_definition_arn"],
        )
        stitcher_task = ecs.FargateTaskDefinition.from_fargate_task_definition_arn(
            self,
            "ImportedStitcherTaskDefinition",
            ecs_config["stitcher_task_definition_arn"],
        )
        connector_role = iam.Role.from_role_arn(
            self,
            "ImportedConnectorTaskRole",
            ecs_config["connector_task_role_arn"],
            mutable=True,
        )
        bucket = s3.Bucket.from_bucket_name(
            self, "ImportedConnectorLandingBucket", config["landing_bucket_name"]
        )
        fast_worker_resources = None
        if ecs_config.get("fast_worker_enabled", False):
            fast_worker_role, fast_worker_role_policy = (
                self._create_fast_worker_task_role(
                    fast_queue=queues["fast"],
                    bucket=bucket,
                    key=key,
                    config=config,
                )
            )
            (
                fast_worker_task,
                fast_worker_service,
                fast_worker_logs,
                execution_role_policy,
            ) = self._create_fast_worker(
                cluster=cluster,
                security_groups=security_groups,
                task_role=fast_worker_role,
                fast_queue=queues["fast"],
                key=key,
                config=config,
            )
            fast_worker_service.node.add_dependency(fast_worker_role_policy)
            fast_worker_service.node.add_dependency(execution_role_policy)
            fast_worker_resources = (
                fast_worker_task,
                fast_worker_service,
                fast_worker_logs,
            )

        scheduler_role, schedule_group = self._create_scheduler(
            queues["occurrence"], queues["occurrence_dlq"], key, config
        )
        state_machine = self._create_state_machine(
            cluster_arn=cluster.cluster_arn,
            connector_task_definition_arn=connector_task.task_definition_arn,
            stitcher_task_definition_arn=stitcher_task.task_definition_arn,
            security_group_ids=vpc_config["security_group_ids"],
            subnet_ids=vpc_config["private_subnet_ids"],
            result_queue=queues["result"],
            key=key,
            config=config,
        )
        dispatcher = self._create_dispatcher(queues["dispatch"], state_machine, config)
        self._grant_runtime_access(
            queues=queues,
            key=key,
            bucket=bucket,
            scheduler_role=scheduler_role,
            schedule_group=schedule_group,
            state_machine=state_machine,
            connector_role=connector_role,
            config=config,
        )
        self._create_alarms(queues, state_machine, config)
        self._outputs(
            queues=queues,
            key=key,
            scheduler_role=scheduler_role,
            schedule_group=schedule_group,
            state_machine=state_machine,
            dispatcher=dispatcher,
            fast_worker_resources=fast_worker_resources,
        )

    def _create_queues(
        self, key: kms.IKey, config: dict[str, Any]
    ) -> dict[str, sqs.Queue]:
        retention = Duration.days(int(config.get("queue_retention_days", 14)))

        def queue(
            name: str,
            *,
            visibility_seconds: int,
            dlq: sqs.Queue | None = None,
            fifo: bool | None = False,
            max_receive_count: int | None = None,
        ) -> sqs.Queue:
            dead_letter_queue = (
                sqs.DeadLetterQueue(
                    max_receive_count=(
                        max_receive_count
                        if max_receive_count is not None
                        else int(config.get("max_receive_count", 5))
                    ),
                    queue=dlq,
                )
                if dlq
                else None
            )
            queue_kwargs: dict[str, Any] = {
                "encryption": sqs.QueueEncryption.KMS,
                "encryption_master_key": key,
                "retention_period": retention,
                "visibility_timeout": Duration.seconds(visibility_seconds),
                "dead_letter_queue": dead_letter_queue,
            }
            if fifo:
                queue_kwargs["fifo"] = True
            return sqs.Queue(self, name, **queue_kwargs)

        occurrence_dlq = queue("ConnectorOccurrenceDlq", visibility_seconds=60)
        dispatch_dlq = queue("ConnectorDispatchDlq", visibility_seconds=60)
        result_dlq = queue("ConnectorResultDlq", visibility_seconds=60)
        queues = {
            "occurrence_dlq": occurrence_dlq,
            "dispatch_dlq": dispatch_dlq,
            "result_dlq": result_dlq,
            "occurrence": queue(
                "ConnectorOccurrenceQueue", visibility_seconds=300, dlq=occurrence_dlq
            ),
            "dispatch": queue(
                "ConnectorDispatchQueue", visibility_seconds=300, dlq=dispatch_dlq
            ),
            "result": queue("ConnectorResultQueue", visibility_seconds=300, dlq=result_dlq),
        }
        if config["ecs"].get("fast_worker_enabled", False):
            fast_dlq = queue(
                "ConnectorFastOperationDlq", visibility_seconds=60, fifo=None
            )
            queues.update(
                fast_dlq=fast_dlq,
                fast=queue(
                    "ConnectorFastOperationQueue",
                    visibility_seconds=int(
                        config.get("fast_queue_visibility_timeout_seconds", 300)
                    ),
                    dlq=fast_dlq,
                    fifo=None,
                    max_receive_count=int(
                        config.get("fast_worker_max_attempts", 5)
                    )
                    + 1,
                ),
            )
        return queues

    def _create_fast_worker_task_role(
        self,
        *,
        fast_queue: sqs.IQueue,
        bucket: s3.IBucket,
        key: kms.IKey,
        config: dict[str, Any],
    ) -> tuple[iam.Role, IDependable]:
        role = iam.Role(
            self,
            "ConnectorFastWorkerTaskRole",
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
        )
        bucket.grant_read_write(role)
        role.add_to_principal_policy(
            iam.PolicyStatement(
                actions=[
                    "sqs:ReceiveMessage",
                    "sqs:DeleteMessage",
                    "sqs:ChangeMessageVisibility",
                    "sqs:GetQueueAttributes",
                ],
                resources=[fast_queue.queue_arn],
            )
        )
        key.grant_encrypt_decrypt(role)
        role.add_to_principal_policy(
            iam.PolicyStatement(
                actions=["secretsmanager:GetSecretValue"],
                resources=[
                    config["warehouse_secret_arn"],
                    config["connector_source_secret_arn_pattern"],
                ],
            )
        )
        policy = role.node.try_find_child("DefaultPolicy")
        if policy is None:
            raise RuntimeError("fast worker task role policy was not created")
        return role, policy

    def _create_fast_worker(
        self,
        *,
        cluster: ecs.ICluster,
        security_groups: list[ec2.ISecurityGroup],
        task_role: iam.IRole,
        fast_queue: sqs.IQueue,
        key: kms.IKey,
        config: dict[str, Any],
    ) -> tuple[
        ecs.FargateTaskDefinition,
        ecs.FargateService,
        logs.LogGroup,
        IDependable,
    ]:
        ecs_config = config["ecs"]
        execution_role = iam.Role(
            self,
            "ConnectorFastWorkerExecutionRole",
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
        )
        task = ecs.FargateTaskDefinition(
            self,
            "ConnectorFastWorkerTaskDefinition",
            cpu=int(ecs_config.get("fast_worker_cpu", 1024)),
            memory_limit_mib=int(
                ecs_config.get("fast_worker_memory_limit_mib", 2048)
            ),
            task_role=task_role,
            execution_role=execution_role,
        )
        log_group = logs.LogGroup(
            self,
            "ConnectorFastWorkerLogs",
            retention=logs.RetentionDays.ONE_MONTH,
            encryption_key=key,
            removal_policy=RemovalPolicy.RETAIN,
        )
        shutdown_seconds = int(config.get("fast_worker_shutdown_grace_seconds", 110))
        image_uri = ecs_config["fast_worker_image_uri"]
        ecr_image = _parse_ecr_digest_uri(image_uri)
        if (
            ecr_image.group("account") != self.account
            or ecr_image.group("region") != self.region
        ):
            raise ValueError(
                "ecs.fast_worker_image_uri must use ECR in the stack account and region"
            )
        repository = ecr.Repository.from_repository_attributes(
            self,
            "ImportedConnectorFastWorkerRepository",
            repository_arn=(
                f"arn:{Aws.PARTITION}:ecr:{ecr_image.group('region')}:"
                f"{ecr_image.group('account')}:repository/"
                f"{ecr_image.group('repository')}"
            ),
            repository_name=ecr_image.group("repository"),
        )
        container_image = ecs.ContainerImage.from_ecr_repository(
            repository, ecr_image.group("digest")
        )
        task.add_container(
            "ConnectorFastWorkerContainer",
            container_name=ecs_config["fast_worker_container_name"],
            image=container_image,
            command=["consume-fast"],
            logging=ecs.LogDrivers.aws_logs(
                stream_prefix="connector-fast-worker", log_group=log_group
            ),
            stop_timeout=Duration.seconds(shutdown_seconds),
            environment={
                "AWS_REGION": Stack.of(self).region,
                "DB_SECRET_NAME": config["warehouse_secret_arn"],
                "CONNECTOR_LANDING_BUCKET": config["landing_bucket_name"],
                "CONNECTOR_KMS_KEY_ID": key.key_arn,
                "CONNECTOR_FAST_OPERATION_QUEUE_URL": fast_queue.queue_url,
                "FAST_OPERATION_VISIBILITY_TIMEOUT_SECONDS": str(
                    int(config.get("fast_queue_visibility_timeout_seconds", 300))
                ),
                "FAST_OPERATION_HEARTBEAT_SECONDS": str(
                    int(config.get("fast_worker_heartbeat_seconds", 60))
                ),
                "FAST_OPERATION_POLL_WAIT_SECONDS": str(
                    int(config.get("fast_worker_poll_wait_seconds", 20))
                ),
                "FAST_OPERATION_MAX_ATTEMPTS": str(
                    int(config.get("fast_worker_max_attempts", 5))
                ),
                "FAST_OPERATION_SHUTDOWN_GRACE_SECONDS": str(
                    shutdown_seconds
                ),
            },
        )
        service_kwargs: dict[str, Any] = {}
        if (
            ecs_config.get("fast_worker_capacity_provider", "FARGATE")
            == "FARGATE_SPOT"
        ):
            service_kwargs["capacity_provider_strategies"] = [
                ecs.CapacityProviderStrategy(
                    capacity_provider="FARGATE_SPOT",
                    weight=1,
                )
            ]
        service = ecs.FargateService(
            self,
            "ConnectorFastWorkerService",
            cluster=cluster,
            task_definition=task,
            desired_count=int(ecs_config.get("fast_worker_desired_count", 1)),
            assign_public_ip=False,
            security_groups=security_groups,
            vpc_subnets=ec2.SubnetSelection(subnets=cluster.vpc.private_subnets),
            circuit_breaker=ecs.DeploymentCircuitBreaker(rollback=True),
            **service_kwargs,
        )
        execution_role_policy = execution_role.node.try_find_child("DefaultPolicy")
        if execution_role_policy is None:
            raise RuntimeError("fast worker execution role policy was not created")
        return task, service, log_group, execution_role_policy

    def _create_scheduler(
        self,
        occurrence_queue: sqs.IQueue,
        occurrence_dlq: sqs.IQueue,
        key: kms.IKey,
        config: dict[str, Any],
    ) -> tuple[iam.Role, scheduler.CfnScheduleGroup]:
        group = scheduler.CfnScheduleGroup(
            self,
            "ConnectorScheduleGroup",
            name=config.get("scheduler_group_name"),
        )
        role = iam.Role(
            self,
            "ConnectorSchedulerRole",
            assumed_by=iam.ServicePrincipal("scheduler.amazonaws.com"),
        )
        occurrence_queue.grant_send_messages(role)
        occurrence_dlq.grant_send_messages(role)
        key.grant_encrypt_decrypt(role)
        return role, group

    def _create_state_machine(
        self,
        *,
        cluster_arn: str,
        connector_task_definition_arn: str,
        stitcher_task_definition_arn: str,
        security_group_ids: list[str],
        subnet_ids: list[str],
        result_queue: sqs.IQueue,
        key: kms.IKey,
        config: dict[str, Any],
    ) -> sfn.IStateMachine:
        role = iam.Role(
            self,
            "ConnectorStateMachineRole",
            assumed_by=iam.ServicePrincipal("states.amazonaws.com"),
        )
        admission_function = config.get("stitching_admission_function_arn")
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["ecs:RunTask"],
                resources=([connector_task_definition_arn] if admission_function else
                           [connector_task_definition_arn, stitcher_task_definition_arn]),
            )
        )
        if admission_function:
            role.add_to_policy(iam.PolicyStatement(
                actions=["lambda:InvokeFunction"], resources=[admission_function]
            ))
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["ecs:DescribeTasks", "ecs:StopTask"],
                resources=["*"],
                conditions={"ArnEquals": {"ecs:cluster": cluster_arn}},
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["events:PutTargets", "events:PutRule", "events:DescribeRule"],
                resources=[
                    f"arn:{Aws.PARTITION}:events:{Aws.REGION}:{Aws.ACCOUNT_ID}:rule/StepFunctionsGetEventsForECSTaskRule"
                ],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["iam:PassRole"], resources=config["ecs"]["pass_role_arns"]
            )
        )
        result_queue.grant_send_messages(role)
        key.grant_encrypt_decrypt(role)
        log_group = logs.LogGroup(
            self,
            "ConnectorStateMachineLogs",
            retention=logs.RetentionDays.ONE_MONTH,
            encryption_key=key,
            removal_policy=RemovalPolicy.RETAIN,
        )
        role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "logs:CreateLogDelivery",
                    "logs:GetLogDelivery",
                    "logs:UpdateLogDelivery",
                    "logs:DeleteLogDelivery",
                    "logs:ListLogDeliveries",
                    "logs:PutResourcePolicy",
                    "logs:DescribeResourcePolicies",
                    "logs:DescribeLogGroups",
                ],
                resources=["*"],
            )
        )
        role.add_to_policy(
            iam.PolicyStatement(
                actions=[
                    "xray:PutTraceSegments",
                    "xray:PutTelemetryRecords",
                    "xray:GetSamplingRules",
                    "xray:GetSamplingTargets",
                ],
                resources=["*"],
            )
        )
        network = {
            "AwsvpcConfiguration": {
                "Subnets": subnet_ids,
                "SecurityGroups": security_group_ids,
                "AssignPublicIp": "DISABLED",
            }
        }
        catch_all = [
            {
                "ErrorEquals": ["States.ALL"],
                "ResultPath": "$.error",
                "Next": "PrepareFailureResult",
            }
        ]
        ecs_config = config["ecs"]
        workflow_capacity_provider = ecs_config.get(
            "workflow_capacity_provider", "FARGATE"
        )
        ecs_retry = [
            {
                "ErrorEquals": ["ECS.AmazonECSException"],
                "IntervalSeconds": int(
                    ecs_config.get("workflow_retry_interval_seconds", 15)
                ),
                "MaxAttempts": int(
                    ecs_config.get("workflow_retry_max_attempts", 20)
                ),
                "BackoffRate": float(
                    ecs_config.get("workflow_retry_backoff_rate", 1.2)
                ),
                "MaxDelaySeconds": int(
                    ecs_config.get("workflow_retry_max_delay_seconds", 60)
                ),
            }
        ]

        def ecs_state(
            task_definition_arn: str,
            container_name: str,
            command_expression: str,
            next_state: str,
        ) -> dict[str, Any]:
            parameters = {
                "Cluster": cluster_arn,
                "TaskDefinition": task_definition_arn,
                "NetworkConfiguration": network,
                "Overrides": {
                    "ContainerOverrides": [
                        {
                            "Name": container_name,
                            "Command.$": command_expression,
                        }
                    ]
                },
            }
            if workflow_capacity_provider == "FARGATE_SPOT":
                parameters["CapacityProviderStrategy"] = [
                    {
                        "CapacityProvider": "FARGATE_SPOT",
                        "Weight": 1,
                        "Base": 0,
                    }
                ]
            else:
                parameters["LaunchType"] = "FARGATE"
            return {
                "Type": "Task",
                "Resource": "arn:aws:states:::ecs:runTask.sync",
                "Parameters": parameters,
                "ResultPath": "$.task_result",
                "Retry": ecs_retry,
                "Catch": catch_all,
                "Next": next_state,
            }

        definition = {
            "StartAt": "RouteConnectorOperation",
            "TimeoutSeconds": int(config.get("workflow_timeout_hours", 6)) * 3600,
            "States": {
                "RouteConnectorOperation": {
                    "Type": "Choice",
                    "Choices": [
                        {
                            "Variable": "$.operation_type",
                            "StringEquals": "DATASET_SYNC",
                            "Next": "ExtractAndLoadConnectorDataset",
                        },
                        {
                            "Or": [
                                {
                                    "Variable": "$.operation_type",
                                    "StringEquals": "CONNECTION_TEST",
                                },
                                {
                                    "Variable": "$.operation_type",
                                    "StringEquals": "SCHEMA_DISCOVERY",
                                },
                                {
                                    "Variable": "$.operation_type",
                                    "StringEquals": "DATASET_PREVIEW",
                                },
                            ],
                            "Next": "RunConnectorOperation",
                        },
                    ],
                    "Default": "UnsupportedOperation",
                },
                "RunConnectorOperation": ecs_state(
                    connector_task_definition_arn,
                    config["ecs"]["connector_container_name"],
                    "States.Array('execute','--operation-id',$.operation_id)",
                    "PrepareSuccessResult",
                ),
                "ExtractAndLoadConnectorDataset": ecs_state(
                    connector_task_definition_arn,
                    config["ecs"]["connector_container_name"],
                    "States.Array('execute','--operation-id',$.operation_id)",
                    "RunImportedIdentityStitcher",
                ),
                "RunImportedIdentityStitcher": ecs_state(
                    stitcher_task_definition_arn,
                    config["ecs"]["stitcher_container_name"],
                    "States.Array('run','--batch-id',$.ingestion_batch_id,'--job-id',$.stitching_job_id)",
                    "PrepareSuccessResult",
                ),
                "UnsupportedOperation": {
                    "Type": "Pass",
                    "Result": {"Error": "UnsupportedOperation"},
                    "ResultPath": "$.error",
                    "Next": "PrepareFailureResult",
                },
                "PrepareSuccessResult": {
                    "Type": "Pass",
                    "Parameters": {
                        "operation_id.$": "$.operation_id",
                        "status": "SUCCEEDED",
                        "metrics": {"workflow": "connector-runtime-v1"},
                    },
                    "ResultPath": "$.result_message",
                    "Next": "PublishConnectorSuccess",
                },
                "PublishConnectorSuccess": {
                    "Type": "Task",
                    "Resource": "arn:aws:states:::sqs:sendMessage",
                    "Parameters": {
                        "QueueUrl": result_queue.queue_url,
                        "MessageBody.$": "States.JsonToString($.result_message)",
                    },
                    "ResultPath": None,
                    "Next": "ConnectorWorkflowSucceeded",
                },
                "ConnectorWorkflowSucceeded": {"Type": "Succeed"},
                "PrepareFailureResult": {
                    "Type": "Pass",
                    "Parameters": {
                        "operation_id.$": "$.operation_id",
                        "status": "FAILED",
                        "error_code.$": "$.error.Error",
                        "error_message": "connector runtime failed",
                    },
                    "ResultPath": "$.result_message",
                    "Next": "PublishConnectorFailure",
                },
                "PublishConnectorFailure": {
                    "Type": "Task",
                    "Resource": "arn:aws:states:::sqs:sendMessage",
                    "Parameters": {
                        "QueueUrl": result_queue.queue_url,
                        "MessageBody.$": "States.JsonToString($.result_message)",
                    },
                    "ResultPath": None,
                    "Next": "ConnectorWorkflowFailed",
                },
                "ConnectorWorkflowFailed": {
                    "Type": "Fail",
                    "Error": "ConnectorRuntimeFailed",
                },
            },
        }
        if admission_function:
            # Queue waiting is durable and independent of the connector extract
            # budget. Stay within the Standard workflow's one-year maximum.
            definition["TimeoutSeconds"] = 604800
            states = definition["States"]
            for name in ('RunConnectorOperation', 'ExtractAndLoadConnectorDataset'):
                states[name]['TimeoutSeconds'] = int(config.get('workflow_timeout_hours',6))*3600
            states["RunImportedIdentityStitcher"] = {
                "Type": "Task", "Resource": "arn:aws:states:::lambda:invoke",
                "Parameters": {"FunctionName": admission_function, "Payload": {
                    "action": "stitching_job_status",
                    "ingestion_batch_id.$": "$.ingestion_batch_id",
                    "stitching_job_id.$": "$.stitching_job_id",
                }},
                "ResultPath": "$.stitching_result",
                "Retry": [{"ErrorEquals": ["Lambda.ServiceException", "Lambda.AWSLambdaException", "Lambda.SdkClientException", "Lambda.TooManyRequestsException"],
                           "IntervalSeconds": 10, "MaxAttempts": 6, "BackoffRate": 2}],
                "Catch": catch_all, "Next": "CheckStitchingCompletion",
            }
            states["CheckStitchingCompletion"] = {
                "Type": "Choice", "Choices": [
                    {"Variable": "$.stitching_result.Payload.status", "StringEquals": "SUCCEEDED", "Next": "PrepareSuccessResult"},
                    {"Variable": "$.stitching_result.Payload.status", "StringEquals": "FAILED", "Next": "PrepareStitchingFailure"},
                    {"Variable": "$.stitching_result.Payload.status", "StringEquals": "BLOCKED", "Next": "PrepareStitchingFailure"},
                    {"Variable": "$.stitching_result.Payload.overdue", "BooleanEquals": True, "Next": "PrepareStitchingOverdue"},
                ], "Default": "WaitForStitchingAdmission",
            }
            states["WaitForStitchingAdmission"] = {
                "Type": "Wait", "Seconds": 60, "Next": "RunImportedIdentityStitcher",
            }
            states["PrepareStitchingOverdue"] = {
                "Type": "Pass", "Result": {"Error": "StitchingCompletionOverdueWorkRetained"},
                "ResultPath": "$.error", "Next": "PrepareFailureResult",
            }
            states["PrepareStitchingFailure"] = {
                "Type": "Pass", "Result": {"Error": "StitchingJobFailed"},
                "ResultPath": "$.error", "Next": "PrepareFailureResult",
            }
        state_machine_resource = sfn.CfnStateMachine(
            self,
            "ConnectorStateMachine",
            definition=definition,
            role_arn=role.role_arn,
            state_machine_type="STANDARD",
            logging_configuration=sfn.CfnStateMachine.LoggingConfigurationProperty(
                destinations=[
                    sfn.CfnStateMachine.LogDestinationProperty(
                        cloud_watch_logs_log_group=(
                            sfn.CfnStateMachine.CloudWatchLogsLogGroupProperty(
                                log_group_arn=log_group.log_group_arn
                            )
                        )
                    )
                ],
                include_execution_data=False,
                level="ERROR",
            ),
            tracing_configuration=sfn.CfnStateMachine.TracingConfigurationProperty(
                enabled=True
            ),
        )
        state_machine_resource.node.add_dependency(log_group)
        state_machine_role_policy = role.node.try_find_child("DefaultPolicy")
        if state_machine_role_policy is None:
            raise RuntimeError("state machine role default policy was not created")
        state_machine_resource.node.add_dependency(state_machine_role_policy)
        return sfn.StateMachine.from_state_machine_arn(
            self,
            "ImportedConnectorStateMachine",
            state_machine_resource.attr_arn,
        )

    def _create_dispatcher(
        self,
        dispatch_queue: sqs.IQueue,
        state_machine: sfn.IStateMachine,
        config: dict[str, Any],
    ) -> lambda_.Function:
        function = lambda_.Function(
            self,
            "ConnectorDispatcher",
            runtime=lambda_.Runtime.PYTHON_3_11,
            handler="lambda_function.lambda_handler",
            code=lambda_.Code.from_asset("_lambda/connector_dispatcher"),
            timeout=Duration.seconds(30),
            memory_size=256,
            environment={"STEP_FUNCTION_ARN": state_machine.state_machine_arn},
        )
        state_machine.grant_start_execution(function)
        function.add_event_source(
            event_sources.SqsEventSource(
                dispatch_queue,
                batch_size=10,
                report_batch_item_failures=True,
                max_batching_window=Duration.seconds(5),
                max_concurrency=int(config.get("dispatcher_concurrency", 5)),
            )
        )
        return function

    def _grant_runtime_access(
        self,
        *,
        queues: dict[str, sqs.Queue],
        key: kms.IKey,
        bucket: s3.IBucket,
        scheduler_role: iam.IRole,
        schedule_group: scheduler.CfnScheduleGroup,
        state_machine: sfn.IStateMachine,
        connector_role: iam.IRole,
        config: dict[str, Any],
    ) -> None:
        bucket.grant_read_write(connector_role)
        key.grant_encrypt_decrypt(connector_role)
        connector_role.add_to_principal_policy(
            iam.PolicyStatement(
                actions=["secretsmanager:GetSecretValue"],
                resources=[
                    config["warehouse_secret_arn"],
                    config["connector_source_secret_arn_pattern"],
                ],
            )
        )

        backend_role = iam.Role.from_role_arn(
            self,
            "ImportedBackendRole",
            config["backend_principal_arn"],
            mutable=True,
        )
        queues["dispatch"].grant_send_messages(backend_role)
        if "fast" in queues:
            queues["fast"].grant_send_messages(backend_role)
        queues["occurrence"].grant_consume_messages(backend_role)
        queues["result"].grant_consume_messages(backend_role)
        key.grant_encrypt_decrypt(backend_role)
        backend_role.add_to_principal_policy(
            iam.PolicyStatement(
                actions=[
                    "scheduler:CreateSchedule",
                    "scheduler:GetSchedule",
                    "scheduler:UpdateSchedule",
                    "scheduler:DeleteSchedule",
                ],
                resources=[
                    f"arn:{Aws.PARTITION}:scheduler:{Aws.REGION}:{Aws.ACCOUNT_ID}:schedule/{schedule_group.ref}/*"
                ],
            )
        )
        backend_role.add_to_principal_policy(
            iam.PolicyStatement(
                actions=["iam:PassRole"],
                resources=[scheduler_role.role_arn],
                conditions={"StringEquals": {"iam:PassedToService": "scheduler.amazonaws.com"}},
            )
        )
        state_machine.grant_read(backend_role)

    def _create_alarms(
        self,
        queues: dict[str, sqs.Queue],
        state_machine: sfn.IStateMachine,
        config: dict[str, Any],
    ) -> None:
        for name in ("occurrence_dlq", "dispatch_dlq", "result_dlq"):
            cloudwatch.Alarm(
                self,
                f"{name.title().replace('_', '')}VisibleAlarm",
                metric=queues[name].metric_approximate_number_of_messages_visible(),
                threshold=1,
                evaluation_periods=1,
                alarm_description=f"Connector runtime has messages in {name}",
            )
        if "fast" in queues:
            cloudwatch.Alarm(
                self,
                "FastDlqVisibleAlarm",
                metric=queues["fast_dlq"].metric_approximate_number_of_messages_visible(),
                threshold=1,
                evaluation_periods=1,
                alarm_description="Connector runtime has messages in fast_dlq",
            )
            cloudwatch.Alarm(
                self,
                "ConnectorFastOperationOldestAgeAlarm",
                metric=queues["fast"].metric_approximate_age_of_oldest_message(),
                threshold=int(config.get("fast_queue_oldest_age_alarm_seconds", 120)),
                evaluation_periods=1,
                alarm_description="Oldest connector fast operation exceeds the target age",
            )
            cloudwatch.Alarm(
                self,
                "ConnectorFastOperationBacklogAlarm",
                metric=queues["fast"].metric_approximate_number_of_messages_visible(),
                threshold=int(config.get("fast_queue_backlog_alarm_threshold", 10)),
                evaluation_periods=1,
                alarm_description="Connector fast-operation queue backlog is elevated",
            )
        cloudwatch.Alarm(
            self,
            "ConnectorWorkflowFailureAlarm",
            metric=state_machine.metric_failed(),
            threshold=int(config.get("workflow_failure_alarm_threshold", 1)),
            evaluation_periods=1,
        )

    def _outputs(
        self,
        *,
        queues: dict[str, sqs.Queue],
        key: kms.IKey,
        scheduler_role: iam.IRole,
        schedule_group: scheduler.CfnScheduleGroup,
        state_machine: sfn.IStateMachine,
        dispatcher: lambda_.IFunction,
        fast_worker_resources: tuple[
            ecs.FargateTaskDefinition, ecs.FargateService, logs.ILogGroup
        ]
        | None,
    ) -> None:
        values = {
            "ConnectorOccurrenceQueueUrl": queues["occurrence"].queue_url,
            "ConnectorOccurrenceDlqArn": queues["occurrence_dlq"].queue_arn,
            "ConnectorDispatchQueueUrl": queues["dispatch"].queue_url,
            "ConnectorResultQueueUrl": queues["result"].queue_url,
            "ConnectorSchedulerGroup": schedule_group.ref,
            "ConnectorSchedulerRoleArn": scheduler_role.role_arn,
            "ConnectorStateMachineArn": state_machine.state_machine_arn,
            "ConnectorDispatcherArn": dispatcher.function_arn,
            "ConnectorRuntimeKmsKeyArn": key.key_arn,
        }
        if fast_worker_resources is not None:
            fast_worker_task, fast_worker_service, fast_worker_logs = (
                fast_worker_resources
            )
            values.update(
                ConnectorFastOperationQueueUrl=queues["fast"].queue_url,
                ConnectorFastOperationQueueArn=queues["fast"].queue_arn,
                ConnectorFastOperationDlqArn=queues["fast_dlq"].queue_arn,
                ConnectorFastWorkerServiceName=fast_worker_service.service_name,
                ConnectorFastWorkerServiceArn=fast_worker_service.service_arn,
                ConnectorFastWorkerTaskDefinitionArn=(
                    fast_worker_task.task_definition_arn
                ),
                ConnectorFastWorkerLogGroupName=fast_worker_logs.log_group_name,
                ConnectorFastWorkerLogGroupArn=fast_worker_logs.log_group_arn,
            )
        for name, value in values.items():
            CfnOutput(self, name, value=value)


def _validate_config(config: dict[str, Any]) -> dict[str, Any]:
    required = [
        "vpc",
        "ecs",
        "landing_bucket_name",
        "warehouse_secret_arn",
        "connector_source_secret_arn_pattern",
        "backend_principal_arn",
    ]
    missing = [name for name in required if not config.get(name)]
    if missing:
        raise ValueError("connector runtime config missing: " + ", ".join(missing))
    vpc_required = [
        "vpc_id",
        "availability_zones",
        "private_subnet_ids",
        "security_group_ids",
    ]
    ecs_required = [
        "cluster_name",
        "connector_task_definition_arn",
        "connector_container_name",
        "connector_task_role_arn",
        "stitcher_task_definition_arn",
        "stitcher_container_name",
        "pass_role_arns",
    ]
    for section, names in (("vpc", vpc_required), ("ecs", ecs_required)):
        absent = [name for name in names if not config[section].get(name)]
        if absent:
            raise ValueError(
                f"connector runtime {section} config missing: " + ", ".join(absent)
            )
    fast_worker_enabled = config["ecs"].get("fast_worker_enabled", False)
    if not isinstance(fast_worker_enabled, bool):
        raise ValueError("ecs.fast_worker_enabled must be a boolean")
    capacity_provider = config["ecs"].get(
        "fast_worker_capacity_provider", "FARGATE"
    )
    if capacity_provider not in {"FARGATE", "FARGATE_SPOT"}:
        raise ValueError(
            "ecs.fast_worker_capacity_provider must be FARGATE or FARGATE_SPOT"
        )
    workflow_capacity_provider = config["ecs"].get(
        "workflow_capacity_provider", "FARGATE"
    )
    if workflow_capacity_provider not in {"FARGATE", "FARGATE_SPOT"}:
        raise ValueError(
            "ecs.workflow_capacity_provider must be FARGATE or FARGATE_SPOT"
        )
    workflow_retry_values = {
        "workflow_retry_interval_seconds": int(
            config["ecs"].get("workflow_retry_interval_seconds", 15)
        ),
        "workflow_retry_max_attempts": int(
            config["ecs"].get("workflow_retry_max_attempts", 20)
        ),
        "workflow_retry_backoff_rate": float(
            config["ecs"].get("workflow_retry_backoff_rate", 1.2)
        ),
        "workflow_retry_max_delay_seconds": int(
            config["ecs"].get("workflow_retry_max_delay_seconds", 60)
        ),
    }
    for name, value in workflow_retry_values.items():
        if value <= 0:
            raise ValueError(f"ecs.{name} must be positive")
    if not fast_worker_enabled:
        return config

    fast_required = ["fast_worker_image_uri", "fast_worker_container_name"]
    fast_missing = [name for name in fast_required if not config["ecs"].get(name)]
    if fast_missing:
        raise ValueError(
            "connector runtime ecs config missing: " + ", ".join(fast_missing)
        )
    _parse_ecr_digest_uri(str(config["ecs"]["fast_worker_image_uri"]))

    visibility = int(config.get("fast_queue_visibility_timeout_seconds", 300))
    heartbeat = int(config.get("fast_worker_heartbeat_seconds", 60))
    poll_wait = int(config.get("fast_worker_poll_wait_seconds", 20))
    max_attempts = int(config.get("fast_worker_max_attempts", 5))
    shutdown_grace = int(config.get("fast_worker_shutdown_grace_seconds", 110))
    desired_count = int(config["ecs"].get("fast_worker_desired_count", 1))
    if not 1 <= visibility <= 43_200:
        raise ValueError(
            "fast_queue_visibility_timeout_seconds must be between 1 and 43200"
        )
    if heartbeat < 1 or heartbeat * 2 > visibility:
        raise ValueError(
            "fast_worker_heartbeat_seconds must be positive and no more than half "
            "the queue visibility timeout"
        )
    if not 1 <= poll_wait <= 20:
        raise ValueError("fast_worker_poll_wait_seconds must be between 1 and 20")
    if not 1 <= max_attempts <= 1_000:
        raise ValueError("fast_worker_max_attempts must be between 1 and 1000")
    if not 2 <= shutdown_grace <= 120:
        raise ValueError(
            "fast_worker_shutdown_grace_seconds must be between 2 and 120"
        )
    if desired_count < 0:
        raise ValueError("ecs.fast_worker_desired_count must not be negative")
    return config


def _parse_ecr_digest_uri(image_uri: str) -> re.Match[str]:
    match = _ECR_DIGEST_URI_PATTERN.fullmatch(image_uri)
    if match is None:
        raise ValueError(
            "ecs.fast_worker_image_uri must be an ECR image URI pinned to a "
            "lowercase sha256 digest"
        )
    return match
