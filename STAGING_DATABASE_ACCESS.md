# Staging database access names

> 2026-10-19: this describes the Oct 5–8 staging. Since 2026-10-12 staging is one box (`cdp-stg`, EIP 43.211.89.31, Docker Compose); see buzz-cdp-workspace `ops/RUNBOOK.md` "Staging". Secret names below are still valid.

Host for workstation clients: `ec2-43-210-31-231.ap-southeast-7.compute.amazonaws.com`.
Internal staging host: `172.31.38.158`. Ports remain PostgreSQL `5432` and ClickHouse `8123`.
All passwords remain staging-specific and are held in Secrets Manager.

| Purpose | Database | Username | Secret |
| --- | --- | --- | --- |
| Pipeline read-only application access | postgres | buzzcdp_app | cdp/staging/pipeline-reader/postgres |
| Journey reads/writes | journey_builder | journey_builder_dev | journey-marketing-automation-dev |
| ClickHouse segments | segment | buzz_segment | clickhouse-marketing-automation-dev |
| Pipeline ingestion/connector/full stitching | postgres | postgres | postgres-marketing-automation-dev |
| PostgreSQL publication worker | postgres | identity_publication_worker | cdp/staging/identity-publication/postgres |
| PostgreSQL scoped-resolution worker | postgres | customer_app_history_backfill | cdp/staging/customer-app-history-backfill/postgres |
| ClickHouse publication worker | cdp_customer | identity_publication_worker | cdp/staging/identity-publication/clickhouse |

Journey application schema: `dev_workspace`, alongside the qualified Journey engine
schemas (`journey`, `runtime`, `registry`, etc.). Database role memberships/grants
are derived from production. Staging admin accounts are PostgreSQL `postgres` and
ClickHouse `app`; their remote Compose configuration is held in a mode-600 `.env`.

Temporal keeps its staging database names (`temporal_staging` and
`temporal_visibility_staging`); its secret uses `journey_builder_dev`, with the same
password already shared by the Journey secret. The PostgreSQL **schema** `staging`
is retained for the standardization pipeline; it is different from the retired
PostgreSQL **database** `staging`.

Secret version IDs, production grant snapshots, and verification reports are under
`outputs/staging-credential-alignment-20261007/`. No production password is copied.
The alignment helper does not create backups.

## Application variables

```dotenv
CDP_DB_HOST=ec2-43-210-31-231.ap-southeast-7.compute.amazonaws.com
CDP_DB_PORT=5432
CDP_DB_NAME=postgres
CDP_DB_USER=buzzcdp_app
CDP_JOURNEY_DB_HOST=ec2-43-210-31-231.ap-southeast-7.compute.amazonaws.com
CDP_JOURNEY_DB_PORT=5432
CDP_JOURNEY_DB_NAME=journey_builder
CDP_JOURNEY_DB_USER=journey_builder_dev
CDP_JOURNEY_DB_SCHEMA=dev_workspace
CDP_CLICKHOUSE_HOST=ec2-43-210-31-231.ap-southeast-7.compute.amazonaws.com
CDP_CLICKHOUSE_PORT=8123
CDP_CLICKHOUSE_DATABASE=segment
CDP_CLICKHOUSE_USER=buzz_segment
```

Resolve passwords from the secret for each purpose. Never give the read-only
application credential to pipeline writers. SSL remains disabled for staging
PostgreSQL, matching the existing EC2 setup.

## Applied and verified (2026-10-07)

`CdpStgCustomerRuntime` was deployed with publication and scoped-resolution task
definitions at revision 3. All three warm ECS services are desired/running `1/1`,
pending `0`, with `COMPLETED` rollouts. PostgreSQL, ClickHouse, connector API,
Journey API and Temporal are healthy; Nginx is active. Landing/ingestion Lambda
concurrency is restored.

Production grants include 54 column-level grants. These are required by the
publication worker: table-level grants alone leave its transfer UPDATE denied.
Reference grants and memberships are saved alongside the verification reports.

Authenticated checks verified pipeline SELECT access with writes denied, Journey
DDL/read/write access in a rolled-back transaction, Temporal database login, and
ClickHouse segment create/insert/read/drop access.

Synthetic app `990007` completed ingestion and stitching (`SUCCEEDED`), overall
status `COMPLETED`, and transfer `PUBLISHED`, with no error code. ClickHouse
readback returned one profile with identity and publication sequence populated.
Scoped-resolution one-shot probe exited zero. Evidence:
`credential-validation.json`, `scoped-worker-probe.json`, `live-readiness.json`,
`clickhouse-smoke-readback.json`, and `final-health.json` in the audit directory.
A real external connector extraction was not part of this verification.

Retired PostgreSQL roles/databases and ClickHouse user/database `staging` are
absent. PostgreSQL schema `staging` remains required by the pipeline. Temporary
alignment admin and the old empty PostgreSQL database are also removed.
