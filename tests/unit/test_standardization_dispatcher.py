import importlib.util
import json
from pathlib import Path

import pytest


MODULE_PATH = (
    Path(__file__).parents[2]
    / "_lambda"
    / "standardization_dispatcher"
    / "lambda_function.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location("standardization_dispatcher", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


def test_dispatcher_runs_one_task_with_message_override(monkeypatch):
    module = _load_module()

    class Ecs:
        request = None

        def run_task(self, **kwargs):
            self.request = kwargs
            return {"tasks": [{"taskArn": "arn:task/1"}], "failures": []}

    ecs = Ecs()
    monkeypatch.setattr(module, "ecs", ecs)
    for key, value in {
        "ECS_CLUSTER_ARN": "arn:cluster",
        "TASK_DEFINITION_ARN": "arn:task-definition",
        "CONTAINER_NAME": "worker",
        "PRIVATE_SUBNET_IDS_JSON": '["subnet-1"]',
        "SECURITY_GROUP_IDS_JSON": '["sg-1"]',
        "RESULT_QUEUE_URL": "https://sqs/result",
    }.items():
        monkeypatch.setenv(key, value)
    payload = {
        "message_version": 1,
        "operation_type": "ELASTICSEARCH_STANDARDIZE",
        "run_id": "00000000-0000-4000-8000-000000000001",
        "dataset_id": 1,
        "source_index": "trackers_202409",
        "stream_kind": "versions",
    }
    result = module.lambda_handler({"Records": [{"body": json.dumps(payload)}]}, None)
    assert result["taskArn"] == "arn:task/1"
    assert ecs.request["count"] == 1
    environment = ecs.request["overrides"]["containerOverrides"][0]["environment"]
    assert len([item for item in environment if item["name"] == "STANDARDIZATION_MESSAGE_JSON"]) == 1


def test_dispatcher_rejects_batch_or_unknown_stream(monkeypatch):
    module = _load_module()
    with pytest.raises(ValueError, match="exactly one"):
        module.lambda_handler({"Records": []}, None)
    with pytest.raises(ValueError, match="stream_kind"):
        module.lambda_handler(
            {
                "Records": [
                    {
                        "body": json.dumps(
                            {
                                "message_version": 1,
                                "operation_type": "ELASTICSEARCH_STANDARDIZE",
                                "run_id": "run",
                                "dataset_id": 1,
                                "source_index": "index",
                                "stream_kind": "automatic-restore",
                            }
                        )
                    }
                ]
            },
            None,
        )

