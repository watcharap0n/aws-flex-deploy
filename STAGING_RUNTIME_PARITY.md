# Staging runtime parity

AWS account `538783577018`, region `ap-southeast-7`, profile `buzz`.
Configuration: `env.staging.yaml`. Cluster: `cdp-stg-stitcher-cluster`.
Database EC2: `i-05a9fa5b081f186fd`, private address `172.31.38.158`.
Staging S3: `cdp-stg-538783577018-ap-southeast-7`.

Staging upload intake: `https://cdp-intake-dev.buzzsprint.dev`, stack
`CdpStgIntake`. See `STAGING_INTAKE.md` for multipart endpoints and deployment.

## Services

| Runtime | Deployment | Staging resources |
| --- | --- | --- |
| Connector fast operations | ECS service, desired 1 | Fargate Spot 0.25 vCPU / 512 MiB |
| Customer publication | `cdp-stg-publication`, desired 1 | Fargate 0.25 vCPU / 512 MiB |
| Scoped upload resolution | `cdp-stg-upload-resolution`, desired 1 | Fargate 0.25 vCPU / 1 GiB |
| Full identity stitching | Admission launches `cdp-stg-stitcher-task:4` on demand | Fargate 0.5 vCPU / 2 GiB |

Full stitching is launched on demand in production too; it is not a fourth warm
service. Worker images are pinned to the deployed production digests. The staging
API has `CONNECTOR_PUBLICATION_STATUS_ENABLED=true`. Existing connector API,
outbox/result/occurrence workers and Journey/Temporal/Kafka remain on staging EC2.

Admission is enabled with a 2026-10-05 activation cutoff in `env.staging.yaml`.
Five historical synthetic QUEUED jobs are outside that cutoff. New arrivals are
eligible. `scripts/staging_stitching_activation_gate.py` passed before activation:
no active writer, no running connector workflow, no direct connector RunTask
permission, and the disabled ingestion Lambda had drained.

The old EC2 `raw-clickhouse-mirror` is stopped to prevent competing direct writes.
Staging credentials are split by application/worker role; see STAGING_DATABASE_ACCESS.md. Database host fields
use the private EC2 address; PostgreSQL uses `sslmode=disable`.

## Schema

All 230 production ClickHouse objects are present. Engine, sorting/partition keys,
column names, positions and types match. The retired staging.raw_customer debug table was removed during credential alignment; other retained staging objects remain.
The public `cdp_customer.customer_profile` view has one intentional SQL difference:
it uses the canonical serving candidate and the deployed publication contract
`customer-identity-tfg:v2`. The production overlay currently fails that same
publisher's preflight with `CH_SCHEMA_MISMATCH`; production was not changed.

ClickHouse `enable_shared_storage_snapshot_in_query=1` is persisted in the remote
Compose-mounted `clickhouse/users.d/query-snapshot.xml`. Definer users have the
permissions needed for serving views and segment refresh objects.

PostgreSQL contains every production relation (130), with no column/type/nullability
differences. All 87 function definitions match. Staging retains three extra relations.
No production customer data was copied. Daily Glue/data completeness is outside
this verification.

## Evidence

Audit files live under `outputs/staging-parity-20261005/`:

- `admission-gate.json`: preactivation checks.
- `clickhouse-final-comparison.json`: current schema comparison and allowed view difference.
- `production-postgres*.json`, `staging-postgres*.json`: relational/function catalogs.
- `deploy-active.log`: staging CloudFormation deployment.
- `live-readiness.json`: service and synthetic pipeline status.
- `runtime-smoke.json`: one synthetic S3 upload, unique source, app `990005`.

Deployment requires the checked activation gate when admission is enabled:

```sh
PATH="$PWD/.venv/bin:$PATH" CONFIG_FILE=env.staging.yaml STITCHING_CHECKED_ACTIVATION=1 \
  cdk deploy CdpStgCustomerRuntime CdpStgIngestionData CdpStgDatabaseConnectorRuntime \
  --profile buzz --require-approval never
```

The marker records that the operator ran the activation checks; do not bypass them
when toggling admission after changing launchers or writer configuration.

## Verified runtime result (2026-10-05)

All three ECS services have desired/running `1/1`, no pending tasks, and completed
rollouts. A unique one-row upload produced stitching job
`9bb682fd-3cd5-4987-a94f-b2d1e6480bf7`: stitching `SUCCEEDED`, overall `COMPLETED`,
transfer `PUBLISHED`, no error code. ClickHouse readback for synthetic app
`990005` returned one profile with a non-null identity and publication sequence.
See `clickhouse-smoke-readback.json`. This verifies the S3 ingestion and full
stitching/publication path; a real external database connector extraction was not
part of this smoke test. Connector service is running and its staging queues,
API and IAM bindings are configured.

## Reduced staging sizing

On 2026-10-05, warm connector/publication services were reduced to 256 CPU units
and 512 MiB each; scoped resolution uses 256 CPU units / 1024 MiB. Total warm
allocation falls from 1.5 to 0.75 vCPU and from 5 to 2 GiB. Full stitching revision
4 uses 512 CPU units / 2048 MiB, with DuckDB capped at 512 MB. EC2 size is retained
because the combined ClickHouse, Kafka and application host had substantial CPU
usage. Ingestion runtime and Lambda sizing are unchanged.

Synthetic app `990006` passed using the reduced full stitcher: job
`6be596db-4d73-4f8e-9eb4-5b8266323477` SUCCEEDED, task revision 4 exited zero,
publication PUBLISHED, and ClickHouse returned one identified/published profile.
Evidence: `reduced-sizing-result.json`, `reduced-clickhouse-readback.json` and
`reduced-smoke.json`. This is a small functional test, not a bulk capacity test.

## Credential naming alignment (2026-10-07)

Application names and worker database roles now follow production. See
`STAGING_DATABASE_ACCESS.md` for the complete mapping. PostgreSQL database
`staging` was renamed to `postgres`; `journey_builder_staging` became
`journey_builder`. The bootstrap role was renamed to `postgres` to preserve
PostgreSQL system catalog ownership. Legacy ClickHouse user/database `staging`
are removed. Pipeline standardization still uses the PostgreSQL schema `staging`.
Publication/scoped resolution task definitions now use separate worker secrets.
Passwords and infrastructure endpoints stay specific to staging.

Post-alignment verification: all three warm ECS rollouts are `COMPLETED` at `1/1`.
Synthetic app `990007` reached stitching `SUCCEEDED`, overall `COMPLETED`, and
transfer `PUBLISHED`. ClickHouse returned one identified/published profile.
The scoped-resolution probe exited zero. Production column-level grants were
included to allow the publication worker's transfer updates. Final evidence is
under `outputs/staging-credential-alignment-20261007/`.

## HTTPS intake verification (2026-10-08)

`CdpStgIntake` is deployed with certificate, domain mapping, Route 53 alias and
staging bucket CORS. All five multipart endpoints passed via
`https://cdp-intake-dev.buzzsprint.dev`. The uploaded synthetic customer (app
`990008`) reached landing publication, ingestion with one loaded/zero rejected,
stitching `SUCCEEDED`, overall `COMPLETED`, and transfer `PUBLISHED`. ClickHouse
returned one identified/published profile. See `STAGING_INTAKE.md` and
`outputs/staging-intake-deployment-20261008/`.
