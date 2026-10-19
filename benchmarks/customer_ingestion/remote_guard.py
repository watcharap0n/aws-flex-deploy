"""Fail-closed guard for this exact ephemeral AWS benchmark, never production.

The runtime calls AWS DescribeDBClusters and authenticates the writer identity
before exposing a cursor to reset_schema. Not enabled by a hostname-only flag.
"""
CLUSTER='ingest-bench-20260918'
ARN='arn:aws:rds:ap-southeast-7:538783577018:cluster:'+CLUSTER
WRITER=CLUSTER+'-writer'
DATABASE='cdp_bulk_cloud_20260918'

def validate_target(params, cluster):
    expected={'host':cluster.get('Endpoint'),'port':'5432','dbname':DATABASE,'user':'benchmark_writer','sslmode':'require'}
    if any(params.get(k)!=v for k,v in expected.items()) or not expected['host']:
        raise ValueError('Remote benchmark connection is not the exact isolated target')
    if set(params)-set(expected)-{'password','connect_timeout','application_name','options'}:
        raise ValueError('Alternate connection route is forbidden')
    if (cluster.get('DBClusterIdentifier')!=CLUSTER or cluster.get('DBClusterArn')!=ARN
        or cluster.get('ServerlessV2ScalingConfiguration')!={'MinCapacity':8,'MaxCapacity':8}
        or [(m['DBInstanceIdentifier'],m['IsClusterWriter']) for m in cluster.get('DBClusterMembers',[])]!=[(WRITER,True)]):
        raise ValueError('AWS target must be the owned single-writer fixed-8-ACU clone')
    if not expected['host'].startswith(CLUSTER+'.cluster-'):
        raise ValueError('Unexpected clone endpoint')
    return params

def connect():
    import os,json,boto3,psycopg2
    from psycopg2.extensions import parse_dsn
    params=parse_dsn(os.environ['CDP_BULK_CLOUD_DSN'])
    c=boto3.client('rds',region_name='ap-southeast-7').describe_db_clusters(DBClusterIdentifier=CLUSTER)['DBClusters'][0]
    validate_target(params,c)
    connection=psycopg2.connect(**params)
    try:
        with connection.cursor() as q:
            q.execute('SELECT current_database(),aurora_db_instance_identifier(),pg_is_in_recovery(),current_user')
            if q.fetchone()!=(DATABASE,WRITER,False,'benchmark_writer'):
                raise ValueError('Authenticated writer identity mismatch')
        connection.commit()
        return connection
    except BaseException:
        connection.close();raise
