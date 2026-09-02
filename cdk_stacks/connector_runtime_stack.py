from __future__ import annotations

from typing import Any

from aws_cdk import (
    Aws,
    CfnOutput,
    Duration,
    RemovalPolicy,
    Stack,
    aws_cloudwatch as cloudwatch,
    aws_ec2 as ec2,
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
from constructs import Construct


class ConnectorRuntimeStack(Stack):
    """Connector orchestration resources with ECS compute imported from configuration."""

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
            scheduler_role=scheduler_role,
            schedule_group=schedule_group,
            state_machine=state_machine,
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
        )

    def _create_queues(
        self, key: kms.IKey, config: dict[str, Any]
    ) -> dict[str, sqs.Queue]:
        retention = Duration.days(int(config.get("queue_retention_days", 14)))

        def queue(name: str, *, visibility_seconds: int, dlq: sqs.Queue | None = None) -> sqs.Queue:
            dead_letter_queue = (
                sqs.DeadLetterQueue(
                    max_receive_count=int(config.get("max_receive_count", 5)), queue=dlq
                )
                if dlq
                else None
            )
            return sqs.Queue(
                self,
                name,
                encryption=sqs.QueueEncryption.KMS,
                encryption_master_key=key,
                retention_period=retention,
                visibility_timeout=Duration.seconds(visibility_seconds),
                dead_letter_queue=dead_letter_queue,
            )

        occurrence_dlq = queue("ConnectorOccurrenceDlq", visibility_seconds=60)
        dispatch_dlq = queue("ConnectorDispatchDlq", visibility_seconds=60)
        result_dlq = queue("ConnectorResultDlq", visibility_seconds=60)
        return {
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
        role.add_to_policy(
            iam.PolicyStatement(
                actions=["ecs:RunTask"],
                resources=[connector_task_definition_arn, stitcher_task_definition_arn],
            )
        )
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

        def ecs_state(
            task_definition_arn: str,
            container_name: str,
            command_expression: str,
            next_state: str,
        ) -> dict[str, Any]:
            return {
                "Type": "Task",
                "Resource": "arn:aws:states:::ecs:runTask.sync",
                "Parameters": {
                    "Cluster": cluster_arn,
                    "TaskDefinition": task_definition_arn,
                    "LaunchType": "FARGATE",
                    "NetworkConfiguration": network,
                    "Overrides": {
                        "ContainerOverrides": [
                            {
                                "Name": container_name,
                                "Command.$": command_expression,
                            }
                        ]
                    },
                },
                "ResultPath": "$.task_result",
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
        scheduler_role: iam.IRole,
        schedule_group: scheduler.CfnScheduleGroup,
        state_machine: sfn.IStateMachine,
        config: dict[str, Any],
    ) -> None:
        connector_role = iam.Role.from_role_arn(
            self,
            "ImportedConnectorTaskRole",
            config["ecs"]["connector_task_role_arn"],
            mutable=True,
        )
        bucket = s3.Bucket.from_bucket_name(
            self, "ImportedConnectorLandingBucket", config["landing_bucket_name"]
        )
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
    return config
