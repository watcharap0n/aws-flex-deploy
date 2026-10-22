"""Streaming path must preserve the complete reviewed CSV parser contract."""
import importlib.util
import io
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('stream_ingestion', ROOT/'_lambda/ingestion_data/lambda_function.py')
ingestion = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ingestion)


def test_stream_parser_uses_repeatable_disk_store_without_semantic_drift(tmp_path):
    assert hasattr(ingestion, 'prepare_customer_stream'), 'bounded parser entry point missing'
    import sys
    sys.path.insert(0, str(ROOT/'_lambda/ingestion_data'))
    from customer_ingestion_runtime import CustomerStore
    data = ('\ufeffcustomer_id,source_customer_id,email,status,age,date_of_birth,external_ids,attributes,device_id(s),source\n'
            'alias, x ,OLD@EXAMPLE.TEST,ใช้งาน,21,2000-01-01,{},{},a;b;a,web\n'
            'alias,,invalid@example.test,ACTIVE,10,2000-01-01,{},{},,\n'
            'alias,x,BAD@EXAMPLE.TEST,ACTIVE,no,2000-01-01,{},{},,\n'
            'alias,y,Y@EXAMPLE.TEST,SUSPENDED,40,1980-01-01,{},{},,\n'
            'alias,x,NEW@EXAMPLE.TEST,ปิดใช้งาน,22,2000-01-01,{},{},c,app\n').encode()
    expected, expected_stats = ingestion.prepare_customers(data)
    with CustomerStore(tmp_path/'customers.sqlite') as store:
        actual, stats = ingestion.prepare_customer_stream(io.BytesIO(data), store)
        assert actual is store
        assert list(actual) == expected == list(actual[:])
        assert list(actual) == expected  # repeatable after bulk COPY iteration
        # The age 'no' row is kept (age NULL, text in attributes); only the keyless row is dropped.
        assert stats == expected_stats == {'records_received': 5, 'records_rejected': 1, 'duplicate_rows': 2,
                                           'records_normalised': 1, 'normalised_fields': {'age': 1}}
        assert len(actual) == 2


def test_bulk_feature_flag_delegates_exact_repeatable_store(monkeypatch):
    import sys
    import types
    sentinel = object()
    calls = []
    monkeypatch.setitem(sys.modules, 'customer_bulk', types.SimpleNamespace(upsert_customers=lambda *a: calls.append(a)))
    monkeypatch.setenv('CUSTOMER_BULK_ENABLED', 'true')
    ingestion._upsert_customers(sentinel, [{'source_customer_id':'x'}], 'batch', 1)
    assert calls == [(sentinel,[{'source_customer_id':'x'}],'batch',1)]


def test_lambda_durable_route_acknowledges_only_persisted_job(monkeypatch):
    import sys
    import types
    called=[]
    connection=types.SimpleNamespace(close=lambda:None)
    monkeypatch.setenv('CUSTOMER_INGESTION_ENABLED','true')
    monkeypatch.setattr(ingestion,'_connect',lambda:connection)
    monkeypatch.setattr(ingestion,'_process_record',lambda *a: (_ for _ in ()).throw(AssertionError('Lambda ran payload')))
    monkeypatch.setitem(sys.modules,'customer_ingestion_runtime',types.SimpleNamespace(enqueue=lambda c,r,**kw: called.append((c,r,kw)) or {'job_id':'j','status':'QUEUED'}))
    record={'s3':{'bucket':{'name':'test'},'object':{'key':'raw/crm/customer.csv','eTag':'etag'}}}
    result=ingestion.lambda_handler({'Records':[record]},None)
    assert result['results']==[{'job_id':'j','status':'QUEUED'}]
    assert called==[(connection,record,{'stage':'canonical'})]


def test_bulk_snapshot_source_lock_precedes_arrival_snapshot():
    import inspect
    source=inspect.getsource(ingestion._process_record)
    assert "customer-bulk:" in source, 'bulk lock must protect arrival snapshot too'
    assert source.index('customer-bulk:') < source.index('_create_existing_source_snapshot(')


def test_task_request_pins_revision_and_disjoint_runtime_config(monkeypatch):
    for k,v in {'CUSTOMER_INGESTION_CLUSTER':'ingest','CUSTOMER_INGESTION_TASK_DEFINITION':'task:2',
        'CUSTOMER_INGESTION_CONTAINER':'worker','CUSTOMER_INGESTION_SUBNETS':'s1,s2',
        'CUSTOMER_INGESTION_SECURITY_GROUPS':'sg1'}.items():
        monkeypatch.setenv(k,v)
    request=ingestion._ingestion_task_request('attempt')
    assert request['clientToken']==request['startedBy']=='attempt'
    assert request['count']==1
    assert request['overrides']['containerOverrides'][0]['command']==['--attempt-id','attempt']
    monkeypatch.setenv('CUSTOMER_INGESTION_TASK_DEFINITION','task')
    import pytest
    with pytest.raises(ValueError,match='numeric revision'):
        ingestion._ingestion_task_request('attempt')


def test_original_s3_event_routes_to_landing_without_payload(monkeypatch):
    import sys
    import types
    called=[]
    monkeypatch.setenv('CUSTOMER_INGESTION_ENABLED','true')
    monkeypatch.setenv('CUSTOMER_LANDING_ENABLED','true')
    monkeypatch.setattr(ingestion,'_connect',lambda:types.SimpleNamespace(close=lambda:None))
    monkeypatch.setitem(sys.modules,'customer_ingestion_runtime',types.SimpleNamespace(enqueue=lambda c,r,**kw:called.append(kw) or {'status':'QUEUED'}))
    record={'s3':{'bucket':{'name':'test'},'object':{'key':'data_sources/crm/batch/customers/source.csv','eTag':'e'}}}
    ingestion.lambda_handler({'Records':[record]},None)
    assert called==[{'stage':'landing'}]
