import aws_cdk as core
import aws_cdk.assertions as assertions
import pytest

from cdk_stacks.standardization_runtime_stack import StandardizationRuntimeStack


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
            "container_name": "elasticsearch-standardization-worker",
            "image_uri": (
                f"{account}.dkr.ecr.{region}.amazonaws.com/"
                "elasticsearch-standardization-worker:test"
            ),
            "source_database_secret_arn": (
                f"arn:aws:secretsmanager:{region}:{account}:secret:source-AbCdEf"
            ),
            "warehouse_database_secret_arn": (
                f"arn:aws:secretsmanager:{region}:{account}:secret:warehouse-AbCdEf"
            ),
        },
        "backend_principal_arn": f"arn:aws:iam::{account}:role/journey-builder-api",
        "dispatcher_concurrency": 5,
    }


def test_standardization_runtime_is_isolated_and_uses_imported_task():
    app = core.App()
    stack = StandardizationRuntimeStack(
        app,
        "StandardizationRuntimeTest",
        project_config=_config(),
        env=core.Environment(account="111111111111", region="ap-southeast-1"),
    )
    template = assertions.Template.from_stack(stack)
    template.resource_count_is("AWS::SQS::Queue", 4)
    # Dispatcher plus CDK's log-retention custom-resource provider.
    template.resource_count_is("AWS::Lambda::Function", 2)
    template.resource_count_is("AWS::ECS::TaskDefinition", 1)
    template.resource_count_is("AWS::ECS::Service", 0)
    rendered = str(template.to_json())
    assert "ELASTICSEARCH_STANDARDIZE" not in rendered
    assert "RESULT_QUEUE_URL" in rendered
    assert "elasticsearch-standardization-worker:test" in rendered


def test_standardization_dispatcher_caps_parallel_ecs_tasks():
    app = core.App()
    stack = StandardizationRuntimeStack(
        app,
        "StandardizationConcurrencyTest",
        project_config=_config(),
        env=core.Environment(account="111111111111", region="ap-southeast-1"),
    )
    resources = assertions.Template.from_stack(stack).to_json()["Resources"]
    event_source = next(
        resource
        for resource in resources.values()
        if resource["Type"] == "AWS::Lambda::EventSourceMapping"
    )
    assert event_source["Properties"]["BatchSize"] == 1
    assert event_source["Properties"]["ScalingConfig"] == {"MaximumConcurrency": 5}


def test_standardization_runtime_requires_separate_ecs_contract():
    app = core.App()
    with pytest.raises(ValueError, match="standardization runtime config missing"):
        StandardizationRuntimeStack(app, "InvalidStandardization", project_config={})
