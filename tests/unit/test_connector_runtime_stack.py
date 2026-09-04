import aws_cdk as core
import aws_cdk.assertions as assertions
import pytest

from cdk_stacks.connector_runtime_stack import ConnectorRuntimeStack


def _config(fast_worker_enabled=True):
    account = "111111111111"
    region = "ap-southeast-1"
    config = {
        "vpc": {
            "vpc_id": "vpc-12345678",
            "availability_zones": [f"{region}a", f"{region}b"],
            "private_subnet_ids": ["subnet-11111111", "subnet-22222222"],
            "private_subnet_route_table_ids": ["rtb-11111111", "rtb-22222222"],
            "security_group_ids": ["sg-11111111"],
        },
        "ecs": {
            "cluster_name": "identity-cluster",
            "connector_task_definition_arn": (
                f"arn:aws:ecs:{region}:{account}:task-definition/connector-worker:12"
            ),
            "connector_container_name": "connector-worker",
            "connector_task_role_arn": f"arn:aws:iam::{account}:role/connector-task",
            "fast_worker_enabled": fast_worker_enabled,
            "stitcher_task_definition_arn": (
                f"arn:aws:ecs:{region}:{account}:task-definition/identity-stitcher:8"
            ),
            "stitcher_container_name": "identity-stitcher",
            "pass_role_arns": [
                f"arn:aws:iam::{account}:role/connector-task",
                f"arn:aws:iam::{account}:role/connector-execution",
                f"arn:aws:iam::{account}:role/stitcher-task",
                f"arn:aws:iam::{account}:role/stitcher-execution",
            ],
        },
        "landing_bucket_name": "connector-landing-bucket",
        "warehouse_secret_arn": (
            f"arn:aws:secretsmanager:{region}:{account}:secret:warehouse-AbCdEf"
        ),
        "connector_source_secret_arn_pattern": (
            f"arn:aws:secretsmanager:{region}:{account}:secret:connector/*"
        ),
        "backend_principal_arn": f"arn:aws:iam::{account}:role/journey-builder-api",
    }
    if fast_worker_enabled:
        config["ecs"].update(
            {
                "fast_worker_image_uri": (
                    f"{account}.dkr.ecr.{region}.amazonaws.com/"
                    f"connector-worker@sha256:{'a' * 64}"
                ),
                "fast_worker_container_name": "connector-fast-worker",
            }
        )
    return config


def test_connector_runtime_synthesizes_fast_worker_service():
    app = core.App()
    stack = ConnectorRuntimeStack(
        app,
        "ConnectorRuntimeTest",
        project_config=_config(),
        env=core.Environment(account="111111111111", region="ap-southeast-1"),
    )
    template = assertions.Template.from_stack(stack)
    template.resource_count_is("AWS::SQS::Queue", 8)
    template.resource_count_is("AWS::KMS::Key", 1)
    template.resource_count_is("AWS::Scheduler::ScheduleGroup", 1)
    template.resource_count_is("AWS::StepFunctions::StateMachine", 1)
    template.resource_count_is("AWS::Lambda::Function", 1)
    template.resource_count_is("AWS::ECS::Service", 1)
    template.resource_count_is("AWS::ECS::TaskDefinition", 1)
    template.resource_count_is("AWS::ECR::Repository", 0)


def test_connector_runtime_disabled_preserves_legacy_resource_shape():
    app = core.App()
    stack = ConnectorRuntimeStack(
        app,
        "ConnectorRuntimeDisabledTest",
        project_config=_config(fast_worker_enabled=False),
        env=core.Environment(account="111111111111", region="ap-southeast-1"),
    )
    template = assertions.Template.from_stack(stack)
    template.resource_count_is("AWS::SQS::Queue", 6)
    template.resource_count_is("AWS::ECS::Service", 0)
    template.resource_count_is("AWS::ECS::TaskDefinition", 0)
    rendered = template.to_json()
    assert not any(name.startswith("ConnectorFast") for name in rendered["Outputs"])
    assert "ConnectorFastOperationQueue" not in str(rendered)


def test_state_machine_runs_connector_then_stitcher_for_sync():
    app = core.App()
    stack = ConnectorRuntimeStack(
        app,
        "ConnectorRuntimeWorkflowTest",
        project_config=_config(),
        env=core.Environment(account="111111111111", region="ap-southeast-1"),
    )
    template = assertions.Template.from_stack(stack).to_json()
    rendered = str(template)
    assert "arn:aws:states:::ecs:runTask.sync" in rendered
    assert "task-definition/connector-worker:12" in rendered
    assert "task-definition/identity-stitcher:8" in rendered
    assert "--operation-id" in rendered
    assert "--batch-id" in rendered
    assert "--job-id" in rendered
    assert "$.task_result" in rendered


def test_state_machine_routes_dataset_preview_to_connector_worker():
    app = core.App()
    stack = ConnectorRuntimeStack(
        app,
        "ConnectorRuntimePreviewTest",
        project_config=_config(),
        env=core.Environment(account="111111111111", region="ap-southeast-1"),
    )
    resources = assertions.Template.from_stack(stack).to_json()["Resources"]
    state_machine = next(
        resource
        for resource in resources.values()
        if resource["Type"] == "AWS::StepFunctions::StateMachine"
    )
    states = state_machine["Properties"]["Definition"]["States"]
    route = states["RouteConnectorOperation"]
    connector_choice = next(
        choice for choice in route["Choices"] if choice.get("Next") == "RunConnectorOperation"
    )
    assert {condition["StringEquals"] for condition in connector_choice["Or"]} >= {
        "CONNECTION_TEST",
        "SCHEMA_DISCOVERY",
        "DATASET_PREVIEW",
    }
    assert states["RunConnectorOperation"]["Parameters"]["TaskDefinition"] == (
        "arn:aws:ecs:ap-southeast-1:111111111111:task-definition/connector-worker:12"
    )


def test_fast_operation_fifo_queue_uses_kms_retention_and_redrive():
    app = core.App()
    config = _config()
    config["queue_retention_days"] = 10
    config["fast_queue_visibility_timeout_seconds"] = 420
    config["fast_worker_max_attempts"] = 7
    stack = ConnectorRuntimeStack(
        app,
        "ConnectorRuntimeFastQueueTest",
        project_config=config,
        env=core.Environment(account="111111111111", region="ap-southeast-1"),
    )
    resources = assertions.Template.from_stack(stack).to_json()["Resources"]
    fast_dlq_id = next(
        logical_id
        for logical_id, resource in resources.items()
        if logical_id.startswith("ConnectorFastOperationDlq")
        and resource["Type"] == "AWS::SQS::Queue"
    )
    fast_queue = next(
        resource
        for logical_id, resource in resources.items()
        if logical_id.startswith("ConnectorFastOperationQueue")
        and resource["Type"] == "AWS::SQS::Queue"
    )
    assert resources[fast_dlq_id]["Properties"]["FifoQueue"] is True
    assert fast_queue["Properties"]["FifoQueue"] is True
    assert "Fn::GetAtt" in fast_queue["Properties"]["KmsMasterKeyId"]
    assert fast_queue["Properties"]["MessageRetentionPeriod"] == 864000
    assert fast_queue["Properties"]["RedrivePolicy"] == {
        "deadLetterTargetArn": {"Fn::GetAtt": [fast_dlq_id, "Arn"]},
        "maxReceiveCount": 8,
    }
    assert fast_queue["Properties"]["VisibilityTimeout"] == 420


def test_fast_worker_uses_private_service_and_worker_environment():
    app = core.App()
    config = _config()
    config.update(
        {
            "fast_queue_visibility_timeout_seconds": 420,
            "fast_worker_heartbeat_seconds": 70,
            "fast_worker_poll_wait_seconds": 15,
            "fast_worker_max_attempts": 7,
            "fast_worker_shutdown_grace_seconds": 45,
        }
    )
    config["ecs"].update(
        {
            "fast_worker_cpu": 512,
            "fast_worker_memory_limit_mib": 1024,
            "fast_worker_desired_count": 2,
        }
    )
    stack = ConnectorRuntimeStack(
        app,
        "ConnectorRuntimeFastWorkerTest",
        project_config=config,
        env=core.Environment(account="111111111111", region="ap-southeast-1"),
    )
    resources = assertions.Template.from_stack(stack).to_json()["Resources"]
    task = next(
        resource
        for resource in resources.values()
        if resource["Type"] == "AWS::ECS::TaskDefinition"
    )
    container = task["Properties"]["ContainerDefinitions"][0]
    environment = {
        item["Name"]: item["Value"] for item in container["Environment"]
    }
    assert task["Properties"]["Cpu"] == "512"
    assert task["Properties"]["Memory"] == "1024"
    task_role_id = next(
        logical_id
        for logical_id, resource in resources.items()
        if logical_id.startswith("ConnectorFastWorkerTaskRole")
        and resource["Type"] == "AWS::IAM::Role"
    )
    assert resources[task_role_id]["Properties"]["AssumeRolePolicyDocument"][
        "Statement"
    ][0]["Principal"] == {"Service": "ecs-tasks.amazonaws.com"}
    assert task["Properties"]["TaskRoleArn"] == {
        "Fn::GetAtt": [task_role_id, "Arn"]
    }
    assert container["Command"] == ["consume-fast"]
    assert container["Name"] == "connector-fast-worker"
    assert container["StopTimeout"] == 45
    assert environment["AWS_REGION"] == "ap-southeast-1"
    assert environment["DB_SECRET_NAME"].endswith(":secret:warehouse-AbCdEf")
    assert environment["CONNECTOR_LANDING_BUCKET"] == "connector-landing-bucket"
    assert "Fn::GetAtt" in environment["CONNECTOR_KMS_KEY_ID"]
    assert "Ref" in environment["CONNECTOR_FAST_OPERATION_QUEUE_URL"]
    assert environment["FAST_OPERATION_VISIBILITY_TIMEOUT_SECONDS"] == "420"
    assert environment["FAST_OPERATION_HEARTBEAT_SECONDS"] == "70"
    assert environment["FAST_OPERATION_POLL_WAIT_SECONDS"] == "15"
    assert environment["FAST_OPERATION_MAX_ATTEMPTS"] == "7"
    assert environment["FAST_OPERATION_SHUTDOWN_GRACE_SECONDS"] == "45"
    assert not {
        "CONNECTOR_FAST_OPERATION_VISIBILITY_TIMEOUT_SECONDS",
        "CONNECTOR_FAST_OPERATION_HEARTBEAT_SECONDS",
        "CONNECTOR_FAST_OPERATION_POLL_WAIT_SECONDS",
        "CONNECTOR_FAST_OPERATION_MAX_ATTEMPTS",
        "CONNECTOR_FAST_OPERATION_SHUTDOWN_GRACE_SECONDS",
    } & environment.keys()

    service = next(
        resource
        for resource in resources.values()
        if resource["Type"] == "AWS::ECS::Service"
    )
    assert service["Properties"]["DesiredCount"] == 2
    assert service["Properties"]["DeploymentConfiguration"][
        "DeploymentCircuitBreaker"
    ] == {"Enable": True, "Rollback": True}
    assert service["Properties"]["NetworkConfiguration"] == {
        "AwsvpcConfiguration": {
            "AssignPublicIp": "DISABLED",
            "SecurityGroups": ["sg-11111111"],
            "Subnets": ["subnet-11111111", "subnet-22222222"],
        }
    }
    task_role_policy_id = next(
        logical_id
        for logical_id, resource in resources.items()
        if resource["Type"] == "AWS::IAM::Policy"
        and logical_id.startswith("ConnectorFastWorkerTaskRoleDefaultPolicy")
    )
    execution_role_policy_id = next(
        logical_id
        for logical_id, resource in resources.items()
        if resource["Type"] == "AWS::IAM::Policy"
        and logical_id.startswith("ConnectorFastWorkerExecutionRoleDefaultPolicy")
    )
    assert task_role_policy_id in service["DependsOn"]
    assert execution_role_policy_id in service["DependsOn"]

    worker_log_group = next(
        resource
        for logical_id, resource in resources.items()
        if logical_id.startswith("ConnectorFastWorkerLogs")
        and resource["Type"] == "AWS::Logs::LogGroup"
    )
    assert "LogGroupName" not in worker_log_group["Properties"]
    assert not any(
        resource["Type"].startswith("AWS::ElasticLoadBalancingV2")
        for resource in resources.values()
    )


def test_fast_queue_iam_grants_are_scoped_to_worker_and_backend():
    app = core.App()
    stack = ConnectorRuntimeStack(
        app,
        "ConnectorRuntimeFastIamTest",
        project_config=_config(),
        env=core.Environment(account="111111111111", region="ap-southeast-1"),
    )
    resources = assertions.Template.from_stack(stack).to_json()["Resources"]
    task_policy = next(
        resource
        for logical_id, resource in resources.items()
        if resource["Type"] == "AWS::IAM::Policy"
        and logical_id.startswith("ConnectorFastWorkerTaskRoleDefaultPolicy")
    )
    statements = task_policy["Properties"]["PolicyDocument"]["Statement"]
    consume = next(
        statement
        for statement in statements
        if isinstance(statement["Action"], list)
        and "sqs:ReceiveMessage" in statement["Action"]
    )
    assert set(consume["Action"]) == {
        "sqs:ReceiveMessage",
        "sqs:DeleteMessage",
        "sqs:ChangeMessageVisibility",
        "sqs:GetQueueAttributes",
    }
    assert consume["Resource"]["Fn::GetAtt"][0].startswith(
        "ConnectorFastOperationQueue"
    )

    imported_connector_policy = next(
        resource
        for logical_id, resource in resources.items()
        if resource["Type"] == "AWS::IAM::Policy"
        and logical_id.startswith("ImportedConnectorTaskRolePolicy")
    )
    imported_connector_actions = {
        action
        for statement in imported_connector_policy["Properties"]["PolicyDocument"][
            "Statement"
        ]
        for action in (
            statement["Action"]
            if isinstance(statement["Action"], list)
            else [statement["Action"]]
        )
    }
    assert not any(action.startswith("sqs:") for action in imported_connector_actions)
    assert {
        "s3:GetObject*",
        "s3:PutObject",
        "kms:Encrypt",
        "kms:Decrypt",
        "secretsmanager:GetSecretValue",
    } <= imported_connector_actions

    backend_policy = next(
        resource
        for resource in resources.values()
        if resource["Type"] == "AWS::IAM::Policy"
        and resource["Properties"]["Roles"] == ["journey-builder-api"]
    )
    send_resources = {
        statement["Resource"]["Fn::GetAtt"][0]
        for statement in backend_policy["Properties"]["PolicyDocument"]["Statement"]
        if isinstance(statement["Action"], list)
        and "sqs:SendMessage" in statement["Action"]
    }
    assert any(name.startswith("ConnectorDispatchQueue") for name in send_resources)
    assert any(
        name.startswith("ConnectorFastOperationQueue") for name in send_resources
    )

    execution_policy = next(
        resource
        for logical_id, resource in resources.items()
        if logical_id.startswith("ConnectorFastWorkerExecutionRoleDefaultPolicy")
    )
    execution_actions = {
        action
        for statement in execution_policy["Properties"]["PolicyDocument"]["Statement"]
        for action in (
            statement["Action"]
            if isinstance(statement["Action"], list)
            else [statement["Action"]]
        )
    }
    assert {
        "ecr:GetAuthorizationToken",
        "ecr:BatchCheckLayerAvailability",
        "ecr:BatchGetImage",
        "ecr:GetDownloadUrlForLayer",
        "logs:CreateLogStream",
        "logs:PutLogEvents",
    } <= execution_actions


def test_fast_worker_alarms_and_outputs_are_present():
    app = core.App()
    stack = ConnectorRuntimeStack(
        app,
        "ConnectorRuntimeFastObservabilityTest",
        project_config=_config(),
        env=core.Environment(account="111111111111", region="ap-southeast-1"),
    )
    template = assertions.Template.from_stack(stack).to_json()
    metrics = {
        resource["Properties"]["MetricName"]
        for resource in template["Resources"].values()
        if resource["Type"] == "AWS::CloudWatch::Alarm"
    }
    assert "ApproximateAgeOfOldestMessage" in metrics
    assert "ApproximateNumberOfMessagesVisible" in metrics
    assert {
        "ConnectorFastOperationQueueUrl",
        "ConnectorFastOperationQueueArn",
        "ConnectorFastOperationDlqArn",
        "ConnectorFastWorkerServiceName",
        "ConnectorFastWorkerServiceArn",
        "ConnectorFastWorkerTaskDefinitionArn",
        "ConnectorFastWorkerLogGroupName",
        "ConnectorFastWorkerLogGroupArn",
    } <= set(template["Outputs"])
    task = next(
        resource
        for resource in template["Resources"].values()
        if resource["Type"] == "AWS::ECS::TaskDefinition"
    )
    container = task["Properties"]["ContainerDefinitions"][0]
    environment = {item["Name"]: item["Value"] for item in container["Environment"]}
    assert container["StopTimeout"] == 110
    assert environment["FAST_OPERATION_SHUTDOWN_GRACE_SECONDS"] == "110"


def test_dispatcher_limits_sqs_source_without_reserving_account_concurrency():
    app = core.App()
    config = _config()
    config["dispatcher_concurrency"] = 5
    stack = ConnectorRuntimeStack(
        app,
        "ConnectorRuntimeDispatcherConcurrencyTest",
        project_config=config,
        env=core.Environment(account="111111111111", region="ap-southeast-1"),
    )
    template = assertions.Template.from_stack(stack).to_json()
    dispatcher = next(
        resource
        for resource in template["Resources"].values()
        if resource["Type"] == "AWS::Lambda::Function"
    )
    assert "ReservedConcurrentExecutions" not in dispatcher["Properties"]
    event_source = next(
        resource
        for resource in template["Resources"].values()
        if resource["Type"] == "AWS::Lambda::EventSourceMapping"
    )
    assert event_source["Properties"]["ScalingConfig"] == {"MaximumConcurrency": 5}


def test_kms_key_policy_allows_regional_cloudwatch_logs():
    app = core.App()
    stack = ConnectorRuntimeStack(
        app,
        "ConnectorRuntimeKmsPolicyTest",
        project_config=_config(),
        env=core.Environment(account="111111111111", region="ap-southeast-1"),
    )
    template = assertions.Template.from_stack(stack)
    template.has_resource_properties(
        "AWS::KMS::Key",
        assertions.Match.object_like(
            {
                "KeyPolicy": {
                    "Statement": assertions.Match.array_with(
                        [
                            assertions.Match.object_like(
                                {
                                    "Sid": "AllowRegionalCloudWatchLogsEncryption",
                                    "Principal": {
                                        "Service": assertions.Match.any_value()
                                    },
                                    "Action": [
                                        "kms:Encrypt*",
                                        "kms:Decrypt*",
                                        "kms:ReEncrypt*",
                                        "kms:GenerateDataKey*",
                                        "kms:Describe*",
                                    ],
                                    "Condition": {
                                        "ArnLike": {
                                            "kms:EncryptionContext:aws:logs:arn": (
                                                assertions.Match.any_value()
                                            )
                                        }
                                    },
                                }
                            )
                        ]
                    )
                }
            }
        ),
    )
    rendered = str(template.to_json())
    assert "logs." in rendered
    assert "AWS::Region" in rendered
    assert "AWS::URLSuffix" in rendered


def test_state_machine_logging_destination_contains_log_group_arn():
    app = core.App()
    stack = ConnectorRuntimeStack(
        app,
        "ConnectorRuntimeLoggingTest",
        project_config=_config(),
        env=core.Environment(account="111111111111", region="ap-southeast-1"),
    )
    template = assertions.Template.from_stack(stack)
    template.has_resource_properties(
        "AWS::StepFunctions::StateMachine",
        assertions.Match.object_like(
            {
                "LoggingConfiguration": {
                    "Destinations": [
                        {
                            "CloudWatchLogsLogGroup": {
                                "LogGroupArn": assertions.Match.any_value()
                            }
                        }
                    ],
                    "IncludeExecutionData": False,
                    "Level": "ERROR",
                }
            }
        ),
    )
    rendered = template.to_json()
    state_machine = next(
        resource
        for resource in rendered["Resources"].values()
        if resource["Type"] == "AWS::StepFunctions::StateMachine"
    )
    log_group_arn = state_machine["Properties"]["LoggingConfiguration"][
        "Destinations"
    ][0]["CloudWatchLogsLogGroup"]["LogGroupArn"]
    assert "Fn::GetAtt" in log_group_arn
    assert "Fn::Join" not in log_group_arn


def test_state_machine_waits_for_its_iam_policy():
    app = core.App()
    stack = ConnectorRuntimeStack(
        app,
        "ConnectorRuntimeIamOrderingTest",
        project_config=_config(),
        env=core.Environment(account="111111111111", region="ap-southeast-1"),
    )
    resources = assertions.Template.from_stack(stack).to_json()["Resources"]
    state_machine = next(
        resource
        for resource in resources.values()
        if resource["Type"] == "AWS::StepFunctions::StateMachine"
    )
    state_machine_policy_id = next(
        logical_id
        for logical_id, resource in resources.items()
        if resource["Type"] == "AWS::IAM::Policy"
        and logical_id.startswith("ConnectorStateMachineRoleDefaultPolicy")
    )
    assert state_machine_policy_id in state_machine["DependsOn"]


def test_connector_runtime_requires_import_references():
    app = core.App()
    with pytest.raises(ValueError, match="connector runtime config missing"):
        ConnectorRuntimeStack(app, "InvalidConnectorRuntime", project_config={})


def test_connector_runtime_requires_immutable_fast_worker_image():
    app = core.App()
    config = _config()
    config["ecs"]["fast_worker_image_uri"] = (
        "111111111111.dkr.ecr.ap-southeast-1.amazonaws.com/connector-worker:latest"
    )
    with pytest.raises(ValueError, match="ECR image URI pinned"):
        ConnectorRuntimeStack(
            app, "MutableConnectorImage", project_config=config
        )


def test_connector_runtime_requires_two_heartbeat_intervals_per_visibility():
    app = core.App()
    config = _config()
    config["fast_queue_visibility_timeout_seconds"] = 300
    config["fast_worker_heartbeat_seconds"] = 151
    with pytest.raises(ValueError, match="no more than half"):
        ConnectorRuntimeStack(
            app, "UnsafeConnectorHeartbeat", project_config=config
        )


@pytest.mark.parametrize(
    "image_uri, error",
    [
        (
            "ghcr.io/example/connector-worker@sha256:" + "a" * 64,
            "ECR image URI pinned",
        ),
        (
            "111111111111.dkr.ecr.us-east-1.amazonaws.com/connector-worker@sha256:"
            + "a" * 64,
            "stack account and region",
        ),
    ],
)
def test_connector_runtime_rejects_unsupported_fast_worker_image(image_uri, error):
    app = core.App()
    config = _config()
    config["ecs"]["fast_worker_image_uri"] = image_uri
    with pytest.raises(ValueError, match=error):
        ConnectorRuntimeStack(
            app,
            "UnsupportedConnectorImage",
            project_config=config,
            env=core.Environment(account="111111111111", region="ap-southeast-1"),
        )
