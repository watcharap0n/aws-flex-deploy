# AWS Flex CDP Project Context

Use this repository as the central discussion and infrastructure repository for the user's CDP platform. Before making cross-repository changes, inspect the relevant sibling repositories and confirm their current contracts; this overview is context, not a substitute for current code.

## Repository Roles

- `aws-flex-deploy` (this repository): central AWS CDK and database-migration repository. It provisions the landing/ingestion Lambdas and the SQS, Step Functions, EventBridge Scheduler, Lambda, ECS/Fargate, IAM, KMS, networking, and observability glue used by the CDP runtimes. Many ECS, VPC, S3, secret, image, and backend resources are imported rather than created here.
- `journey-builder-api`: CDP control plane despite its name. It owns connector metadata, discovery, mappings, schedules, sync operations, standardization runs, outbox/result workers, and public APIs. It is the main backend consumer of outputs from this CDK repository.
- `cdp-database-connector-worker`: one-shot Fargate worker for PostgreSQL, MySQL, MariaDB, and MongoDB connection tests, discovery, previews, and bounded dataset syncs. Syncs land Parquet in S3 and load the shared customer warehouse contract.
- `fargate-user-stitching`: operational identity-resolution worker. It standardizes and validates `raw.customer`, performs deterministic/Splink matching, and writes `identity.*`. This is the runtime stitcher used by ingestion and connector syncs.
- `cdp-elasticsearch-standardization-worker`: one-shot Fargate worker that reads Elasticsearch data already migrated into PostgreSQL, then writes source-neutral `standardized.*` and quality/control records. It does not query Elasticsearch directly.
- `identity-resolution`: research, design, migration, and analytics workbench. Its Neo4j/ClickHouse identity code is a POC, not the production runtime stitcher. It also contains foundational standardized-data and ClickHouse-publication design artifacts.
- `journey-builder`: separate journey orchestration POC using FastAPI, Temporal, Kafka, PostgreSQL, and React. It currently has no confirmed direct runtime integration with `aws-flex-deploy` or `journey-builder-api`; similar names and workspace concepts do not imply shared schemas.

## Main Flows

1. File ingestion: S3 source upload -> landing Lambda -> canonical S3 customer CSV -> ingestion Lambda -> `raw.customer` and immutable batch lineage -> `fargate-user-stitching` -> `identity.*` and pipeline status.
2. Database connector: `journey-builder-api` -> dispatch SQS -> dispatcher Lambda -> Step Functions -> connector Fargate task -> for dataset sync, stitcher Fargate task -> result SQS -> API result worker.
3. Elasticsearch standardization: `journey-builder-api` plans partitions -> dispatch SQS -> dispatcher Lambda -> standardization Fargate tasks -> `standardized.*` -> result SQS -> API workers.
4. Journey orchestration is currently separate. A future integration could consume CDP audiences/events through an explicit connector or API contract, but it must not be assumed from the current code.

## Important Boundaries

- Treat `aws-flex-deploy` as the infrastructure hub, `journey-builder-api` as the CDP control plane, and the Fargate repositories as data-plane workers.
- The operational identity path uses `fargate-user-stitching`, not the top-level `identity-resolution` POC.
- Connector customer data converges on `raw.customer` and `identity.*`; Elasticsearch standardization currently writes a separate `standardized.*` model. Do not assume those identity models are bridged.
- Historical customer batch membership must use `raw.customer_batch_member`, not mutable `raw.customer.batch_id`.
- Database bootstrap is distributed. API/workers assume foundational CDP schemas that are not all created by this repository's migrations.
- Separate local validation, CDK synth, deployment, and live-production verification in all status reports.
- Never read or record `.env` values, credentials, secret payloads, or customer PII in project memory.

## Sibling Paths

- `/Users/kmac15/PycharmProjects/identity-resolution`
- `/Users/kmac15/PycharmProjects/journey-builder-api`
- `/Users/kmac15/PycharmProjects/journey-builder`
- `/Users/kmac15/PycharmProjects/cdp-database-connector-worker`
- `/Users/kmac15/PycharmProjects/cdp-elasticsearch-standardization-worker`
- `/Users/kmac15/PycharmProjects/fargate-user-stitching`
