# Durable stitching admission release

`control.stitching_job` is the transactional outbox shared by uploads and connector imports. Only completed ingestion with an arrival-time validation snapshot can be admitted. Admission is polled every minute and does not occupy Fargate while waiting. A separate advisory launch lock serializes dispatchers, while the unchanged writer lock and live ECS task check protect full-staging publication (including legacy/untracked callers).

## Deployment invariant

Apply migration 018 first. The ingestion asset automatically installs its schedule; synth requires `ECS_CLUSTER` and explicit `STITCHING_ADMISSION_NOT_BEFORE`. When the app includes ingestion and connector stacks, it automatically wires connector completion polling to ingestion. Connector-only config must set `stitching_admission_function_arn`. Deploy ingestion then connector, and inspect their exact live templates/IAM. Existing Step Functions executions keep old definitions: wait/observe them, never kill their writers. Keep worker revision 9 and immutable image unchanged.

Production cutover is `2026-09-17T00:00:00+07:00`. Older abandoned control rows are outside this incident and not rewritten. All live ECS writers and the physical database lock are still respected regardless of cutoff.

## Enforced cross-version activation gate

First deploy ingestion with `STITCHING_ADMISSION_ENABLED=false`: new uploads only enqueue, no new admission tasks start. Then deploy connector polling and removal of its `ecs:RunTask` permission for the stitcher. Verify zero running legacy connector executions (never stop an active writer), keep the old local recovery dispatcher paused, and wait strictly longer than the ingestion Lambda's configured 900-second maximum from its LastModified so no cached old invocation can still launch. Only then enable admission through the same CDK asset/config. `database/stitching_release_gate.py::verify_activation_gate` enforces all checks before the activating change set executes. The existing worker is allowed to complete throughout; admission still checks both live ECS and its physical writer lock. Administrative manual RunTask is not an application caller and must use the admission route after cutover.

`database/deploy_stitching_admission.py` is the actual executor; set `STITCHING_RELEASE_EVIDENCE` to the persisted synth/evidence directory and `STITCHING_PAUSED_RUNNER_PID` to the verified paused recovery parent. It calls `verify_activation_gate` immediately before the activating change set, not only during preparation. It rejects unexpected changed resources and permits only unchanged Lambda-ARN dependency propagation for notification permissions. The paused PID must remain paused until the old cached dispatcher is replaced.

## Durable failure rules

- Commit an immutable ECS request and client token before RunTask. Retry uncertain transport/commit results with the same request, at most 20 dispatch attempts within one hour. Never create a replacement merely because a response is missing.
- Definitive AWS rejection becomes a real FAILED job; later jobs can proceed. Actual worker validation, OOM, SQL and other failures remain visible and are never automatically reingested.
- A managed LOCK timeout caused by a racing legacy caller gets a new tracked stitching-only attempt with copied arrival facts after STOPPED is verified. Retain old attempts and follow the explicit retry chain for connector status. The safeguard is bounded at 20.
- A BLOCKED uncertain launch intentionally fences admission. Skipping it could run two writers after lost ECS visibility. Alarm and inspect the exact intent/token plus ECS task history. Never simply delete it or clear its token. Resolve by recording the proven task ARN with the normal dispatch recorder, or—only after proving no launch—mark the job failed with the normal failure recorder. This is a safety stop, not successful completion.
- Connector status is never enqueue success. It polls the effective attempt, returns real FAILED/BLOCKED, and after 24 hours reports `StitchingCompletionOverdueWorkRetained` while retaining queued work. The workflow has a seven-day outer bound, longer than the explicit monitored completion gate; extraction still has its original task timeout.

## Recovery coordination

The local incident runner caches Python dispatch code and ECS env. Before rollout pause it only while it waits on its verifier subprocess, not during ingestion/retry launch. Resume from manifest and persisted handles using the new revision and queue-aware return. No customer truncation, active-writer termination, ingestion replay for stitching failures, or task-definition replacement is part of this release.

## Tests

`CDP_TEST_DSN='host=127.0.0.1 port=55439 user=postgres dbname=admission_tests' uv run --with pytest --with psycopg2-binary --with boto3 pytest tests/integration/test_stitching_admission.py -q`

Use a dedicated disposable local PostgreSQL database: tests recreate only `control`/`quality` in that database, and assert loopback port 55439. Set `CDP_CONTENTION_SECONDS=905` for the real two-session beyond-old-budget lock proof. Run unit tests separately because legacy unit fixtures stub psycopg2.
