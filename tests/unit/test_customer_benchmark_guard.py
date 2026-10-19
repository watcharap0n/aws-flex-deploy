import importlib.util
from pathlib import Path
import pytest
PATH=Path(__file__).resolve().parents[2]/'benchmarks/customer_ingestion/remote_guard.py'
def module():
    assert PATH.exists(), 'remote benchmark guard must exist'
    s=importlib.util.spec_from_file_location('remote_guard',PATH);m=importlib.util.module_from_spec(s);s.loader.exec_module(m);return m

def test_accepts_exact_owned_clone_only():
    m=module()
    p={'host':'ingest-bench-20260918.cluster-cbswkuo2sorn.ap-southeast-7.rds.amazonaws.com','port':'5432','dbname':'cdp_bulk_cloud_20260918','sslmode':'require','user':'benchmark_writer'}
    c={'DBClusterIdentifier':'ingest-bench-20260918','DBClusterArn':'arn:aws:rds:ap-southeast-7:538783577018:cluster:ingest-bench-20260918','Endpoint':p['host'],'ServerlessV2ScalingConfiguration':{'MinCapacity':8,'MaxCapacity':8},'DBClusterMembers':[{'DBInstanceIdentifier':'ingest-bench-20260918-writer','IsClusterWriter':True}]}
    assert m.validate_target(p,c)==p
    for key,value in [('host','buzz-marketing-automation-db.cluster-cbswkuo2sorn.ap-southeast-7.rds.amazonaws.com'),('dbname','postgres'),('dbname','elasticsearch'),('sslmode','disable'),('user','postgres'),('hostaddr','172.31.38.169'),('service','production')]:
        bad=dict(p);bad[key]=value
        with pytest.raises(ValueError):m.validate_target(bad,c)
    for key,value in [('DBClusterIdentifier','buzz-marketing-automation-db'),('DBClusterArn','arn:aws:rds:ap-southeast-7:538783577018:cluster:buzz-marketing-automation-db'),('ServerlessV2ScalingConfiguration',{'MinCapacity':4,'MaxCapacity':16}),('DBClusterMembers',[{'DBInstanceIdentifier':'buzz-marketing-automation-db-reader-1','IsClusterWriter':True}])]:
        bad=dict(c);bad[key]=value
        with pytest.raises(ValueError):m.validate_target(p,bad)
