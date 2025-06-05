"""Basic tests for the Lambda CDK stack."""

import aws_cdk as core
import aws_cdk.assertions as assertions

from cdk_stacks.lambda_stack import LambdaStack


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
        {"Handler": "lambda_function.lambda_handler"},
    )
