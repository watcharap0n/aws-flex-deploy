"""Dispatch connector operations from SQS into one idempotent Step Functions run."""

from __future__ import annotations

import json
import os
from typing import Any

import boto3
from botocore.exceptions import ClientError

STEP_FUNCTION_ARN = os.environ["STEP_FUNCTION_ARN"]
SFN = boto3.client("stepfunctions")


def lambda_handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    failures: list[dict[str, str]] = []
    for record in event.get("Records", []):
        message_id = str(record.get("messageId", ""))
        try:
            payload = json.loads(record["body"])
            operation_id = str(payload["operation_id"])
            SFN.start_execution(
                stateMachineArn=STEP_FUNCTION_ARN,
                name=operation_id,
                input=json.dumps(payload, separators=(",", ":")),
            )
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") != "ExecutionAlreadyExists":
                failures.append({"itemIdentifier": message_id})
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            failures.append({"itemIdentifier": message_id})
    return {"batchItemFailures": failures}

