"""Local-only ECS rejection/admission contract; no AWS or database access."""
import copy
import importlib.util
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parents[2]
CAPACITY_REASON = "You've reached the limit on the number of vCPUs you can run concurrently"


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, ROOT / '_lambda/ingestion_data' / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def harness(monkeypatch):
    ingestion = load('capacity_ingestion', 'lambda_function.py')
    admission = load('capacity_admission', 'stitching_admission.py')
    ecs = MagicMock()
    ecs.list_tasks.return_value = {'taskArns': []}
    monkeypatch.setattr(ingestion, 'boto3', SimpleNamespace(client=lambda _: ecs))
    monkeypatch.setenv('ECS_CLUSTER', 'test-cluster')
    monkeypatch.setenv('STITCHING_ADMISSION_NOT_BEFORE', '1970-01-01T00:00:00+00:00')
    conn = MagicMock()
    cursor = conn.cursor.return_value.__enter__.return_value
    now = datetime.now(timezone.utc)
    intent = dict(state='PREPARED', dispatch_attempts=0, prepared_at=now,
                  next_attempt_at=now-timedelta(seconds=1),
                  request={'clientToken': 'stitch-job', 'taskDefinition': 'pinned:9',
                           'cluster': 'test-cluster', 'count': 1})
    state = SimpleNamespace(status='QUEUED')
    def fail(*args):
        state.status = 'FAILED'
    def record(*args):
        state.status = 'SUBMITTED'
    monkeypatch.setattr(ingestion, '_mark_stitching_dispatch_failed', MagicMock(side_effect=fail))
    monkeypatch.setattr(ingestion, '_record_stitching_dispatch', MagicMock(side_effect=record))
    monkeypatch.setattr(ingestion, '_stitching_task_request', MagicMock(side_effect=AssertionError('must reuse pinned request')))
    def tick():
        cursor.fetchone.side_effect = [
            (True,), (False,), (True,), None,
            {'job_id': 'job', 'ingestion_batch_id': 'batch'}, intent,
        ]
        cursor.fetchall.side_effect = [[], []]
        return admission.tick(conn, ingestion)
    return SimpleNamespace(ingestion=ingestion, admission=admission, ecs=ecs,
                           conn=conn, cursor=cursor, intent=intent, state=state, tick=tick)


def test_vcpu_rejection_retains_queue_then_replays_exact_request(harness, caplog):
    h = harness
    original = copy.deepcopy(h.intent['request'])
    h.ecs.run_task.side_effect = [
        {'tasks': [], 'failures': [{'reason': CAPACITY_REASON,
                                  'detail': 'private-response-body@example.invalid'}]},
        {'tasks': [{'taskArn': 'task-1'}], 'failures': []},
    ]
    assert h.tick() == {'status': 'retry_pending', 'job_id': 'job'}
    assert h.state.status == 'QUEUED'
    h.ingestion._mark_stitching_dispatch_failed.assert_not_called()
    updates = [(str(c.args[0]), c.args[1:]) for c in h.cursor.execute.call_args_list]
    updates = [(sql, args[0] if args else None) for sql, args in updates]
    assert any('dispatch_attempts=dispatch_attempts+1' in sql and "interval '60 seconds'" in sql for sql, _ in updates)
    assert any('last_error=%s' in sql and args == ('StitchingDispatchRetryable', 'job') for sql, args in updates)
    assert 'private-response-body' not in caplog.text
    assert h.tick()['status'] == 'submitted'
    assert h.state.status == 'SUBMITTED'
    assert [c.kwargs for c in h.ecs.run_task.call_args_list] == [original, original]
    h.ingestion._stitching_task_request.assert_not_called()


def test_submission_reports_job_age_and_admission_stage_without_payload(harness, caplog):
    h = harness
    h.ecs.run_task.return_value = {'tasks': [{'taskArn': 'task-1'}], 'failures': []}
    # Only a queue timestamp is needed; no customer fields are read or logged.
    h.cursor.fetchone.side_effect = [
        (True,), (False,), (True,), None,
        {'job_id': 'job', 'ingestion_batch_id': 'batch',
         'created_at': datetime.now(timezone.utc) - timedelta(seconds=1511)},
        h.intent,
    ]
    h.cursor.fetchall.side_effect = [[], []]
    with caplog.at_level(logging.INFO, logger=h.admission.__name__):
        assert h.admission.tick(h.conn, h.ingestion)['status'] == 'submitted'
    lines = [r.message for r in caplog.records if 'stitching_admission_tick ' in r.message]
    assert len(lines) == 1
    assert 'status=submitted' in lines[0]
    assert 'stage=dispatch' in lines[0]
    assert 'admission_elapsed_ms=' in lines[0]
    assert 'job_age_seconds=' in lines[0]
    assert 'task-1' not in lines[0]
    age = int(lines[0].split('job_age_seconds=')[1].split()[0])
    assert 1510 <= age <= 1514


def test_writer_contention_reports_blocked_stage_without_launch(harness, caplog):
    h = harness
    h.cursor.fetchone.side_effect = [(True,), (False,), (False,)]
    with caplog.at_level(logging.INFO, logger=h.admission.__name__):
        assert h.admission.tick(h.conn, h.ingestion) == {'status': 'waiting_for_writer'}
    lines = [r.message for r in caplog.records if 'stitching_admission_tick ' in r.message]
    assert len(lines) == 1
    assert 'status=waiting_for_writer stage=writer_lock ' in lines[0]
    h.ecs.run_task.assert_not_called()


def test_overdue_tick_reports_error_not_success(harness, caplog):
    h = harness
    h.cursor.fetchone.side_effect = [(True,), (True,), (False,)]
    with caplog.at_level(logging.INFO, logger=h.admission.__name__):
        with pytest.raises(RuntimeError, match='Stitching completion overdue'):
            h.admission.tick(h.conn, h.ingestion)
    lines = [r.message for r in caplog.records if 'stitching_admission_tick ' in r.message]
    assert len(lines) == 1
    assert 'status=error stage=writer_lock ' in lines[0]
    h.ecs.run_task.assert_not_called()


@pytest.mark.parametrize('reason', [
    CAPACITY_REASON, CAPACITY_REASON + '.',
    'You’ve reached the limit on the number of vCPUs you can run concurrently',
    'You’ve reached the limit on the number of vCPUs you can run concurrently.',
])
def test_only_known_capacity_reason_is_retryable(harness, reason):
    h = harness
    h.ecs.run_task.return_value = {'tasks': [], 'failures': [{'reason': reason}]}
    with pytest.raises(h.ingestion.StitchingDispatchRetryable, match='Fargate vCPU limit'):
        h.ingestion._start_stitching_task('batch', 'job', request=h.intent['request'])


def test_live_curly_apostrophe_quota_response_keeps_job_queued(harness):
    h = harness
    h.ecs.run_task.return_value = {
        'tasks': [], 'failures': [{
            'reason': 'You’ve reached the limit on the number of vCPUs you can run concurrently',
        }],
    }
    assert h.tick() == {'status': 'retry_pending', 'job_id': 'job'}
    assert h.state.status == 'QUEUED'
    h.ingestion._mark_stitching_dispatch_failed.assert_not_called()


def test_confirmed_zero_task_quota_resets_only_uncertain_dispatch_budget(harness):
    h = harness
    h.intent['dispatch_attempts'] = 19
    h.intent['prepared_at'] = datetime.now(timezone.utc) - timedelta(seconds=3599)
    h.ecs.run_task.return_value = {
        'tasks': [], 'failures': [{'reason':
            'You’ve reached the limit on the number of vCPUs you can run concurrently'}],
    }
    assert h.tick() == {'status': 'retry_pending', 'job_id': 'job'}
    statements = [call.args[0] for call in h.cursor.execute.call_args_list]
    assert any('dispatch_attempts=0' in sql and
               'prepared_at=clock_timestamp()' in sql and
               "interval '60 seconds'" in sql for sql in statements)
    h.ingestion._mark_stitching_dispatch_failed.assert_not_called()


@pytest.mark.parametrize('failures', [
    [{'reason': 'RESOURCE:MEMORY'}],
    [{'reason': 'RESOURCE:CPU'}],
    [{'reason': 'unknown capacity error'}],
    [{}],
    [{'reason': CAPACITY_REASON}, {'reason': 'RESOURCE:MEMORY'}],
    [{'reason': CAPACITY_REASON + ' unrecognized suffix'}],
])
def test_other_zero_task_rejections_remain_permanent(harness, failures):
    h = harness
    h.ecs.run_task.return_value = {'tasks': [], 'failures': failures}
    with pytest.raises(h.ingestion.StitchingDispatchRejected):
        h.tick()
    assert h.state.status == 'FAILED'
    h.ingestion._mark_stitching_dispatch_failed.assert_called_once()


@pytest.mark.parametrize('response', [
    {'tasks': [{'taskArn': 'task-1'}], 'failures': [{'reason': CAPACITY_REASON}]},
    {'tasks': [], 'failures': []},
    {'tasks': [{}]},
])
def test_ambiguous_responses_stay_queued_and_raise(harness, response):
    h = harness
    h.ecs.run_task.return_value = response
    with pytest.raises(RuntimeError) as error:
        h.tick()
    assert type(error.value) is RuntimeError
    assert h.state.status == 'QUEUED'
    h.ingestion._mark_stitching_dispatch_failed.assert_not_called()


@pytest.mark.parametrize('code,failed', [
    ('AccessDeniedException', True), ('InvalidParameterException', True),
    ('ClientException', True), ('ClusterNotFoundException', True),
    ('PlatformUnknownException', True), ('ThrottlingException', False),
    ('ServerException', False),
])
def test_client_error_classification_is_unchanged(harness, code, failed):
    from botocore.exceptions import ClientError
    h = harness
    h.ecs.run_task.side_effect = ClientError({'Error': {'Code': code, 'Message': 'test'}}, 'RunTask')
    with pytest.raises(ClientError):
        h.tick()
    assert h.state.status == ('FAILED' if failed else 'QUEUED')


def test_lost_response_is_not_acknowledged_as_capacity_retry(harness):
    h = harness
    h.ecs.run_task.side_effect = TimeoutError('response lost')
    with pytest.raises(TimeoutError):
        h.tick()
    assert h.state.status == 'QUEUED'
    h.ingestion._mark_stitching_dispatch_failed.assert_not_called()


def test_retry_delay_prevents_immediate_relaunch(harness):
    h = harness
    h.intent['next_attempt_at'] = datetime.now(timezone.utc) + timedelta(seconds=60)
    assert h.tick() == {'status': 'retry_pending', 'job_id': 'job'}
    h.ecs.run_task.assert_not_called()


@pytest.mark.parametrize('attempts,age', [(20, 0), (1, 3601)])
def test_capacity_retry_retains_existing_bounded_fence(harness, attempts, age):
    h = harness
    h.intent['dispatch_attempts'] = attempts
    h.intent['prepared_at'] = datetime.now(timezone.utc) - timedelta(seconds=age)
    with pytest.raises(RuntimeError, match='budget exhausted'):
        h.tick()
    assert h.state.status == 'QUEUED'
    h.ecs.run_task.assert_not_called()
    h.ingestion._mark_stitching_dispatch_failed.assert_not_called()
    assert any("state='BLOCKED'" in c.args[0] for c in h.cursor.execute.call_args_list)


def test_missing_configuration_does_not_become_capacity_retry(harness, monkeypatch):
    monkeypatch.delenv('ECS_CLUSTER')
    with pytest.raises(ValueError, match='ECS_CLUSTER'):
        harness.tick()
    harness.ecs.run_task.assert_not_called()
