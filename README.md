# Deploying AWS CDK Stack with YAML Configuration

This guide explains how to deploy an AWS CDK stack dynamically using a YAML configuration file. This setup allows for deploying various AWS services flexibly across different projects.

## Prerequisites

Ensure you have the following installed:

- [AWS CLI](https://aws.amazon.com/cli/)
- [Node.js](https://nodejs.org/)
- [AWS CDK](https://docs.aws.amazon.com/cdk/latest/guide/getting_started.html)
- Python (for Lambda functions, if applicable)
- An AWS account with necessary permissions

## Project Structure

```plaintext
/project-root
│── cdk_stacks/             # CDK stack definitions
│── _lambda/                # Lambda function source code (if applicable)
│── app.py                  # CDK entry point
│── env.yaml                # Environment configuration file
│── requirements.txt        # Python dependencies (if applicable)
│── README.md               # This file
```

## Configuring the Environment

The `env.yaml` file contains configuration details, such as AWS account settings, project tags, and specific stack configurations. Below is an example structure:

```yaml
# Global AWS settings
aws_account_id: "<AWS_ACCOUNT_ID>"           # Your AWS account ID.
aws_region: "<AWS_REGION>"              # AWS region for deployment.
project_tag: "EI-<PROJECT_NAME>"             # Project tag for resource tagging.
environment_tag: "dev"                    # Environment tag (e.g., dev, prod).

###########################
# Lambda Stack Settings
###########################
lambda_stack:
  stack_name: "DynamicLambdaStack"        # Name of the CloudFormation stack for Lambda.
  project_config:
    vpc_id: "<VPC_ID>"         # VPC ID for deploying the Lambda function.
    bucket_layer_name: "lambda-layer-ap-southeast-1-771555507582"  # S3 bucket name containing Lambda layers.
    lambda_function_name: "<LAMBDA_NAME>"   # Name of the Lambda function.
    code_from_asset: "_lambda"              # Directory containing the Lambda function code.
    function_handler: "lambda_function.lambda_handler"  # Lambda handler (file.function).
    memory_size: 256                        # Memory (MB) allocated to the Lambda function.
    ephemeral_storage_size: 1024            # Ephemeral storage size (MB) for the Lambda.
    environments:
      ENV_VAR: "value"                      # Environment variables for the Lambda function.
    layers:                                 # List of Lambda layers (from S3).
      - id: "Geoalchemy2Layer"              
        key: "geoalchemy2.zip"              
      - id: "GeopandasLayer"                
        key: "geopandas.zip"                
      - id: "Psycopg2Layer"                 
        key: "psycopg2-binary.zip"          
      - id: "PydanticV2Layer"               
        key: "pydantic-v2.zip"              
    role:                                   # IAM Role configuration for the Lambda.
      assumed_by: "lambda.amazonaws.com"    # Principal that can assume this role.
      role_name: "IamRoleLambdaDynamic"     # Name of the IAM role.
      ids:                                  # Identifiers for the managed policies.
        - "PolicyBasic"
        - "PolicyS3"
        - "PolicyNetwork"
        - "PolicySecret"
        - "PolicySagemaker"
        - "PolicySES"
      managed_policy_arns:                  # ARNs for the managed policies to attach.
        - "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
        - "arn:aws:iam::aws:policy/AmazonS3FullAccess"
        - "arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"
        - "arn:aws:iam::aws:policy/SecretsManagerReadWrite"
        - "arn:aws:iam::aws:policy/AmazonSageMakerFullAccess"
        - "arn:aws:iam::aws:policy/AmazonSESFullAccess"
    security_group:                         # Security group settings for the Lambda.
      name: "LambdaSecurityGroupDynamicV2"    # Name of the security group.
      description: "Custom SG for Lambda functions"  # Description.
      allow_all_outbound: true                # Allow all outbound traffic.
      ingress_rules:                          # List of ingress (inbound) rules.
        - peer: "any_ipv4"                    # Allow traffic from any IPv4 address.
          port: 5432                          # Port number (e.g., for database connections).
          description: "Allow database connections"
        - peer: "any_ipv4"
          port: 80
          description: "Allow HTTP access"

###########################
# API Gateway Stack Settings
###########################
api_gtw_stack:
  stack_name: "DynamicApiGatewayStack"      # Name of the CloudFormation stack for API Gateway.
  project_config:
    # Basic API Gateway settings
    lambda_function_arn: "arn:aws:lambda:ap-southeast-1:771555507582:function:test-dynamic-lambda"  # ARN of the backend Lambda.
    stage_name: "prod"                      # Deployment stage name (prod, dev, etc.).
    rest_api_name: "DynamicRestAPI"         # Name of the REST API.
    rest_api_description: "This API is managed by CDK and YAML configuration."  # Description.
    # Stage-level throttling and caching settings (apply to the entire API stage)
    throttling_rate_limit: 100              # Global throttling rate limit.
    throttling_burst_limit: 200             # Global throttling burst limit.
    caching_enabled: false                  # Whether caching is enabled on the API stage.
    # API resource endpoints and HTTP methods to create
    resources_methods:
      - [ "v1/bird", "GET" ]
      - [ "v1/dog", "POST" ]
      - [ "v1/fish", "PUT" ]
      - [ "v1/cat", "DELETE" ]
    # Grouped API Key Configuration (only used if API key usage is enabled)
    api_key_config:
      enable_api_key: true                  # Set to true to enforce API key usage; false to disable.
      # If enabling API keys, you can either import an existing key by uncommenting the next line:
      # api_key_id: "existing-api-key-id"
      # Or create a new API key using the following settings:
      api_key_name: "DynamicAPIKey"         # Name of the API key (if created new).
      api_key_value: null                   # API key value (null to auto-generate, or provide a custom value between 20-40 characters).
      usage_plan:                           # Usage plan settings associated with the API key.
        usage_plan_name: "DynamicUsagePlan"     # Name of the usage plan.
        usage_plan_description: "Usage plan for the Dynamic API"  # Description.
        plan_rate_limit: 50                 # Rate limit for the usage plan.
        plan_burst_limit: 100               # Burst limit for the usage plan.
        quota_limit: 10000                  # Quota limit for the usage plan.
        quota_period: "WEEK"                # Quota period (e.g., WEEK, DAY).
```

Update this file with your specific AWS settings before deployment.

## Installing Dependencies

Run the following command to install dependencies:

```sh
pip install -r requirements.txt
```

## Bootstrapping AWS CDK (If Needed)

If this is your first time using AWS CDK in your AWS account, you need to bootstrap it:

```sh
cdk bootstrap aws://<aws_account_id>/<aws_region>
```

## Deploying the CDK Stack

To deploy the stack, run:

```sh
cdk deploy --context env=env.yaml
```

This command reads the configuration from `env.yaml` and deploys the stack dynamically.

## Destroying the Stack

If you need to remove the stack, run:

```sh
cdk destroy
```

## Next Steps

This project is designed to be modular, allowing dynamic deployment of various AWS services. The next step involves adding additional stacks, such as API Gateway, DynamoDB, or other AWS services, for more complex deployments.

## Troubleshooting

- Ensure AWS credentials are configured properly using `aws configure`.
- Verify the CDK version with `cdk --version`.
- If a stack fails, check CloudFormation logs for details.

For more details, refer to the [AWS CDK documentation](https://docs.aws.amazon.com/cdk/latest/guide/home.html).

