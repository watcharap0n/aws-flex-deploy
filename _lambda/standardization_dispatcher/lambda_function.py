"""Dispatch one standardization SQS work item to one ECS Fargate task."""

from __future__ import annotations

import json
import os
from typing import Any

import boto3

ecs = boto3.client("ecs")


def _required(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} is required")
    return value


def _validate_message(payload: dict[str, Any]) -> None:
    if payload.get("message_version") != 1:
        raise ValueError("unsupported message_version")
    if payload.get("operation_type") != "ELASTICSEARCH_STANDARDIZE":
        raise ValueError("unsupported operation_type")
    if payload.get("stream_kind") not in {"baseline", "versions"}:
        raise ValueError("unsupported stream_kind")
    for name in ("run_id", "dataset_id", "source_index"):
        if payload.get(name) in (None, ""):
            raise ValueError(f"{name} is required")


def lambda_handler(event: dict[str, Any], _context: Any) -> dict[str, Any]:
    records = event.get("Records") or []
    if len(records) != 1:
        raise ValueError("dispatcher requires exactly one SQS record")
    payload = json.loads(records[0]["body"])
    _validate_message(payload)

    response = ecs.run_task(
        cluster=_required("ECS_CLUSTER_ARN"),
        taskDefinition=_required("TASK_DEFINITION_ARN"),
        launchType="FARGATE",
        count=1,
        networkConfiguration={
            "awsvpcConfiguration": {
                "subnets": json.loads(_required("PRIVATE_SUBNET_IDS_JSON")),
                "securityGroups": json.loads(_required("SECURITY_GROUP_IDS_JSON")),
                "assignPublicIp": "DISABLED",
            }
        },
        overrides={
            "containerOverrides": [
                {
                    "name": _required("CONTAINER_NAME"),
                    "environment": [
                        {
                            "name": "STANDARDIZATION_MESSAGE_JSON",
                            "value": json.dumps(payload, separators=(",", ":")),
                        },
                        {
                            "name": "STANDARDIZATION_RESULT_QUEUE_URL",
                            "value": _required("RESULT_QUEUE_URL"),
                        },
                    ],
                }
            ]
        },
        startedBy=f"std-{str(payload['run_id']).replace('-', '')[:28]}",
    )
    failures = response.get("failures") or []
    if failures:
        raise RuntimeError(f"ECS RunTask failed: {failures[0].get('reason', 'unknown')}")
    tasks = response.get("tasks") or []
    if len(tasks) != 1:
        raise RuntimeError("ECS RunTask did not return exactly one task")
    return {"taskArn": tasks[0]["taskArn"], "run_id": payload["run_id"]}

