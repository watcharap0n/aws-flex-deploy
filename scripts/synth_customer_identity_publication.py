#!/usr/bin/env python3
"""Offline publication-only CDK synth; does not import app.py or call AWS.

Usage: .venv/bin/python scripts/synth_customer_identity_publication.py \
    --config /path/to/publication.json --outdir /path/to/assembly

JSON envelope: {"aws_account_id": "123456789012", "aws_region": "ap-southeast-7",
"stack_name": "CustomerIdentityPublication", "project_config": {...}}.
project_config requires cluster_name, vpc {vpc_id, availability_zones,
private_subnet_ids, security_group_ids}, digest-pinned same-account/region ECR
image_uri, db_secret_arn, clickhouse_secret_arn, clickhouse_url,
clickhouse_database and clickhouse_single_server_confirmed=true.

Secrets already exist; only their ARNs belong here. Dedicated PG JSON fields:
hostname, port, dbname, username, password, sslmode=require. CH fields:
username/password. Optional db_secret_kms_key_arn/clickhouse_secret_kms_key_arn
add scoped decrypt grants for customer-managed keys.

Default desired_count=0 creates a staged service. Set desired_count=1 AND
activation_ready=true only AFTER parent verifies DB migrations/grants, CH
DDL/cutover/query-log fencing, private connectivity and image architecture.
That flag is operator attestation, not an automated readiness probe. HTTPS is
the default transport; HTTP requires allow_private_http=true attesting an
approved private route or encrypted tunnel. Neither synth nor subnet imports
prove TLS, private DNS routing, secret contents or live readiness.

Runtime defaults: cpu=1024, memory_mib=2048 (4096 optional), batch_rows=10000,
batch_bytes=8388608, retry_seconds=60, poll_seconds=10. Root filesystem is
read-only; image must support it. AWS managed execution policy supplies ECR
pull/log delivery; task role reads only dedicated PG secret (plus optional KMS).
No image build, migration, lookup, deployment or worker startup is performed.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import aws_cdk as cdk

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from cdk_stacks.customer_identity_publication_stack import CustomerIdentityPublicationStack


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--config', required=True, type=Path, help='JSON with nonsecret identifiers only')
    parser.add_argument('--outdir', required=True, type=Path, help='isolated CDK assembly directory')
    args = parser.parse_args(argv)
    config = json.loads(args.config.read_text())
    app = cdk.App(outdir=str(args.outdir.resolve()), analytics_reporting=False)
    CustomerIdentityPublicationStack(
        app, config.get('stack_name', 'CustomerIdentityPublication'),
        project_config=config['project_config'],
        env=cdk.Environment(account=config['aws_account_id'], region=config['aws_region']),
    )
    app.synth()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
