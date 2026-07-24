"""Basic tests for the Glue CDK stack."""

import aws_cdk as core
import aws_cdk.assertions as assertions
import pytest

from cdk_stacks.glue_stack import GlueJobStack


def test_glue_stack_role_allows_managed_policies_without_ids():
    """Ensure Glue role policy IDs are generated automatically when ids are omitted."""
    app = core.App()
    stack = GlueJobStack(
        app,
        "test-glue-stack-role-no-ids",
        project_config={
            "job_name": "test-glue-job",
            "command": {
                "name": "glueetl",
                "script_location": "s3://example-bucket/scripts/job.py",
                "python_version": "3",
            },
            "role": {
                "managed_policy_arns": [
                    "arn:aws:iam::aws:policy/service-role/AWSGlueServiceRole",
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
                "arn:aws:iam::aws:policy/service-role/AWSGlueServiceRole",
                "arn:aws:iam::aws:policy/AmazonS3FullAccess",
            ])
        },
    )


def test_glue_stack_role_raises_on_mismatched_ids_length():
    """Ensure explicit ids still require the same length as managed_policy_arns."""
    app = core.App()

    with pytest.raises(ValueError, match="must have the same length"):
        GlueJobStack(
            app,
            "test-glue-stack-role-id-mismatch",
            project_config={
                "job_name": "test-glue-job",
                "command": {
                    "name": "glueetl",
                    "script_location": "s3://example-bucket/scripts/job.py",
                    "python_version": "3",
                },
                "role": {
                    "ids": ["OnlyOneId"],
                    "managed_policy_arns": [
                        "arn:aws:iam::aws:policy/service-role/AWSGlueServiceRole",
                        "arn:aws:iam::aws:policy/AmazonS3FullAccess",
                    ],
                },
            },
        )

