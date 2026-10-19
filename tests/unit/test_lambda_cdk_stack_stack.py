"""Basic tests for the Lambda CDK stack."""

import aws_cdk as core
import aws_cdk.assertions as assertions
from aws_cdk import aws_ec2 as ec2
import pytest

from cdk_stacks.lambda_stack import LambdaStack


def _mock_vpc_from_lookup(scope, construct_id, vpc_id):
    return ec2.Vpc.from_vpc_attributes(
        scope,
        f"{construct_id}Imported",
        vpc_id=vpc_id,
        availability_zones=["ap-southeast-1a"],
        private_subnet_ids=["subnet-12345678"],
        private_subnet_route_table_ids=["rtb-12345678"],
    )


def test_lambda_stack_synthesizes():
    """Ensure the LambdaStack can be synthesized with minimal config."""
    app = core.App()
    stack = LambdaStack(
        app,
        "test-stack",
        project_config={
            "lambda_function_name": "test_fn",
            "code_from_asset": "_lambda",
            "function_handler": "lambda_function.lambda_handler",
        },
    )

    template = assertions.Template.from_stack(stack)

    template.has_resource_properties(
        "AWS::Lambda::Function",
        {
            "Handler": "lambda_function.lambda_handler",
            "Runtime": "python3.9",
        },
    )


def test_lambda_stack_allows_configured_python_version():
    """Ensure Lambda runtime can be configured via python_version."""
    app = core.App()
    stack = LambdaStack(
        app,
        "test-stack-runtime",
        project_config={
            "lambda_function_name": "test_fn",
            "code_from_asset": "_lambda",
            "function_handler": "lambda_function.lambda_handler",
            "python_version": "3.10",
        },
    )

    template = assertions.Template.from_stack(stack)
    template.has_resource_properties(
        "AWS::Lambda::Function",
        {
            "Runtime": "python3.10",
        },
    )


def test_lambda_stack_allows_configured_timeout():
    """Ensure Lambda timeout can be configured."""
    app = core.App()
    stack = LambdaStack(
        app,
        "test-stack-timeout",
        project_config={
            "lambda_function_name": "test_fn",
            "code_from_asset": "_lambda",
            "function_handler": "lambda_function.lambda_handler",
            "timeout": 300,
        },
    )

    template = assertions.Template.from_stack(stack)
    template.has_resource_properties(
        "AWS::Lambda::Function",
        {
            "Timeout": 300,
        },
    )


def test_lambda_stack_allows_reserved_concurrency():
    app = core.App()
    stack = LambdaStack(
        app,
        "test-stack-reserved-concurrency",
        project_config={
            "lambda_function_name": "test_fn",
            "code_from_asset": "_lambda",
            "function_handler": "lambda_function.lambda_handler",
            "reserved_concurrent_executions": 1,
        },
    )

    template = assertions.Template.from_stack(stack)
    template.has_resource_properties(
        "AWS::Lambda::Function",
        {
            "ReservedConcurrentExecutions": 1,
        },
    )


def test_lambda_stack_raises_on_unsupported_python_version():
    """Ensure unsupported python_version values fail with a clear error."""
    app = core.App()

    with pytest.raises(ValueError, match="Unsupported lambda python_version"):
        LambdaStack(
            app,
            "test-stack-runtime-invalid",
            project_config={
                "lambda_function_name": "test_fn",
                "code_from_asset": "_lambda",
                "function_handler": "lambda_function.lambda_handler",
                "python_version": "3.12",
            },
        )


def test_lambda_stack_layers_allow_key_only_entries():
    """Ensure Lambda layers can be configured with only a key."""
    app = core.App()
    stack = LambdaStack(
        app,
        "test-stack-key-only-layers",
        project_config={
            "lambda_function_name": "test_fn",
            "code_from_asset": "_lambda",
            "function_handler": "lambda_function.lambda_handler",
            "bucket_layer_name": "layer-bucket",
            "layers": [
                {"key": "geoalchemy2.zip"},
                {"key": "geopandas.zip"},
            ],
        },
    )

    template = assertions.Template.from_stack(stack)
    template.resource_count_is("AWS::Lambda::LayerVersion", 2)


def test_lambda_stack_layers_raise_when_key_missing():
    """Ensure invalid layer entries fail fast when key is not provided."""
    app = core.App()

    with pytest.raises(ValueError, match="must include a non-empty \"key\""):
        LambdaStack(
            app,
            "test-stack-invalid-layer",
            project_config={
                "lambda_function_name": "test_fn",
                "code_from_asset": "_lambda",
                "function_handler": "lambda_function.lambda_handler",
                "bucket_layer_name": "layer-bucket",
                "layers": [{"id": "OnlyIdNoKey"}],
            },
        )


def test_lambda_stack_role_allows_managed_policies_without_ids():
    """Ensure role policy IDs are generated automatically when ids are omitted."""
    app = core.App()
    stack = LambdaStack(
        app,
        "test-stack-role-no-ids",
        project_config={
            "lambda_function_name": "test_fn",
            "code_from_asset": "_lambda",
            "function_handler": "lambda_function.lambda_handler",
            "role": {
                "managed_policy_arns": [
                    "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
                    "arn:aws:iam::aws:policy/AmazonS3FullAccess",
                ]
            },
        },
    )

    template = assertions.Template.from_stack(stack)
    template.has_resource_properties(
        "AWS::IAM::Role",
        {
            "ManagedPolicyArns": assertions.Match.array_with([
                "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
                "arn:aws:iam::aws:policy/AmazonS3FullAccess",
            ])
        },
    )


def test_lambda_stack_role_raises_on_mismatched_ids_length():
    """Ensure explicit ids still require the same length as managed_policy_arns."""
    app = core.App()

    with pytest.raises(ValueError, match="must have the same length"):
        LambdaStack(
            app,
            "test-stack-role-id-mismatch",
            project_config={
                "lambda_function_name": "test_fn",
                "code_from_asset": "_lambda",
                "function_handler": "lambda_function.lambda_handler",
                "role": {
                    "ids": ["OnlyOneId"],
                    "managed_policy_arns": [
                        "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole",
                        "arn:aws:iam::aws:policy/AmazonS3FullAccess",
                    ],
                },
            },
        )


def test_lambda_stack_uses_existing_security_groups(monkeypatch):
    """Ensure existing security groups can be imported instead of creating a new one."""
    monkeypatch.setattr(ec2.Vpc, "from_lookup", _mock_vpc_from_lookup)

    app = core.App()
    stack = LambdaStack(
        app,
        "test-stack-existing-security-groups",
        project_config={
            "vpc_id": "vpc-12345678",
            "lambda_function_name": "test_fn",
            "code_from_asset": "_lambda",
            "function_handler": "lambda_function.lambda_handler",
            "security_group": {
                "existing_ids": ["sg-0123456789abcdef0", "sg-0fedcba9876543210"],
            },
        },
    )

    template = assertions.Template.from_stack(stack)
    template.resource_count_is("AWS::EC2::SecurityGroup", 0)
    template.has_resource_properties(
        "AWS::Lambda::Function",
        {
            "VpcConfig": {
                "SecurityGroupIds": [
                    "sg-0123456789abcdef0",
                    "sg-0fedcba9876543210",
                ]
            }
        },
    )


def test_lambda_stack_existing_security_groups_reject_create_fields(monkeypatch):
    """Ensure imported security groups cannot be mixed with create-only settings."""
    monkeypatch.setattr(ec2.Vpc, "from_lookup", _mock_vpc_from_lookup)

    app = core.App()

    with pytest.raises(ValueError, match="existing_ids cannot be combined"):
        LambdaStack(
            app,
            "test-stack-existing-security-groups-invalid",
            project_config={
                "vpc_id": "vpc-12345678",
                "lambda_function_name": "test_fn",
                "code_from_asset": "_lambda",
                "function_handler": "lambda_function.lambda_handler",
                "security_group": {
                    "existing_ids": ["sg-0123456789abcdef0"],
                    "ingress_rules": [{"peer": "any_ipv4", "port": 80}],
                },
            },
        )


def test_lambda_stack_adds_ecs_stopped_event_rule():
    app = core.App()
    stack = LambdaStack(
        app,
        "test-stack-ecs-events",
        project_config={
            "lambda_function_name": "test_fn",
            "code_from_asset": "_lambda",
            "function_handler": "lambda_function.lambda_handler",
            "ecs_task_state_rule": {
                "cluster_arn": "arn:aws:ecs:region:account:cluster/test",
                "task_family": "stitcher",
            },
        },
    )

    template = assertions.Template.from_stack(stack)
    template.has_resource_properties(
        "AWS::Events::Rule",
        {
            "EventPattern": {
                "source": ["aws.ecs"],
                "detail-type": ["ECS Task State Change"],
                "detail": {
                    "clusterArn": [
                        "arn:aws:ecs:region:account:cluster/test"
                    ],
                    "group": ["family:stitcher"],
                    "lastStatus": ["STOPPED"],
                },
            },
            "State": "ENABLED",
        },
    )
