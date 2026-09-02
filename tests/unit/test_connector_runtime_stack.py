import aws_cdk as core
import aws_cdk.assertions as assertions
import pytest

from cdk_stacks.connector_runtime_stack import ConnectorRuntimeStack


def _config():
    account = "111111111111"
    region = "ap-southeast-1"
    return {
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


def test_connector_runtime_synthesizes_without_creating_fargate_resources():
    app = core.App()
    stack = ConnectorRuntimeStack(
        app,
        "ConnectorRuntimeTest",
        project_config=_config(),
        env=core.Environment(account="111111111111", region="ap-southeast-1"),
    )
    template = assertions.Template.from_stack(stack)
    template.resource_count_is("AWS::SQS::Queue", 6)
    template.resource_count_is("AWS::KMS::Key", 1)
    template.resource_count_is("AWS::Scheduler::ScheduleGroup", 1)
    template.resource_count_is("AWS::StepFunctions::StateMachine", 1)
    template.resource_count_is("AWS::Lambda::Function", 1)
    template.resource_count_is("AWS::ECS::Service", 0)
    template.resource_count_is("AWS::ECS::TaskDefinition", 0)
    template.resource_count_is("AWS::ECR::Repository", 0)


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
