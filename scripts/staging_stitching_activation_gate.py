"""Check isolated staging launchers and database writers before admission."""
import json
import shlex
import subprocess
from datetime import datetime, timezone
from pathlib import Path
import boto3


def main():
    session = boto3.Session(profile_name='buzz', region_name='ap-southeast-7')
    if session.client('sts').get_caller_identity()['Account'] != '538783577018':
        raise RuntimeError('AWS account mismatch')
    cluster = 'cdp-stg-stitcher-cluster'
    fn = session.client('lambda').get_function_configuration(FunctionName='cdp-stg-ingestion-data')
    env = fn['Environment']['Variables']
    assert env['ECS_CLUSTER'] == cluster
    assert env['STITCHING_ADMISSION_ENABLED'] == 'false'
    age = (datetime.now(timezone.utc) - datetime.fromisoformat(fn['LastModified'])).total_seconds()
    assert age > fn['Timeout'], 'disabled Lambda has not drained'
    ecs = session.client('ecs')
    arns = ecs.list_tasks(cluster=cluster)['taskArns']
    tasks = ecs.describe_tasks(cluster=cluster, tasks=arns)['tasks'] if arns else []
    assert not any(c['name'] == 'cdp-stitcher-container' for t in tasks for c in t['containers']), 'stitcher still active'
    cf = session.client('cloudformation')
    outputs = {x['OutputKey']: x['OutputValue'] for x in cf.describe_stacks(
        StackName='CdpStgDatabaseConnectorRuntime')['Stacks'][0]['Outputs']}
    sf = session.client('stepfunctions')
    machine = sf.describe_state_machine(stateMachineArn=outputs['ConnectorStateMachineArn'])
    running = sf.list_executions(stateMachineArn=machine['stateMachineArn'], statusFilter='RUNNING')['executions']
    assert not running, 'connector execution still active'
    states = json.loads(machine['definition'])['States']
    assert states['RunImportedIdentityStitcher']['Parameters']['Payload']['action'] == 'stitching_job_status'
    task = ecs.describe_task_definition(taskDefinition='cdp-stg-stitcher-task')['taskDefinition']['taskDefinitionArn']
    simulation = session.client('iam').simulate_principal_policy(
        PolicySourceArn=machine['roleArn'], ActionNames=['ecs:RunTask'], ResourceArns=[task],
        ContextEntries=[{'ContextKeyName': 'ecs:cluster', 'ContextKeyValues': [
            'arn:aws:ecs:ap-southeast-7:538783577018:cluster/' + cluster], 'ContextKeyType': 'string'}])
    assert all(x['EvalDecision'] != 'allowed' and not x.get('MissingContextValues')
               for x in simulation['EvaluationResults']), 'connector can launch stitching directly'
    remote = 'sudo docker exec -i staging-databases-postgres-1 sh -c ' + shlex.quote(
        'exec psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d postgres -At')
    sql = 'SELECT pg_try_advisory_lock(7413220179),pg_try_advisory_lock(7413220180),pg_try_advisory_lock(7413220181);'
    result = subprocess.run(['ssh', '-i', '/Users/kmac15/Downloads/buzz-mk-automation.pem',
        'ec2-user@ec2-43-210-31-231.ap-southeast-7.compute.amazonaws.com', remote],
        input=sql, text=True, capture_output=True, check=True)
    assert result.stdout.strip() == 't|t|t', 'staging database writer still active'
    report = {'passed': True, 'checked_at': datetime.now(timezone.utc).isoformat(),
        'lambda_revision': fn['RevisionId'], 'stitcher_task_definition': task,
        'connector_executions': 0, 'writer_locks_free': True,
        'direct_connector_stitching_permission': 'denied'}
    Path('outputs/staging-parity-20261005/admission-gate.json').write_text(json.dumps(report, indent=2))
    print(json.dumps(report))


if __name__ == '__main__':
    main()
