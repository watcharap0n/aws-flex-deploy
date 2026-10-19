# Upload-only identity publication — deployed

## Verified result

AWS account `538783577018`, `ap-southeast-7`.

- `CdpCustomerIdentityPublicationRuntime`: CloudFormation UPDATE_COMPLETE, ECS rollout COMPLETED.
- One on-demand Fargate worker, task definition `CdpCustomerIdentityPublicationRuntimeTask8244070E:2`, desired/running/pending **1/1/0**.
- Immutable image: `538783577018.dkr.ecr.ap-southeast-7.amazonaws.com/customer-identity-stitcher@sha256:12978a1a8fb8216ac3139f69fe22228d3ee1b86cabd0ba1a53bb573ac581566b`.
- Only file uploads publish. Literal numeric app IDs come from durable batch refs; a private upload-scope UUID is internal bookkeeping, not a connector/platform workspace.
- Connector capture/publication/status gating excluded; connector API publication flag stays false deliberately.
- PostgreSQL17.7 migrations024/025 applied and trigger/private ACLs read back. No ingestion replay or old-success rewrite.
- ClickHouse25.8 paired views/storage and fixed HOST NONE consumer definer deployed. Existing business reads preserved; raw identity snapshots/stages/logs denied to business roles.
- Private TLS on `172.31.44.89:8443`, source-SG-only ingress from `sg-01429bf2cb2e6e7f8`; real ECS default-trust/authenticated CH+PG probe passed. Private keys stayed root-only on EC2.

## Real canary

Existing upload `f5bb48e1-9927-4d97-9f20-5156f7675a6e` received a new stitching attempt2 through durable admission:

- New job: `50c57903-be24-4a2a-8669-4357786a21ec`
- New resolution: `4ad04f78-8062-41e1-a79e-cafdf3d439a3`
- Stitching SUCCEEDED; transfer PUBLISHED; public pipeline **COMPLETED**.
- Ten normalized historical records, ten source users, ten mapping/profile rows, ten physical version rows, one marker.
- Actual source/ClickHouse payload SHA256: `ef5cda6a3a2f5e8326dc749be74721540122423bcec9ea6b856a4d76fcfc2657`.
- Existing436,623 profiles retained in the original rollback table; current consumer436,633 rows with no duplicate business keys.
- Explicit replay in a separate actual ECS task succeeded with **zero insert calls**, unchanged physical/marker counts.
- Zero connector transfers and zero capture issues at final verification. Worker subsequently IDLE without errors.

## Activation issue resolved

The first full worker preflight hit nginx414: encoded canonical-DDL query URI exceeded the default request-line buffer. No transfer had been claimed. The private-vhost-only fix is `large_client_header_buffers 4 64k`; all15 exact preflight queries then returned200 (largest observed URI24,997 bytes). Schema/TLS checks were not weakened. Live nginx config SHA256 `d162eabb27126b938e2c007e1cd653c105f3baa40c04e5f4f44478c4fd1d10b3`.

## Restored and intentionally held systems

- Stitching admission minute schedule restored ENABLED after canary exit0 and publication verification; its cutoff/backlog were not changed.
- Legacy commerce-loader cron `e61591e1cedb` remains PAUSED. Its old sequential profile-table exchange is incompatible and must not restart blindly. This pause is not a claim that commerce backfill finished.
- Connector/standardization workers were not restarted or stopped. API healthy; only API service was updated.
- No historical uploads were backfilled beyond this new-stitch canary. Unsafe global components remain blocked; global matching itself was not rewritten.

## Evidence

Release workspace `/Users/kmac15/Downloads/customer-identity-publication-release/`:
- `deployment-status.json`, `canary-end-to-end-verified.json`, `ecs-replay-verified.json`
- `migration-readback.json`, `live-cutover-readback.json`, `admission-restored.json`
- `activation-readback.json`, `ecs-probe-readback.json`, `ecs-probe-logs.json`
- `network-readback.json`, `acl/live-activation-readback.json`, `tls-preflight-diagnostic.json`
- Exact templates/configs, source manifests, review records and build scripts.

Backup: restrictive schema-only `backups/pre-publication-schema.dump`; archive listing/hash verified. It is not a full data backup. Aurora PITR retention7days was confirmed; original ClickHouse data remains in `cdp_customer.customer_profile_pre_identity` plus legacy snapshot.

## Operational caveats

- ECS restarts exited daemons; ambiguous query intent remains the durable safety fence. Do not clear CH_INFLIGHT or force PUBLISHED manually.
- Source registry must survive restores. A blocked capture needs a new safe stitch, not stale recapture of mutable identity state.
- CA valid through2028-09-18; leaf through2027-09-19. Rotate with overlap and rebuild trust image as documented in `tls/HANDOFF.md`.
- Host root was91% full (~9.5GB free) at last check. Retained publication versions need explicit capacity/retention policy; never delete current/rollback data casually.
- Source changes remain local/uncommitted, with deployed hashes pinned. A Git release/merge is separate from the verified live deployment.
