"""Runtime regression using portable synthetic schema; never contacts AWS."""
import importlib.util
import os
from pathlib import Path
import sys
import uuid

import psycopg2
import pytest
from dataclasses import replace
from psycopg2.extensions import parse_dsn

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize('late_first_arrival', [False, True])
@pytest.mark.parametrize('writer', ['lambda', 'connector'])
def test_runtime_writer_keeps_latest_and_old_history(writer, late_first_arrival):
    from types import SimpleNamespace
    sys.path.insert(0, str(ROOT.parent/'cdp-database-connector-worker/src'))
    sys.path.insert(0, str(ROOT/'_lambda/ingestion_data'))
    spec = importlib.util.spec_from_file_location('history_writer_ingestion', ROOT/'_lambda/ingestion_data/lambda_function.py')
    ingestion = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ingestion)
    target = psycopg2.connect(os.environ['CDP_HISTORY_TEST_DSN'])
    assert target.info.host == '127.0.0.1' and target.info.dbname == 'cdp_history_regression'
    try:
        with target, target.cursor() as q:
            q.execute((ROOT/'tests/integration/fixtures/customer_history_base.sql').read_text())
            q.execute((ROOT/'database/migrations/019_customer_typed_history.sql').read_text())
            q.execute('BEGIN')
            first, second = [str(uuid.uuid4()) for _ in range(2)]
            q.execute("INSERT INTO control.ingestion_batch(batch_id,created_at) VALUES (%s,'2025-01-01'),(%s,'2025-01-02')", (first,second))
            batches = [(second,'new@example.test')] if late_first_arrival else [(first,'old@example.test'),(second,'new@example.test')]
            for batch,email in batches + [(first,'old@example.test')]:
                if writer == 'lambda':
                    customers,_ = ingestion.prepare_customers(f'source_customer_id,email\nkey,{email}\n'.encode())
                    ingestion._upsert_customers(q,customers,batch,1)
                    ingestion._record_batch_members(q,batch,1,1)
                else:
                    from connector_worker.loader import WarehouseLoader
                    loader = object.__new__(WarehouseLoader)
                    q.execute('DROP TABLE IF EXISTS connector_customer_stage; DROP TABLE IF EXISTS connector_customer_selected')
                    loader._create_stage(q)
                    q.execute("INSERT INTO connector_customer_stage(source_row_number,source_customer_id,email,external_ids,attributes,raw_payload) VALUES (1,'key',%s,'{}','{}','{}')",(email,))
                    q.execute('CREATE TEMP TABLE connector_customer_selected AS SELECT * FROM connector_customer_stage')
                    loader._upsert_customers(q,SimpleNamespace(ingestion_batch_id=batch,source_system_id=1))
            q.execute('SELECT batch_id::text,email FROM raw.customer_current')
            assert q.fetchall() == [(second,'new@example.test')]
            q.execute('SELECT batch_id::text,email FROM raw.customer')
            assert dict(q.fetchall()) == {first:'old@example.test',second:'new@example.test'}
    finally:
        target.close()


def test_late_batch_normalizes_historical_values_without_rewinding_identity(monkeypatch):
    sys.path.insert(0, str(ROOT.parent/'fargate-user-stitching/src'))
    sys.path.insert(0, str(ROOT/'_lambda/ingestion_data'))
    from identity_stitcher.config import AppConfig, DatabaseConfig
    from identity_stitcher.standardize import standardize_customers
    spec = importlib.util.spec_from_file_location('history_runtime_ingestion', ROOT/'_lambda/ingestion_data/lambda_function.py')
    ingestion = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(ingestion)
    params = parse_dsn(os.environ['CDP_HISTORY_TEST_DSN'])
    assert params.get('host') == '127.0.0.1' and params.get('dbname') == 'cdp_history_regression'
    target = psycopg2.connect(os.environ['CDP_HISTORY_TEST_DSN'])
    try:
        with target, target.cursor() as q:
            q.execute((ROOT/'tests/integration/fixtures/customer_history_base.sql').read_text())
            q.execute((ROOT/'database/migrations/019_customer_typed_history.sql').read_text())
            first,second=[str(uuid.uuid4()) for _ in range(2)]
            q.execute("INSERT INTO control.ingestion_batch(batch_id,created_at) VALUES (%s,'2025-01-01'),(%s,'2025-01-02')",(first,second))
            for batch,email,status in [(first,'old@example.test','ACTIVE'),(second,'new@example.test','SUSPENDED')]:
                customers,_=ingestion.prepare_customers(f'source_customer_id,first_name,email,status\nkey,Alice,{email},{status}\n'.encode())
                ingestion._upsert_customers(q,customers,batch,1)
                ingestion._record_batch_members(q,batch,1,1)
        config=AppConfig(database=DatabaseConfig(host=target.info.host,port=target.info.port,dbname=target.info.dbname,user=target.info.user,password='',sslmode='disable'))
        assert standardize_customers(target,config,[second])==1
        assert standardize_customers(target,config,[first])==1
        assert standardize_customers(target,config,[first])==1
        with pytest.raises(ValueError, match='Historical normalization retry mismatch'):
            standardize_customers(target,replace(config, normalization_version='changed-version'),[first])
        target.rollback()
        import identity_stitcher.standardize as normalizer
        original = normalizer.standardize_record
        def changed_value(row, cfg):
            item = original(row,cfg)
            item['email_normalized'] = 'drift@example.test'
            return item
        with monkeypatch.context() as patcher:
            patcher.setattr(normalizer,'standardize_record',changed_value)
            with pytest.raises(ValueError, match='Historical normalization retry mismatch'):
                standardize_customers(target,config,[first])
        with target.cursor() as q:
            q.execute('SELECT normalization_version FROM staging.customer_profile')
            assert q.fetchone()[0] == config.normalization_version
            q.execute('SELECT batch_id::text,email_normalized FROM staging.customer_profile_history')
            assert dict(q.fetchall())=={first:'old@example.test',second:'new@example.test'}
            q.execute('SELECT batch_id::text,email_normalized FROM staging.customer_profile')
            assert q.fetchall()==[(second,'new@example.test')]
            q.execute('SELECT count(DISTINCT raw_customer_id),count(*) FROM raw.customer')
            assert q.fetchone()==(1,2)
        from identity_stitcher.validation.repository import load_selected_profiles
        run=str(uuid.uuid4())
        with target, target.cursor() as q:
            q.execute('DROP SCHEMA IF EXISTS quality CASCADE; CREATE SCHEMA quality; CREATE TABLE quality.customer_validation_run(validation_run_id uuid,ingestion_batch_id uuid); CREATE TABLE quality.customer_validation_record(validation_run_id uuid,raw_customer_id uuid,existing_source_record boolean,preexisting_entity_id uuid,disposition text,issue_codes text[],selected_for_canonical boolean,source_row_number int)')
            q.execute('INSERT INTO quality.customer_validation_run VALUES (%s,%s)',(run,first))
            q.execute("INSERT INTO quality.customer_validation_record SELECT %s,raw_customer_id,false,NULL,'ACCEPTED','{}',true,1 FROM raw.customer_current",(run,))
        selected=load_selected_profiles(target,run)
        assert len(selected)==1
        assert selected.iloc[0]['email_normalized']=='old@example.test'
        assert selected.iloc[0]['customer_status']=='ACTIVE'
        with target.cursor() as q:
            q.execute('SELECT standardized_record_id::text FROM staging.customer_profile')
            assert selected.iloc[0]['unique_id']==q.fetchone()[0]
    finally:
        target.close()
