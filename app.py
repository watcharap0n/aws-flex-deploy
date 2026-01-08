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
assert 'lambda_stack' in config
assert 'stack_name' in config['lambda_stack']
if config.get('api_gtw_stack'):
    assert 'api_gtw_stack' in config
    assert 'stack_name' in config['api_gtw_stack']
if config.get('glue_stack'):
    assert 'glue_stack' in config
    assert 'stack_name' in config['glue_stack']

# Set up the CDK environment using environment variables or other means
ENV = cdk.Environment(
    region=config['aws_region'],
    account=config['aws_account_id']
)

app = cdk.App()

# Create the stack, passing the configuration from the YAML file
LambdaStack(
    app,
    config['lambda_stack']['stack_name'],
    project_config=config['lambda_stack']['project_config'],
    env=ENV,
    tags={
        'project': config.get('project_tag', 'EI-CarbonWatch'),
        'environment': config.get('environment_tag', 'dev')
    }
)

if config.get('api_gtw_stack'):
    ApiGwStack(
        app,
        config['api_gtw_stack']['stack_name'],
        project_config=config['api_gtw_stack']['project_config'],
        env=ENV,
        tags={
            'project': config.get('project_tag', 'EI-CarbonWatch'),
            'environment': config.get('environment_tag', 'dev')
        }
    )

if config.get('glue_stack'):
    GlueJobStack(
        app,
        config['glue_stack']['stack_name'],
        project_config=config['glue_stack']['project_config'],
        env=ENV,
        tags={
            'project': config.get('project_tag', 'EI-CarbonWatch'),
            'environment': config.get('environment_tag', 'dev')
        }
    )

app.synth()
