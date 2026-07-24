#!/usr/bin/env python3
import os
import yaml
import aws_cdk as cdk
from cdk_stacks.lambda_stack import LambdaStack
from cdk_stacks.apigw_stack import ApiGwStack
from cdk_stacks.glue_stack import GlueJobStack

# Load YAML configuration (defaults to "config.yaml", can be overridden by CONFIG_FILE env variable)
config_file = os.environ.get('CONFIG_FILE', 'env.yaml')
with open(config_file, 'r') as stream:
    config = yaml.safe_load(stream)

# Validate the configuration
assert 'aws_region' in config
assert 'aws_account_id' in config

# Support both a single lambda_stack (dict) and multiple lambda_stacks (list of dicts)
_raw_lambda = config.get('lambda_stacks') or config.get('lambda_stack')
if _raw_lambda is None:
    raise AssertionError("env.yaml must define 'lambda_stack' or 'lambda_stacks'")
lambda_stack_configs = _raw_lambda if isinstance(_raw_lambda, list) else [_raw_lambda]
for _ls in lambda_stack_configs:
    assert 'stack_name' in _ls, f"Each lambda stack entry must have a 'stack_name': {_ls}"

if config.get('api_gtw_stack'):
    assert 'stack_name' in config['api_gtw_stack']
if config.get('glue_stack'):
    assert 'stack_name' in config['glue_stack']

# Set up the CDK environment using environment variables or other means
ENV = cdk.Environment(
    region=config['aws_region'],
    account=config['aws_account_id']
)

app = cdk.App()

tags = {
    'project': config.get('project_tag', 'EI-CarbonWatch'),
    'environment': config.get('environment_tag', 'dev')
}

# Deploy one Lambda stack per entry (supports both single dict and list of dicts)
for lambda_stack_conf in lambda_stack_configs:
    LambdaStack(
        app,
        lambda_stack_conf['stack_name'],
        project_config=lambda_stack_conf['project_config'],
        env=ENV,
        tags=tags,
    )

if config.get('api_gtw_stack'):
    ApiGwStack(
        app,
        config['api_gtw_stack']['stack_name'],
        project_config=config['api_gtw_stack']['project_config'],
        env=ENV,
        tags=tags,
    )

if config.get('glue_stack'):
    GlueJobStack(
        app,
        config['glue_stack']['stack_name'],
        project_config=config['glue_stack']['project_config'],
        env=ENV,
        tags=tags,
    )

app.synth()
