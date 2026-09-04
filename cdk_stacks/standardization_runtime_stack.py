from __future__ import annotations

import json
from typing import Any

from aws_cdk import (
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
    aws_secretsmanager as secretsmanager,
    aws_sqs as sqs,
)
from constructs import Construct


class StandardizationRuntimeStack(Stack):
    """Isolated SQS-to-one-shot-ECS runtime for Elasticsearch standardization."""

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
            "StandardizationRuntimeKey",
            enable_key_rotation=True,
            alias=config.get("kms_alias"),
            removal_policy=RemovalPolicy.RETAIN,
        )
        dispatch_dlq = self._queue("DispatchDlq", key, visibility=60)
        result_dlq = self._queue("ResultDlq", key, visibility=60)
        dispatch = self._queue("DispatchQueue", key, visibility=300, dlq=dispatch_dlq)
        result = self._queue("ResultQueue", key, visibility=300, dlq=result_dlq)

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
                self, f"ImportedStandardizationSecurityGroup{position}", group_id
            )
            for position, group_id in enumerate(
                vpc_config["security_group_ids"], start=1
            )
        ]
        ecs_config = config["ecs"]
        cluster = ecs.Cluster.from_cluster_attributes(
            self,
            "ImportedCluster",
            cluster_name=ecs_config["cluster_name"],
            vpc=vpc,
            security_groups=security_groups,
        )
        task_role = iam.Role(
            self,
            "StandardizationTaskRole",
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
        )
        execution_role = iam.Role(
            self,
            "StandardizationExecutionRole",
            assumed_by=iam.ServicePrincipal("ecs-tasks.amazonaws.com"),
            managed_policies=[
                iam.ManagedPolicy.from_aws_managed_policy_name(
                    "service-role/AmazonECSTaskExecutionRolePolicy"
                )
            ],
        )
        task = ecs.FargateTaskDefinition(
            self,
            "StandardizationTaskDefinition",
            cpu=int(ecs_config.get("cpu", 2048)),
            memory_limit_mib=int(ecs_config.get("memory_limit_mib", 4096)),
            task_role=task_role,
            execution_role=execution_role,
        )
        task_logs = logs.LogGroup(
            self,
            "StandardizationTaskLogs",
            retention=logs.RetentionDays.ONE_MONTH,
            removal_policy=RemovalPolicy.RETAIN,
        )
        source_secret = secretsmanager.Secret.from_secret_complete_arn(
            self,
            "ImportedStandardizationSourceSecret",
            ecs_config["source_database_secret_arn"],
        )
        warehouse_secret = secretsmanager.Secret.from_secret_complete_arn(
            self,
            "ImportedStandardizationWarehouseSecret",
            ecs_config["warehouse_database_secret_arn"],
        )
        task.add_container(
            "StandardizationWorkerContainer",
            container_name=ecs_config["container_name"],
            image=ecs.ContainerImage.from_registry(ecs_config["image_uri"]),
            logging=ecs.LogDrivers.aws_logs(
                stream_prefix="elasticsearch-standardization",
                log_group=task_logs,
            ),
            stop_timeout=Duration.seconds(
                int(ecs_config.get("stop_timeout_seconds", 120))
            ),
            environment={
                "SOURCE_DATABASE_NAME": ecs_config.get(
                    "source_database_name", "elasticsearch"
                ),
                "WAREHOUSE_DATABASE_NAME": ecs_config.get(
                    "warehouse_database_name", "postgres"
                ),
                "STANDARDIZATION_PAGE_SIZE": str(
                    int(ecs_config.get("page_size", 5000))
                ),
                "STANDARDIZATION_LOAD_STATEMENT_TIMEOUT_SECONDS": str(
                    int(ecs_config.get("load_statement_timeout_seconds", 1800))
                ),
                "STANDARDIZATION_MERGE_LOCK_TIMEOUT_SECONDS": str(
                    int(ecs_config.get("merge_lock_timeout_seconds", 1800))
                ),
                "AWS_REGION": Stack.of(self).region,
            },
            secrets={
                "SOURCE_DATABASE_SECRET_JSON": ecs.Secret.from_secrets_manager(
                    source_secret
                ),
                "WAREHOUSE_DATABASE_SECRET_JSON": ecs.Secret.from_secrets_manager(
                    warehouse_secret
                ),
            },
        )
        source_secret.grant_read(execution_role)
        warehouse_secret.grant_read(execution_role)
        dispatcher = lambda_.Function(
            self,
            "StandardizationDispatcher",
            runtime=lambda_.Runtime.PYTHON_3_10,
            handler="lambda_function.lambda_handler",
            code=lambda_.Code.from_asset("_lambda/standardization_dispatcher"),
            timeout=Duration.seconds(60),
            memory_size=256,
            environment={
                "ECS_CLUSTER_ARN": cluster.cluster_arn,
                "TASK_DEFINITION_ARN": task.task_definition_arn,
                "CONTAINER_NAME": ecs_config["container_name"],
                "PRIVATE_SUBNET_IDS_JSON": json.dumps(vpc_config["private_subnet_ids"]),
                "SECURITY_GROUP_IDS_JSON": json.dumps(
                    vpc_config["security_group_ids"]
                ),
                "RESULT_QUEUE_URL": result.queue_url,
            },
            log_retention=logs.RetentionDays.ONE_MONTH,
        )
        dispatcher.add_event_source(
            event_sources.SqsEventSource(
                dispatch,
                batch_size=1,
                max_concurrency=int(config.get("dispatcher_concurrency", 5)),
                report_batch_item_failures=True,
            )
        )
        dispatcher.add_to_role_policy(
            iam.PolicyStatement(
                actions=["ecs:RunTask"],
                resources=[task.task_definition_arn],
            )
        )
        dispatcher.add_to_role_policy(
            iam.PolicyStatement(
                actions=["iam:PassRole"],
                resources=[task_role.role_arn, execution_role.role_arn],
            )
        )
        result.grant_send_messages(task_role)
        key.grant_encrypt_decrypt(task_role)
        backend = iam.Role.from_role_arn(
            self,
            "ImportedBackendRole",
            config["backend_principal_arn"],
            mutable=True,
        )
        dispatch.grant_send_messages(backend)
        result.grant_consume_messages(backend)
        key.grant_encrypt_decrypt(backend)
        cloudwatch.Alarm(
            self,
            "StandardizationDispatchDlqAlarm",
            metric=dispatch_dlq.metric_approximate_number_of_messages_visible(),
            threshold=1,
            evaluation_periods=1,
        )
        cloudwatch.Alarm(
            self,
            "StandardizationResultDlqAlarm",
            metric=result_dlq.metric_approximate_number_of_messages_visible(),
            threshold=1,
            evaluation_periods=1,
        )
        CfnOutput(self, "StandardizationDispatchQueueUrl", value=dispatch.queue_url)
        CfnOutput(self, "StandardizationResultQueueUrl", value=result.queue_url)
        CfnOutput(self, "StandardizationDispatchDlqArn", value=dispatch_dlq.queue_arn)
        CfnOutput(self, "StandardizationResultDlqArn", value=result_dlq.queue_arn)

    def _queue(
        self,
        construct_id: str,
        key: kms.IKey,
        *,
        visibility: int,
        dlq: sqs.IQueue | None = None,
    ) -> sqs.Queue:
        dead_letter_queue = (
            sqs.DeadLetterQueue(max_receive_count=5, queue=dlq) if dlq else None
        )
        return sqs.Queue(
            self,
            construct_id,
            encryption=sqs.QueueEncryption.KMS,
            encryption_master_key=key,
            retention_period=Duration.days(14),
            visibility_timeout=Duration.seconds(visibility),
            dead_letter_queue=dead_letter_queue,
        )


def _validate_config(config: dict[str, Any]) -> dict[str, Any]:
    missing = [name for name in ("vpc", "ecs", "backend_principal_arn") if not config.get(name)]
    if missing:
        raise ValueError(
            f"standardization runtime config missing: {', '.join(sorted(missing))}"
        )
    vpc_required = (
        "vpc_id",
        "availability_zones",
        "private_subnet_ids",
        "security_group_ids",
    )
    ecs_required = (
        "cluster_name",
        "container_name",
        "image_uri",
        "source_database_secret_arn",
        "warehouse_database_secret_arn",
    )
    for section, required in (("vpc", vpc_required), ("ecs", ecs_required)):
        section_missing = [name for name in required if not config[section].get(name)]
        if section_missing:
            raise ValueError(
                f"standardization runtime {section} config missing: "
                f"{', '.join(sorted(section_missing))}"
            )
    concurrency = int(config.get("dispatcher_concurrency", 5))
    if not 2 <= concurrency <= 20:
        raise ValueError("dispatcher_concurrency must be between 2 and 20")
    page_size = int(config["ecs"].get("page_size", 5000))
    if not 1000 <= page_size <= 5000:
        raise ValueError("ecs.page_size must be between 1000 and 5000")
    statement_timeout = int(
        config["ecs"].get("load_statement_timeout_seconds", 1800)
    )
    if not 60 <= statement_timeout <= 3600:
        raise ValueError(
            "ecs.load_statement_timeout_seconds must be between 60 and 3600"
        )
    merge_lock_timeout = int(
        config["ecs"].get("merge_lock_timeout_seconds", 1800)
    )
    if not 60 <= merge_lock_timeout <= 7200:
        raise ValueError(
            "ecs.merge_lock_timeout_seconds must be between 60 and 7200"
        )
    stop_timeout = int(config["ecs"].get("stop_timeout_seconds", 120))
    if not 30 <= stop_timeout <= 120:
        raise ValueError("ecs.stop_timeout_seconds must be between 30 and 120")
    return config
