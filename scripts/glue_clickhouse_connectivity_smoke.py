"""Read-only ClickHouse probe using an isolated Glue NETWORK connection.

Connection details are non-secret Glue job arguments. Credentials are fetched
from Secrets Manager and never written to S3, Glue metadata or logs.
"""
import argparse
import json
import ssl
from urllib.request import Request, urlopen

import boto3


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--ch_secret_arn", required=True)
    parser.add_argument("--ch_ca_bucket", required=True)
    parser.add_argument("--ch_ca_key", required=True)
    parser.add_argument("--ch_endpoint", required=True)
    args, _glue_args = parser.parse_known_args()
    secrets = boto3.client("secretsmanager")
    s3 = boto3.client("s3")
    secret = json.loads(secrets.get_secret_value(
        SecretId=args.ch_secret_arn)["SecretString"])
    ca_pem = s3.get_object(Bucket=args.ch_ca_bucket,
                           Key=args.ch_ca_key)["Body"].read().decode("ascii")
    context = ssl.create_default_context(cadata=ca_pem)
    endpoint = args.ch_endpoint.rstrip("/")
    request = Request(endpoint + "/?query=SELECT%201%20FORMAT%20JSON",
                      headers={"X-ClickHouse-User": secret["username"],
                               "X-ClickHouse-Key": secret["password"]})
    try:
        with urlopen(request, context=context, timeout=15) as response:
            payload = json.load(response)
    except Exception as exc:
        # A transport exception can include request details; log type only.
        print("CLICKHOUSE_SMOKE_FAILED", type(exc).__name__, flush=True)
        return 1
    if payload.get("rows") != 1 or payload.get("data") != [{"1": 1}]:
        print("CLICKHOUSE_SMOKE_FAILED invalid SELECT 1 response", flush=True)
        return 1
    print("CLICKHOUSE_SMOKE_OK verified_tls=true rows=1", flush=True)
    return 0


if __name__ == "__main__":
    if main() != 0:
        raise RuntimeError("ClickHouse connectivity smoke failed")
