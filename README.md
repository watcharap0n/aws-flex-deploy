# Deploying AWS CDK Stack with YAML Configuration

This guide explains how to deploy AWS CDK stacks dynamically using a YAML configuration file. This setup allows for deploying various AWS services flexibly across different projects.

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
│── dependencies/           # Glue job scripts or shared Python code (optional)
│── app.py                  # CDK entry point
│── env.yaml                # Environment configuration file
│── requirements.txt        # Python dependencies (if applicable)
│── README.md               # This file
```

## Configuring the Environment

The `env.yaml` file contains configuration details, such as AWS account settings, project tags, and specific stack configurations. Below is an example structure:

```yaml
# Global AWS settings
aws_account_id: "771555507582"           # Your AWS account ID.
aws_region: "ap-southeast-1"              # AWS region for deployment.
project_tag: "EI-CarbonWatch"             # Project tag for resource tagging.
environment_tag: "dev"                    # Environment tag (e.g., dev, prod).

###########################
# Lambda Stack Settings
###########################
lambda_stack:
  stack_name: "DynamicLambdaStack"        # Name of the CloudFormation stack for Lambda.
  project_config:
    vpc_id: "vpc-0b0c789397b306f2c"         # VPC ID for deploying the Lambda function.
    bucket_layer_name: "lambda-layer-ap-southeast-1-771555507582"  # S3 bucket name containing Lambda layers.
    lambda_function_name: "test-dynamic-lambda"   # Name of the Lambda function.
    code_from_asset: "_lambda"              # Directory containing the Lambda function code.
    function_handler: "lambda_function.lambda_handler"  # Lambda handler (file.function).
    python_version: "3.10"                  # Optional. Supported: 3.8, 3.9, 3.10, 3.11 (default: 3.9)
    timeout: 30                             # Optional. Lambda timeout in seconds (default: 30, max: 900)
    memory_size: 256                        # Memory (MB) allocated to the Lambda function.
    ephemeral_storage_size: 1024            # Ephemeral storage size (MB) for the Lambda.
    environments:
      ENV_VAR: "value"                      # Environment variables for the Lambda function.
      S3_BUCKET: "my-bucket"                # Example environment variable.
    layers: # List of Lambda layers (from S3). IDs are auto-generated from keys.
      - key: "geoalchemy2.zip"
      - key: "geopandas.zip"
      - key: "psycopg2-binary.zip"
      - key: "pydantic-v2.zip"
    role: # IAM Role configuration for the Lambda.
      assumed_by: "lambda.amazonaws.com"    # Principal that can assume this role.
      role_name: "IamRoleLambdaDynamic"     # Name of the IAM role.
      managed_policy_arns: # ARNs for the managed policies to attach. IDs are auto-generated.
        - "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
        - "arn:aws:iam::aws:policy/AmazonS3FullAccess"
        - "arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"
        - "arn:aws:iam::aws:policy/SecretsManagerReadWrite"
        - "arn:aws:iam::aws:policy/AmazonSageMakerFullAccess"
        - "arn:aws:iam::aws:policy/AmazonSESFullAccess"
    security_group: # Security group settings for the Lambda.
      name: "LambdaSecurityGroupDynamicV2"    # Name of the security group.
      description: "Custom SG for Lambda functions"  # Description.
      allow_all_outbound: true                # Allow all outbound traffic.
      ingress_rules: # List of ingress (inbound) rules.
        - peer: "any_ipv4"                    # Allow traffic from any IPv4 address.
          port: 5432                          # Port number (e.g., for database connections).
          description: "Allow database connections"
        - peer: "any_ipv4"
          port: 80
          description: "Allow HTTP access"
      # Or import existing security groups instead of creating a new one:
      # existing_ids:
      #   - "sg-0123456789abcdef0"
      #   - "sg-0fedcba9876543210"
  # Configuration for S3 trigger and EventBridge permission
  # s3_trigger:
  #   bucket_name: "<S3_BUCKET_ANALYSIS_NAME>"  # Name of the S3 bucket to trigger the Lambda function.
  #   prefix: "output/"                         # Prefix filter for S3 objects to trigger the Lambda function.
  #   suffix: ".shp"                            # Suffix filter for S3 objects to trigger the Lambda function.
  # add_permission_eventbridge: true            # Boolean flag to add permission for EventBridge to invoke the Lambda function.

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
      - path: "collections/{proxy+}"
        method: "ANY"
        integration_type: "http_proxy"
        http_proxy_url: "https://example.com/collections/{proxy}"
    # Grouped API Key Configuration (only used if API key usage is enabled)
    api_key_config:
      enable_api_key: true                  # Set to true to enforce API key usage; false to disable.
      # If enabling API keys, you can either import an existing key by uncommenting the next line:
      # api_key_id: "existing-api-key-id"
      # Or create a new API key using the following settings:
      api_key_name: "DynamicAPIKey"         # Name of the API key (if created new).
      api_key_value: null                   # API key value (null to auto-generate, or provide a custom value between 20-40 characters).
      usage_plan: # Usage plan settings associated with the API key.
        usage_plan_name: "DynamicUsagePlan"     # Name of the usage plan.
        usage_plan_description: "Usage plan for the Dynamic API"  # Description.
        plan_rate_limit: 50                 # Rate limit for the usage plan.
        plan_burst_limit: 100               # Burst limit for the usage plan.
        quota_limit: 10000                  # Quota limit for the usage plan.
        quota_period: "WEEK"                # Quota period (e.g., WEEK, DAY).

###########################
# Glue Job Stack Settings
###########################
glue_stack:
  stack_name: "DynamicGlueJobStack"
  project_config:
    job_name: "dynamic-glue-job"
    description: "Glue job managed by CDK and YAML configuration."
    command:
      name: "glueetl"
      script_location: "s3://my-bucket/glue/scripts/job.py"
      python_version: "3"
    # Or use a local file and let CDK upload it to S3 on deploy:
    # command:
    #   name: "glueetl"
    #   local_script_path: "dependencies/main.py"
    #   python_version: "3"
    glue_version: "4.0"
    worker_type: "G.1X"
    number_of_workers: 2
    max_retries: 1
    timeout: 30
    max_concurrent_runs: 1
    default_arguments:
      --job-language: "python"
      --enable-continuous-cloudwatch-log: "true"
    additional_python_modules:
      - "pandas==1.5.3"
      - "pyarrow==11.0.0"
    extra_py_files:
      - "s3://my-bucket/glue/libs/deps.zip"
    connections:
      create:
        - name: "pgstac-connection"
          connection_type: "JDBC"
          connection_properties:
            JDBC_CONNECTION_URL: "jdbc:postgresql://example.rds.amazonaws.com:5432/db"
            USERNAME: "db_user"
            PASSWORD: "db_password"
          physical_connection_requirements:
            subnet_id: "subnet-0123456789abcdef0"
            security_group_id_list:
              - "sg-0123456789abcdef0"
            availability_zone: "ap-southeast-1a"
      use:
        - "existing-connection-name"
    role:
      assumed_by: "glue.amazonaws.com"
      role_name: "DynamicGlueJobRole"
      managed_policy_arns:
        - "arn:aws:iam::aws:policy/service-role/AWSGlueServiceRole"
        - "arn:aws:iam::aws:policy/AmazonS3FullAccess"
```

Update this file with your specific AWS settings before deployment.

### Using an Existing Lambda Security Group

If your Lambda should reuse an existing security group, set `lambda_stack.project_config.vpc_id` and configure:

```yaml
security_group:
  existing_ids:
    - "sg-0123456789abcdef0"
```

Notes:

- `existing_ids` accepts one or more security group IDs.
- When `existing_ids` is used, do not combine it with `name`, `description`, `allow_all_outbound`, or `ingress_rules`.
- If `existing_ids` is omitted, the stack keeps the current behavior and creates a new security group.
- Set `python_version` in each Lambda `project_config` to choose runtime (supported: `3.8`, `3.9`, `3.10`, `3.11`).

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

### Customer ingestion contract

The customer landing flow accepts exactly one source file per source and batch:

```text
data_sources/<source_code>/<batch_ref>/customers/<file>.(csv|xlsx|json)
```

The only required source column is `source_customer_id`. Optional source
columns are `external_id`, `first_name`, `middle_name`, `last_name`,
`full_name`, `nickname`, `email`, `phone`, `gender`, `date_of_birth`,
`birthday`, `age`, `line_user_id`, `facebook_id`, `google_id`, `apple_id`,
`member_code`, `customer_type`, `status`, `language`, `address_line_1`,
`address_line_2`, `district`, `province`, `postal_code`, `country`, `source`,
`source_system`, `external_ids`, `attributes`, `source_created_at`, and
`source_updated_at`.

`raw_customer_id`, `batch_id`, `source_system_id`, `raw_payload`, `created_at`,
`updated_at`, and `ingested_at` are pipeline-managed and must not be supplied by
the source file. `customer_id` is a nullable legacy database column; it is not
the source identifier and is intentionally not part of the upload contract.

The CRM aliases `StoreCode`, `FirstName`, `LastName`, `ContactNumber`, and
`RegisterDate` remain accepted during the cut-over. The landing Lambda writes:

```text
raw/<source_code>/<landing_batch_id>/customer.csv
```

Apply the database migration before deploying the Lambda stacks:

```sh
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 \
  -f database/migrations/013_single_customer_source.sql
```

Apply pipeline lineage and stitching tracking before deploying the updated
ingestion Lambda or Fargate image:

```sh
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 \
  -f database/migrations/014_pipeline_tracking.sql

psql "$DATABASE_URL" -v ON_ERROR_STOP=1 \
  -f database/queries/verify_pipeline_tracking.sql
```

Then apply the customer data-quality snapshot migration. Migration 015 must be
present before either the updated ingestion Lambda or Fargate image is
deployed:

```sh
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 \
  -f database/migrations/015_customer_data_quality.sql

psql "$DATABASE_URL" -v ON_ERROR_STOP=1 \
  -f database/queries/verify_customer_data_quality.sql
```

Allow different source records to carry the same external identity signal.
`raw.customer` remains unique by `(source_system_id, source_customer_id)`;
identity merging belongs to the downstream stitcher:

```sh
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 \
  -f database/migrations/016_shared_customer_external_ids.sql
```

The quality layer is DB-first and runs between standardization and stitching.
Data findings are informational and do not stop stitching; a technical
validation or snapshot-write failure marks the pipeline `FAILED` with
`error_stage = 'VALIDATION'` and prevents the resolver from starting.

`control.customer_pipeline_status` is the read-only contract for a future API.
Query it with the public landing batch ID:

```sql
SELECT *
FROM control.customer_pipeline_status
WHERE landing_batch_id = $1;
```

The two PII-safe detail contracts use the same public ID:

```sql
SELECT *
FROM control.customer_data_quality_metric
WHERE landing_batch_id = $1;

SELECT *
FROM control.customer_data_quality_record
WHERE landing_batch_id = $1;
```

Migration 015 intentionally does not backfill old batches. For historical
batches `control.customer_pipeline_status.validation_available` remains
`false`, because current-state raw/staging rows cannot recreate a trustworthy
arrival-time snapshot.

After a test upload, run the read-only verification queries:

```sh
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 \
  -f database/queries/verify_customer_pipeline.sql
```

Deploy the landing stack before the ingestion stack so no new canonical output
is consumed by the old ingestion contract.

For a tracking-only release, deploy the Fargate image/task revision first and
then deploy only the ingestion stack:

```sh
CONFIG_FILE=env.yaml cdk deploy CdpIngestionData
```

To deploy the stack, run:

```sh
CONFIG_FILE=env.yaml cdk deploy
```

This command reads the configuration from `env.yaml` and deploys the stack dynamically.

## Destroying the Stack

If you need to remove the stack, run:

```sh
cdk destroy
```

## Elasticsearch standardization runtime stack

`StandardizationRuntimeStack` creates the dedicated queues, dispatcher, and
one-shot Fargate task definition. Destination merges are serialized per source
system inside PostgreSQL so monthly-index workers cannot block each other on
shared customer-key tuples. For the initial large baseline use `page_size:
1000`, `load_statement_timeout_seconds: 900`, and
`merge_lock_timeout_seconds: 1800` in the ECS configuration. Deploying these
values registers a new task-definition revision; already-running tasks keep
their previous image and environment.

Validate and deploy only this stack:

```sh
.venv/bin/pytest -q tests/unit/test_standardization_runtime_stack.py
PATH="$PWD/.venv/bin:$PATH" CONFIG_FILE=env.yaml \
  cdk diff CdpElasticsearchStandardizationRuntime
PATH="$PWD/.venv/bin:$PATH" CONFIG_FILE=env.yaml \
  cdk deploy CdpElasticsearchStandardizationRuntime
```

The worker image must already exist in ECR at the exact `image_uri` configured
in `env.yaml`.

## Database connector runtime stack

`ConnectorRuntimeStack` is the AWS orchestration half of database connector
intake. It creates encrypted occurrence/dispatch/result queues and DLQs,
EventBridge Scheduler group/role, an idempotent dispatch Lambda, a Standard Step
Functions workflow, logs, alarms, and least-privilege IAM policies. When the
fast worker is enabled, it additionally creates a FIFO fast-operation queue and
FIFO DLQ.

The stack imports the ECS cluster, existing one-shot connector and identity
stitcher task definitions, network references, and connector task role. When
`ecs.fast_worker_enabled` is `true`, it also creates a dedicated task role, task
definition, and continuously running Fargate service for fast operations. The
service runs `consume-fast` in private subnets without a public IP or load
balancer and uses ECS deployment-circuit-breaker rollback. The stack does not
create an ECS cluster, ECR repository, or container image. Copy
[env.connector.example.yaml](env.connector.example.yaml) and replace every
placeholder with an existing reference before synth or deployment.

`ecs.fast_worker_enabled` defaults to `false`, preserving the legacy stack when
the image settings are absent. When enabled, `ecs.fast_worker_image_uri` and
`ecs.fast_worker_container_name` are required. The image URI must reference a
private ECR repository in the same AWS account and region as the stack and must
be pinned by digest in the form
`<account>.dkr.ecr.<region>.amazonaws.com/<repository>@sha256:<64 lowercase hex characters>`.
The worker defaults to 1024 CPU units, 2048 MiB memory, one desired task, and a
110-second shutdown grace period. Queue visibility defaults to 300 seconds. The
heartbeat, long-poll, maximum-attempt, graceful-shutdown,
oldest-message-age-alarm, and backlog-alarm settings are configurable as shown
in the example file. The worker task receives these environment variables from
CDK; no database credential value is stored in YAML:

- `AWS_REGION`
- `DB_SECRET_NAME` (the configured warehouse secret ARN or name)
- `CONNECTOR_LANDING_BUCKET`
- `CONNECTOR_KMS_KEY_ID`
- `CONNECTOR_FAST_OPERATION_QUEUE_URL`
- `FAST_OPERATION_VISIBILITY_TIMEOUT_SECONDS`
- `FAST_OPERATION_HEARTBEAT_SECONDS`
- `FAST_OPERATION_POLL_WAIT_SECONDS`
- `FAST_OPERATION_MAX_ATTEMPTS`
- `FAST_OPERATION_SHUTDOWN_GRACE_SECONDS`

The heartbeat must be no more than half the queue visibility timeout. The FIFO
queue redrive count is `fast_worker_max_attempts + 1`: the first deliveries are
execution attempts, and the final source delivery is reserved for claim logic
to persist `MAX_EXECUTION_ATTEMPTS` before acknowledging the message. The ECS
container `StopTimeout` is the configured
`fast_worker_shutdown_grace_seconds` value.

Runtime routing is split as follows:

- `CONNECTION_TEST`, `SCHEMA_DISCOVERY`, and `DATASET_PREVIEW`: the API sends
  directly to the FIFO fast-operation queue for the warm worker. The existing
  dispatch queue, dispatcher Lambda, and Step Functions route remain available
  as fallback.
- `DATASET_SYNC`: imported connector task, then imported identity stitcher with
  `--batch-id` and `--job-id` through the existing dispatch and Step Functions
  path.
- Every terminal path publishes one idempotent result message for the backend
  result worker.

Validate without deployment:

```sh
.venv/bin/pytest -q tests/unit/test_connector_runtime_stack.py
PATH="$PWD/.venv/bin:$PATH" CONFIG_FILE=env.connector.example.yaml \
  cdk synth CdpDatabaseConnectorRuntime
```

The synthesized stack additionally outputs the fast-operation queue URL and
ARN, fast-operation DLQ ARN, fast-worker ECS service name and ARN, task
definition ARN, and log group name and ARN. Configure the API with the fast
queue URL; its imported backend role can send to both the existing dispatch
queue and the new FIFO queue. Building and publishing the digest-pinned worker
image remains a separate deployment step.

Use an additive rollout:

1. Publish the worker image and configure `ecs.fast_worker_enabled: true` with
   its immutable digest.
2. Keep `CONNECTOR_FAST_OPERATIONS_ENABLED=false` on `journey-builder-api`
   while deploying this infrastructure and verifying that the warm ECS task is
   healthy.
3. Configure the API with both
   `CONNECTOR_FAST_OPERATION_QUEUE_URL=<ConnectorFastOperationQueueUrl output>`
   and `CONNECTOR_FAST_OPERATIONS_ENABLED=true` to enable direct routing.
4. Keep the existing dispatch worker, Lambda, and Step Functions resources in
   place for fallback and `DATASET_SYNC`.

## Next Steps

This project is designed to be modular, allowing dynamic deployment of various AWS services. The next step involves adding additional stacks, such as API Gateway, DynamoDB, or other AWS services, for more complex deployments.

## Troubleshooting

- Ensure AWS credentials are configured properly using `aws configure`.
- Verify the CDK version with `cdk --version`.
- If a stack fails, check CloudFormation logs for details.

For more details, refer to the [AWS CDK documentation](https://docs.aws.amazon.com/cdk/latest/guide/home.html).
