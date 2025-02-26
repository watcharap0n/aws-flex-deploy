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
aws_account_id: "771555507582"
aws_region: "ap-southeast-1"
project_tag: "EI-CarbonWatch"
environment_tag: "dev"

lambda_stack:
  stack_name: "DynamicLambdaStack"
  project_config:
    vpc_id: "vpc-0b0c789397b306f2c"
    bucket_layer_name: "lambda-layer-ap-southeast-1-771555507582"
    lambda_function_name: "test-dynamic-lambda"
    code_from_asset: "_lambda"
    function_handler: "lambda_function.lambda_handler"
    memory_size: 256
    ephemeral_storage_size: 1024
    environments:
      ENV_VAR: "value"
    layers:
      - id: "Geoalchemy2Layer"
        key: "geoalchemy2.zip"
      - id: "GeopandasLayer"
        key: "geopandas.zip"
      - id: "Psycopg2Layer"
        key: "psycopg2-binary.zip"
      - id: "PydanticV2Layer"
        key: "pydantic-v2.zip"
    role:
      assumed_by: "lambda.amazonaws.com"
      role_name: "IamRoleLambdaGis"
      ids:
        - "PolicyBasic"
        - "PolicyS3"
        - "PolicyNetwork"
        - "PolicySecret"
        - "PolicySagemaker"
        - "PolicySES"
      managed_policy_arns:
        - "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
        - "arn:aws:iam::aws:policy/AmazonS3FullAccess"
        - "arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"
        - "arn:aws:iam::aws:policy/SecretsManagerReadWrite"
        - "arn:aws:iam::aws:policy/AmazonSageMakerFullAccess"
        - "arn:aws:iam::aws:policy/AmazonSESFullAccess"
    security_group:
      name: "LambdaSecurityGroupDynamic"
      description: "Custom SG for Lambda functions"
      allow_all_outbound: true
      ingress_rules:
        - peer: "any_ipv4"
          port: 5432
          description: "Allow database connections"
        - peer: "any_ipv4"
          port: 80
          description: "Allow HTTP access"
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

