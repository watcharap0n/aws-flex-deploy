"""One-off synthetic-only Aurora core benchmark; no production entrypoint calls."""
import os,sys,json,time,hashlib,secrets,signal,resource,gc,threading,traceback
from pathlib import Path
from datetime import datetime,timezone
import boto3,psycopg2
from psycopg2 import sql
from psycopg2.extensions import make_dsn
import remote_guard as guard
import benchmark_bulk_core as core
ROOT=Path(__file__).resolve().parents[2]
CFG=json.loads(os.environ['BENCHMARK_CONFIG'])
S3=boto3.client('s3');RDS=boto3.client('rds')
RESULT={'scope':'isolated Aurora committed raw core only; NOT full pipeline','rows_per_batch':150000,'overlap_fraction':0.5,'exclusions':['landing','S3 canonical availability','queue','existing_source_snapshot','validation seeding','production fixture cardinality'],'acceptance_1m_under_600s':False,'runs':[],'started_at':datetime.now(timezone.utc).isoformat()}
OUT=ROOT/'results';OUT.mkdir(exist_ok=True)
def persist(name,value):
    data=json.dumps(value,indent=2,default=str).encode();(OUT/name).write_bytes(data)
    key=CFG['prefix']+'results/'+name
    S3.put_object(Bucket=CFG['bucket'],Key=key,Body=data,ServerSideEncryption='AES256')
    if hashlib.sha256(S3.get_object(Bucket=CFG['bucket'],Key=key)['Body'].read()).digest()!=hashlib.sha256(data).digest():raise RuntimeError('Artifact verification failed')
def emit(event,**values):
    print(json.dumps(dict(event=event,at=datetime.now(timezone.utc).isoformat(),**values),default=str),flush=True)
def deadline(signum,frame):raise TimeoutError('Bounded benchmark phase deadline')
signal.signal(signal.SIGALRM,deadline)
def bootstrap():
    c=RDS.describe_db_clusters(DBClusterIdentifier=guard.CLUSTER)['DBClusters'][0]
    params={'host':c['Endpoint'],'port':'5432','dbname':guard.DATABASE,'user':'benchmark_writer','sslmode':'require'}
    guard.validate_target(params,c)
    instance=RDS.describe_db_instances(DBInstanceIdentifier=guard.WRITER)['DBInstances'][0]
    if instance['PubliclyAccessible']:raise ValueError('Public benchmark DB forbidden')
    cred=json.loads(boto3.client('secretsmanager').get_secret_value(SecretId=CFG['secret_arn'])['SecretString'])
    admin=psycopg2.connect(host=params['host'],port=5432,dbname='postgres',user=cred['username'],password=cred['password'],sslmode='require',connect_timeout=10,application_name='synthetic-bench-bootstrap',options='-c statement_timeout=30000 -c lock_timeout=5000')
    admin.autocommit=True
    pwd=secrets.token_urlsafe(32)
    with admin.cursor() as q:
        q.execute('SELECT current_database(),aurora_db_instance_identifier(),pg_is_in_recovery()')
        if q.fetchone()!=('postgres',guard.WRITER,False):raise ValueError('Bootstrap writer identity mismatch')
        q.execute("SELECT 1 FROM pg_roles WHERE rolname='benchmark_writer'")
        if q.fetchone():raise ValueError('Benchmark role already exists; refuse ambiguous repeat')
        q.execute(sql.SQL('CREATE ROLE benchmark_writer LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION CONNECTION LIMIT 5').format(sql.Literal(pwd)))
        q.execute(sql.SQL('CREATE DATABASE {} OWNER benchmark_writer TEMPLATE template0').format(sql.Identifier(guard.DATABASE)))
        q.execute('SELECT datname FROM pg_database WHERE NOT datistemplate')
        for (dbname,) in q.fetchall():
            if dbname!=guard.DATABASE:q.execute(sql.SQL('REVOKE CONNECT ON DATABASE {} FROM PUBLIC').format(sql.Identifier(dbname)))
        q.execute(sql.SQL('REVOKE ALL ON DATABASE {} FROM PUBLIC').format(sql.Identifier(guard.DATABASE)))
    admin.close();del cred
    params.update(password=pwd,connect_timeout='10',application_name='synthetic-bench-core',options='-c statement_timeout=600000 -c lock_timeout=10000 -c idle_in_transaction_session_timeout=120000')
    os.environ['CDP_BULK_CLOUD_DSN']=make_dsn(**params)
    conn=guard.connect()
    with conn.cursor() as q:
        q.execute('SELECT current_database(),aurora_db_instance_identifier(),current_user,version(),current_setting(\'synchronous_commit\')')
        RESULT['authenticated_target']=list(q.fetchone())
        q.execute("SELECT count(*) FROM information_schema.tables WHERE table_schema NOT IN ('pg_catalog','information_schema')")
        if q.fetchone()[0]:raise ValueError('Synthetic database must initially be empty')
    conn.close();RESULT['capacity']={'MinCapacity':8,'MaxCapacity':8,'writer':guard.WRITER,'worker_vcpu':4,'worker_memory_gib':16,'spot':True}
    persist('bootstrap.json',RESULT);emit('bootstrap_verified',writer=guard.WRITER)
def typed(q,table,bid=None):
    excludes="ARRAY['raw_customer_id','snapshot_id','snapshot_recorded_at','created_at','updated_at','ingested_at','batch_id']"
    query='SELECT to_jsonb(t)-'+excludes+' FROM '+table+' t'
    if bid:query+=' WHERE batch_id=%s'
    query+=' ORDER BY source_customer_id'
    q.execute(query,(bid,) if bid else None)
    h=hashlib.sha256();n=0
    for (item,) in q:
        h.update(json.dumps(item,sort_keys=True,ensure_ascii=False,separators=(',',':')).encode());n+=1
    return {'rows':n,'sha256':h.hexdigest()}
def run_mode(mode,run_no,paths,rows):
    c=guard.connect();current={'mode':mode,'run':run_no,'phases':{},'started_at':datetime.now(timezone.utc).isoformat()};RESULT['runs'].append(current)
    handler=core.Timings();writer=core.bulk_module();writer.LOGGER.setLevel('INFO');writer.LOGGER.addHandler(handler)
    baseline=core.load('cloud_baseline',ROOT/'tests/integration/fixtures/customer_bulk_baseline.py')
    parser=core.load('cloud_parser',ROOT/'_lambda/ingestion_data/lambda_function.py')
    a='00000000-0000-4000-8000-000000000001';b='00000000-0000-4000-8000-000000000002'
    start=time.perf_counter()
    try:
        with c.cursor() as q:
            core.reset_schema(q,optimized=mode=='bulk')
            q.execute('INSERT INTO control.ingestion_batch VALUES (%s,1,\'2025-01-01\'),(%s,1,\'2025-01-02\')',(a,b))
        c.commit();current['schema_setup_seconds']=time.perf_counter()-start
        with c.cursor() as q:
            q.execute('SELECT pg_current_wal_insert_lsn()::text');wal=q.fetchone()[0]
        runstart=time.perf_counter()
        for phase,rev in [('A','A'),('B','B'),('replay_B','B')]:
            phase_start=time.perf_counter();v={'started_at':datetime.now(timezone.utc).isoformat()};current['phases'][phase]=v
            emit('phase_start',mode=mode,phase=phase,rows=rows)
            remaining=(datetime.fromisoformat(CFG['deadline'])-datetime.now(timezone.utc)).total_seconds()-120
            if remaining<120:raise TimeoutError('Insufficient remaining benchmark window')
            signal.alarm(int(min(720,remaining)))
            t=time.perf_counter()
            with paths[rev].open('rb') as f:customers,stats=parser.prepare_customer_stream(f)
            v['parse_seconds']=time.perf_counter()-t
            if len(customers)!=rows or stats['records_rejected']:raise ValueError('Parser cardinality mismatch')
            bid=a if rev=='A' else b;handler.messages.clear();c.notices.clear()
            with c.cursor() as q:
                t=time.perf_counter()
                if mode=='baseline':baseline._upsert_customers(q,customers,bid,1)
                else:writer.upsert_customers(q,iter(customers),bid,1)
                v['write_seconds']=time.perf_counter()-t;v['bulk_python']=list(handler.messages)
                v['sql_notices']=[n.strip() for n in c.notices if 'customer_bulk_sql' in n]
                t=time.perf_counter()
                q.execute('INSERT INTO raw.customer_batch_member(ingestion_batch_id,raw_customer_id,source_system_id,source_customer_id) SELECT batch_id,raw_customer_id,source_system_id,source_customer_id FROM raw.customer WHERE batch_id=%s ON CONFLICT DO NOTHING',(bid,))
                v['compatibility_membership_seconds']=time.perf_counter()-t
            t=time.perf_counter();c.commit();v['commit_seconds']=time.perf_counter()-t;v['parse_to_commit_seconds']=time.perf_counter()-phase_start
            t=time.perf_counter()
            with c.cursor() as q:
                expected=sorted(v['source_customer_id'] for v in customers)
                for table,col in [('raw.customer','batch_id'),('raw.customer_batch_member','ingestion_batch_id')]:
                    q.execute('SELECT source_customer_id FROM '+table+' WHERE '+col+'=%s ORDER BY source_customer_id',(bid,))
                    if [r[0] for r in q]!=expected:raise ValueError('Full selected key readback mismatch')
            del customers;gc.collect()
            with c.cursor(name='typed_history_'+phase) as q:v['history_a']=typed(q,'raw.customer',a)
            if phase!='A':
                with c.cursor(name='typed_b_'+phase) as q:v['history_b']=typed(q,'raw.customer',b)
                if v['history_a']!=current['phases']['A']['history_a']:raise ValueError('Historical typed A changed')
            with c.cursor(name='typed_current_'+phase) as q:v['current']=typed(q,'raw.customer_current')
            if phase=='replay_B' and any(v[k]!=current['phases']['B'][k] for k in ['history_a','history_b','current']):raise ValueError('Replay changed typed state')
            with c.cursor() as q:
                v['counts']=list(core.counts(q))
                expected_counts=[rows,rows,rows] if phase=='A' else [rows+rows//2,2*rows,2*rows]
                if v['counts']!=expected_counts:raise ValueError('Exact final count mismatch')
                q.execute('SELECT count(*) FROM raw.customer_current c JOIN raw.customer h USING(raw_customer_id,batch_id) WHERE (to_jsonb(c)-ARRAY[\'updated_at\',\'ingested_at\',\'created_at\']) IS DISTINCT FROM (to_jsonb(h)-ARRAY[\'snapshot_id\',\'snapshot_recorded_at\',\'updated_at\',\'ingested_at\',\'created_at\'])')
                v['current_history_mismatches']=q.fetchone()[0]
                if v['current_history_mismatches']:raise ValueError('Current typed winner mismatch')
            c.commit();v['readback_seconds']=time.perf_counter()-t;v['total_seconds']=time.perf_counter()-phase_start;v['peak_rss_kib']=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss;v['passed']=True
            signal.alarm(0);persist('core-150000.json',RESULT);emit('phase_complete',mode=mode,phase=phase,metrics=v)
        current['wall_seconds']=time.perf_counter()-runstart
        with c.cursor() as q:
            q.execute('SELECT pg_wal_lsn_diff(pg_current_wal_insert_lsn(),%s)::bigint,pg_database_size(current_database())',(wal,));current['cluster_wal_bytes'],current['database_bytes']=q.fetchone()
        current['passed']=True
    except BaseException as e:
        signal.alarm(0);c.rollback();current['error']={'type':type(e).__name__,'pgcode':getattr(e,'pgcode',None),'safe_message':str(e) if isinstance(e,(TimeoutError,ValueError)) else 'Database/runtime error; SQL and payload omitted','elapsed_seconds':time.perf_counter()-start}
        with c.cursor() as q:current['rollback_counts']=list(core.counts(q))
        emit('mode_failed',mode=mode,error=current['error'])
    finally:
        signal.alarm(0);c.close();writer.LOGGER.removeHandler(handler);persist('core-150000.json',RESULT)
    return current.get('passed',False)
def main():
    bootstrap()
    RESULT['hashes']={str(p.relative_to(ROOT)):hashlib.sha256(p.read_bytes()).hexdigest() for p in ROOT.rglob('*') if p.is_file() and p.suffix in {'.py','.sql'} and '__pycache__' not in str(p)}
    rows=150000;paths={r:ROOT/(r+'.csv') for r in ['A','B']}
    t=time.perf_counter();RESULT['fixtures']={r:core.generate(p,rows=rows,start=0 if r=='A' else rows//2,revision=r,padding=256) for r,p in paths.items()};RESULT['fixture_generation_seconds']=time.perf_counter()-t
    persist('core-150000.json',RESULT)
    for no,mode in enumerate(['baseline','bulk','bulk','baseline']):
        if (datetime.fromisoformat(CFG['deadline'])-datetime.now(timezone.utc)).total_seconds()<300:break
        run_mode(mode,no,paths,rows)
        # A failed bulk is a diagnostic result, not grounds to scale to 1M.
        if mode=='bulk' and not RESULT['runs'][-1].get('passed'):break
    passing=[r for r in RESULT['runs'] if r.get('passed')]
    RESULT['all_comparisons_match']=len(passing)==4 and len({json.dumps({p:{k:v[k] for k in ['history_a','current','counts']} for p,v in r['phases'].items()},sort_keys=True) for r in passing})==1
    RESULT['finished_at']=datetime.now(timezone.utc).isoformat();persist('core-150000.json',RESULT);emit('benchmark_finished',passing_runs=len(passing),all_comparisons_match=RESULT['all_comparisons_match'])
if __name__=='__main__':
    try:main()
    except BaseException as e:
        RESULT['fatal']={'type':type(e).__name__,'pgcode':getattr(e,'pgcode',None),'safe_message':str(e) if isinstance(e,(ValueError,TimeoutError)) else 'Exception details suppressed to protect SQL/credentials'}
        persist('core-150000.json',RESULT);emit('fatal',error=RESULT['fatal']);raise SystemExit(1)
