# Staging upload intake

Base URL: `https://cdp-intake-dev.buzzsprint.dev`

Stack: `CdpStgIntake`, AWS account `538783577018`, region `ap-southeast-7`,
profile `buzz`. API: `cdp-stg-upload-multipart`; stage: `staging`.
Lambda: `cdp-stg-s3-multipart`, Python 3.11, 128 MiB, 30-second timeout.
Bucket: `cdp-stg-538783577018-ap-southeast-7`.

## Client contract

Use the same multipart paths and JSON fields as production, with this base URL.
Do not include `/staging` when using the custom domain.

| POST path | JSON fields | Result |
| --- | --- | --- |
| `/multipart-upload/initiate` | `key`, optional `contentType` | `uploadId`, `key`, staging `bucket` |
| `/multipart-upload/presign-part` | `key`, `uploadId`, `partNumber` | `url`, `partNumber`, `expiresIn` |
| `/multipart-upload/list-parts` | `key`, `uploadId` | `parts`: part number, ETag, byte size |
| `/multipart-upload/complete` | `key`, `uploadId`, `parts` | Completed staging object details |
| `/multipart-upload/abort` | `key`, `uploadId` | Upload aborted |

After presigning, PUT the bytes directly to the returned S3 URL. Read its `ETag`
response header. Complete accepts parts as either
`{"partNumber":1,"etag":"..."}` or `{"PartNumber":1,"ETag":"..."}`.
Presigned part URLs expire after 3,600 seconds. APIs accept JSON POST requests;
OPTIONS handles browser preflight. API/S3 CORS allows all origins, matching the
development setup. S3 CORS exposes `ETag` for multipart completion.

## Customer landing path

```text
data_sources/<source_code>/<batch_ref>/customers/customer.csv
```

Source code uses lowercase letters, numbers, underscore or hyphen, at most 64
characters. For customer app lineage, use the current pipeline batch-ref format:
`YYYY-MM-DD-HHMMSS-<numeric_app_id>-<12_lowercase_hex_characters>`.
CSV requires `source_customer_id`; optional customer columns follow the existing
landing contract. XLSX and JSON are also accepted by the landing processor.

Completing an object under this path invokes `cdp-stg-landing-data`. Its published
`raw/.../customer.csv` invokes `cdp-stg-ingestion-data`, followed by the existing
staging stitching and publication workers.

## Deployment

Configuration is the `staging_intake_stack` section of `env.staging.yaml`.
This entry point deploys only intake:

```sh
cdk deploy CdpStgIntake --app '.venv/bin/python scripts/staging_intake_app.py' \
  --profile buzz --require-approval never
```

CloudFormation owns the ACM certificate, its Route 53 validation record, API
Gateway custom domain and root mapping, Route 53 A alias, Lambda/API, and bucket
CORS configuration. The existing S3 bucket is imported. Upload IAM permissions
are limited to that staging bucket's objects. Domain and bucket validation reject
production bindings.

## Verification

```sh
python3 scripts/staging_intake_smoke.py
python3 scripts/staging_intake_pipeline_check.py
```

The smoke exercises all five multipart endpoints through HTTPS, API/S3 CORS,
uploaded byte equality in the staging bucket, and abort cleanup. It uses synthetic
app `990008`. The pipeline check reads that exact landing/ingestion batch through
stitching/publication and verifies ClickHouse readback.

Deployment outputs and verification evidence are in
`outputs/staging-intake-deployment-20261008/`. The smoke does not log presigned
URLs. When a local resolver still caches the earlier missing-domain response,
public DNS can be used for the test connection with HTTPS hostname verification
enabled; the record and certificate remain the same.

### Verified deployment (2026-10-08)

- CloudFormation `CREATE_COMPLETE`, API ID `tna3kjpd2i`.
- ACM certificate `792ed131-0503-4c9b-b83f-7aedb862c66f` is `ISSUED`.
- Custom domain is `AVAILABLE`, with root mapping to API stage `staging`.
- Route 53 A alias targets `d-tcn07barub.execute-api.ap-southeast-7.amazonaws.com`;
  the certificate DNS-validation CNAME is also installed.
- API/S3 CORS, all five multipart endpoints, uploaded byte equality, and abort
  cleanup passed through HTTPS with hostname verification enabled.
- Normal DNS plus HTTPS was independently confirmed from staging EC2 (OPTIONS
  returned `204`, `Access-Control-Allow-Origin: *`).
- Synthetic landing batch `dfcbee43-c158-4c3b-9c7b-7c647d76875e` published one row;
  ingestion batch `3f2b8c48-475b-4de2-8b98-9b94b88cc463` loaded one, rejected zero.
- Stitching job `c638db91-45bc-4f0c-b200-ee7e7d1ebd5f` is `SUCCEEDED`, overall
  status `COMPLETED`, transfer `PUBLISHED`, all error fields null.
- ClickHouse returned one profile for app `990008`, with identity and publication
  sequence populated.
- Existing staging bucket notifications and production intake bindings were
  preserved. Three stack isolation tests passed. The documented intake-only CDK
  entry point synthesizes the exact deployed template.

Evidence: `deploy.log`, `stack-outputs.json`, `live-bindings.json`,
`upload-smoke.json`, `normal-dns-https.json`, `pipeline-smoke.json`, and
`deployment-validation.json` in the deployment output directory.
