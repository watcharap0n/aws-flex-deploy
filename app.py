#!/usr/bin/env python3
import os
from pathlib import Path
import yaml
import aws_cdk as cdk
from cdk_stacks.lambda_stack import LambdaStack
from cdk_stacks.apigw_stack import ApiGwStack
from cdk_stacks.glue_stack import GlueJobStack
from cdk_stacks.connector_runtime_stack import ConnectorRuntimeStack

# Load YAML configuration (defaults to "config.yaml", can be overridden by CONFIG_FILE env variable)
config_file = os.environ.get('CONFIG_FILE', 'env.yaml')
with open(config_file, 'r') as stream:
    config = yaml.safe_load(stream)

# Validate the configuration
assert 'aws_region' in config
assert 'aws_account_id' in config

# Support both a single lambda_stack (dict) and multiple lambda_stacks (list of dicts)
_raw_lambda = config.get('lambda_stacks') or config.get('lambda_stack')
if (
    _raw_lambda is None
    and not config.get('connector_runtime_stack')
    and not config.get('standardization_runtime_stack')
    and not config.get('customer_ingestion_runtime_stack')
):
    raise AssertionError(
        "env.yaml must define lambda_stack(s) or connector_runtime_stack"
    )
lambda_stack_configs = (
    _raw_lambda if isinstance(_raw_lambda, list) else [_raw_lambda]
) if _raw_lambda is not None else []
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

if config.get('connector_runtime_stack'):
    connector_stack_config = config['connector_runtime_stack']
    if not connector_stack_config.get('stack_name'):
        raise AssertionError("connector_runtime_stack requires stack_name")
    ingestion_config = next((entry['project_config'] for entry in lambda_stack_configs
        if Path(entry['project_config'].get('code_from_asset', '')).resolve() == Path('_lambda/ingestion_data').resolve()), None)
    connector_project_config = dict(connector_stack_config['project_config'])
    if ingestion_config:
        connector_project_config['stitching_admission_function_arn'] = (
            f"arn:aws:lambda:{config['aws_region']}:{config['aws_account_id']}:"
            f"function:{ingestion_config['lambda_function_name']}"
        )
    ConnectorRuntimeStack(
        app,
        connector_stack_config['stack_name'],
        project_config=connector_project_config,
        env=ENV,
        tags=tags,
    )

if config.get('standardization_runtime_stack'):
    # Keep connector-only synths independent from the optional standardization stack.
    from cdk_stacks.standardization_runtime_stack import StandardizationRuntimeStack

    standardization_stack_config = config['standardization_runtime_stack']
    if not standardization_stack_config.get('stack_name'):
        raise AssertionError("standardization_runtime_stack requires stack_name")
    StandardizationRuntimeStack(
        app,
        standardization_stack_config['stack_name'],
        project_config=standardization_stack_config['project_config'],
        env=ENV,
        tags=tags,
    )

if config.get('customer_ingestion_runtime_stack'):
    from cdk_stacks.customer_ingestion_runtime_stack import CustomerIngestionRuntimeStack
    runtime_config = config['customer_ingestion_runtime_stack']
    CustomerIngestionRuntimeStack(
        app, runtime_config['stack_name'],
        project_config=runtime_config['project_config'], env=ENV, tags=tags,
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
