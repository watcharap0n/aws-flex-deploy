"""Original S3 notifications ACK only after synchronous durable enqueue."""
import io
import json
from types import SimpleNamespace

import pytest
import psycopg2.extras  # Load real driver before legacy unit fixtures install stubs.
from tests.unit.test_customer_pipeline import landing, ingestion, _config


def original():
    return {'eventTime':'2026-09-18T00:00:00Z','s3':{'bucket':{'name':'input'},
        'object':{'key':'data_sources/crm/batch/customers/source.csv','eTag':'etag','versionId':'version'}}}


def test_original_lambda_enqueues_synchronously_without_payload_work(landing, monkeypatch):
    monkeypatch.setenv('CUSTOMER_LANDING_ENABLED','true')
    monkeypatch.setenv('CUSTOMER_INGESTION_FUNCTION_ARN','arn:aws:lambda:region:account:function:ingestion')
    monkeypatch.setattr(landing,'_load_config',lambda:_config(landing))
    monkeypatch.setattr(landing,'_get_secret',lambda *a:pytest.fail('original must enqueue before database/download'))
    record=original()
    calls=[]
    body=io.BytesIO(json.dumps({'results':[{'job_id':'11111111-1111-1111-1111-111111111111','status':'QUEUED'}]}).encode())
    def invoke(**kwargs):
        calls.append(kwargs)
        return {'StatusCode':200,'Payload':body}
    monkeypatch.setattr(landing.boto3,'client',lambda name:SimpleNamespace(invoke=invoke) if name=='lambda' else pytest.fail('no S3 in relay'))
    result=landing.lambda_handler({'Records':[record]},None)
    assert result['status']=='queued' and result['processed_records']==0
    assert calls[0]['InvocationType']=='RequestResponse'
    assert json.loads(calls[0]['Payload'])=={'action':'customer_ingestion_enqueue','stage':'landing','Records':[record]}
    assert result['results'][0]['status']=='QUEUED'
    assert body.closed


def test_landing_relay_does_not_bundle_independent_accepted_records(landing, monkeypatch):
    monkeypatch.setenv('CUSTOMER_LANDING_ENABLED', 'true')
    monkeypatch.setenv('CUSTOMER_INGESTION_FUNCTION_ARN', 'ingestion')
    monkeypatch.setattr(landing, '_load_config', lambda: _config(landing))
    monkeypatch.setattr(landing, '_get_secret', lambda *a: pytest.fail('relay must not run payload work'))
    first = original()
    second = original()
    second['s3']['object']['key'] = 'data_sources/crm/batch/customers/second.csv'
    accepted = [first, second]
    persisted = {}
    bodies = []

    def invoke(**kwargs):
        assert kwargs['InvocationType'] == 'RequestResponse'
        records = json.loads(kwargs['Payload'])['Records']
        assert len(records) == 1, 'one oversized aggregate must not gate independent jobs'
        record = records[0]
        key = record['s3']['object']['key']
        persisted.setdefault(key, f'11111111-1111-1111-1111-{len(persisted) + 1:012d}')
        body = io.BytesIO(json.dumps({'results': [{'job_id': persisted[key], 'status': 'QUEUED'}]}).encode())
        bodies.append(body)
        return {'StatusCode': 200, 'Payload': body}

    monkeypatch.setattr(landing.boto3, 'client', lambda name: SimpleNamespace(invoke=invoke) if name == 'lambda' else pytest.fail('relay must not download'))
    result = landing.lambda_handler({'Records': accepted}, None)
    assert result['queued_records'] == len(accepted)
    assert [job['job_id'] for job in result['results']] == [persisted[record['s3']['object']['key']] for record in accepted]
    assert len(bodies) == len(accepted) and all(body.closed for body in bodies)


def test_landing_relay_retries_after_partial_durable_enqueue(landing, monkeypatch):
    monkeypatch.setenv('CUSTOMER_LANDING_ENABLED', 'true')
    monkeypatch.setenv('CUSTOMER_INGESTION_FUNCTION_ARN', 'ingestion')
    monkeypatch.setattr(landing, '_load_config', lambda: _config(landing))
    first = original()
    second = original()
    second['s3']['object']['key'] = 'data_sources/crm/batch/customers/second.csv'
    records = [first, second]
    seen = []
    durable = {}
    bodies = []
    fail_once = True

    def invoke(**kwargs):
        nonlocal fail_once
        record, = json.loads(kwargs['Payload'])['Records']
        key = record['s3']['object']['key']
        seen.append(key)
        if key == second['s3']['object']['key'] and fail_once:
            fail_once = False
            body = io.BytesIO(b'{}')
            bodies.append(body)
            return {'StatusCode': 200, 'FunctionError': 'Unhandled', 'Payload': body}
        durable.setdefault(key, f'11111111-1111-1111-1111-{len(durable) + 1:012d}')
        body = io.BytesIO(json.dumps({'results': [{'job_id': durable[key], 'status': 'QUEUED'}]}).encode())
        bodies.append(body)
        return {'StatusCode': 200, 'Payload': body}

    monkeypatch.setattr(landing.boto3, 'client', lambda name: SimpleNamespace(invoke=invoke) if name == 'lambda' else pytest.fail('no payload work in relay'))
    with pytest.raises(RuntimeError, match='Durable landing enqueue failed'):
        landing.lambda_handler({'Records': records}, None)
    assert list(durable) == [first['s3']['object']['key']]
    result = landing.lambda_handler({'Records': records}, None)
    assert result['queued_records'] == 2
    assert [job['job_id'] for job in result['results']] == [durable[record['s3']['object']['key']] for record in records]
    assert seen == [first['s3']['object']['key'], second['s3']['object']['key']] * 2
    assert all(body.closed for body in bodies)


def test_dedicated_cap_admits_137mib_before_download_materialization(landing,monkeypatch):
    class ReadReached(RuntimeError):
        pass
    class Body:
        closed=False
        def read(self,size):
            raise ReadReached('bounded read reached')
        def close(self):
            self.closed=True
    body=Body()
    source=landing._source_from_record(original(),_config(landing))
    s3=SimpleNamespace(get_object=lambda **kw:{'Body':body,'ContentLength':137*1024**2,'ETag':'etag','VersionId':'version'})
    monkeypatch.setenv('LANDING_MAX_CSV_BYTES', str(64*1024**2))
    with pytest.raises(landing.SourceCapacityError):
        landing._download_object(s3,source)
    monkeypatch.setenv('LANDING_MAX_CSV_BYTES',str(256*1024**2))
    with pytest.raises(ReadReached):
        landing._download_object(s3,source)
    assert body.closed


@pytest.mark.parametrize('response',[
    {'StatusCode':202}, {'StatusCode':200,'FunctionError':'Unhandled'},
    {'StatusCode':200,'payload':{'results':[]}},
    {'StatusCode':200,'payload':{'results':[{'job_id':'bad','status':'QUEUED'}]}},
    {'StatusCode':200,'payload':{'results':[{'job_id':'11111111-1111-1111-1111-111111111111','status':'completed'}]}},
])
def test_original_relay_never_acks_failed_or_partial_response(landing,monkeypatch,response):
    monkeypatch.setenv('CUSTOMER_LANDING_ENABLED','true')
    monkeypatch.setenv('CUSTOMER_INGESTION_FUNCTION_ARN','ingestion')
    monkeypatch.setattr(landing,'_load_config',lambda:_config(landing))
    body=io.BytesIO(json.dumps(response.get('payload',{})).encode())
    monkeypatch.setattr(landing.boto3,'client',lambda n:SimpleNamespace(invoke=lambda **kw:dict(response,Payload=body)))
    with pytest.raises((ValueError,RuntimeError)):
        landing.lambda_handler({'Records':[original()]},None)
    assert body.closed
