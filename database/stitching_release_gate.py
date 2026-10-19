"""Deployment gates for the cross-version direct-dispatch cutover."""
from datetime import datetime, timezone


def verify_activation_gate(lambda_config, connector_definition, running_executions, runner_paused):
    """Fail closed before enabling queue admission; do not terminate writers."""
    env=lambda_config['Environment']['Variables']
    if env.get('STITCHING_ADMISSION_ENABLED') != 'false':
        raise RuntimeError('Admission must remain disabled during direct-caller drain')
    state=connector_definition['States']['RunImportedIdentityStitcher']
    params=state.get('Parameters',{})
    expected=lambda_config.get('FunctionArn')
    if state.get('Resource') != 'arn:aws:states:::lambda:invoke' or not expected or params.get('FunctionName') != expected:
        raise RuntimeError('Connector direct launch route has not been removed')
    payload = {'action': 'stitching_job_status',
               'ingestion_batch_id.$': '$.ingestion_batch_id',
               'stitching_job_id.$': '$.stitching_job_id'}
    if params.get('Payload') != payload:
        raise RuntimeError('Connector payload contract mismatch')
    if (state.get('Type') != 'Task'
            or set(params) - {'FunctionName', 'Payload', 'InvocationType'}
            or params.get('InvocationType', 'RequestResponse') != 'RequestResponse'
            or state.get('InputPath', '$') != '$'
            or state.get('OutputPath', '$') != '$'
            or 'ResultSelector' in state):
        raise RuntimeError('Connector synchronous polling contract mismatch')
    if state.get('Next') != 'CheckStitchingCompletion' or state.get('ResultPath') != '$.stitching_result':
        raise RuntimeError('Connector result contract mismatch')
    states=connector_definition['States'];wait=states.get('WaitForStitchingAdmission',{})
    if wait.get('Next')!='RunImportedIdentityStitcher' or wait.get('Type')!='Wait' or wait.get('Seconds')!=60:
        raise RuntimeError('Connector polling contract mismatch')
    choice=states.get('CheckStitchingCompletion',{})
    if choice.get('Type')!='Choice' or choice.get('Default')!='WaitForStitchingAdmission':
        raise RuntimeError('Connector choice contract mismatch')
    success={'Variable':'$.stitching_result.Payload.status','StringEquals':'SUCCEEDED','Next':'PrepareSuccessResult'}
    if not choice.get('Choices') or choice['Choices'][0]!=success:
        raise RuntimeError('Connector success contract mismatch')
    failures = [
        {'Variable': '$.stitching_result.Payload.status', 'StringEquals': 'FAILED', 'Next': 'PrepareStitchingFailure'},
        {'Variable': '$.stitching_result.Payload.status', 'StringEquals': 'BLOCKED', 'Next': 'PrepareStitchingFailure'},
        {'Variable': '$.stitching_result.Payload.overdue', 'BooleanEquals': True, 'Next': 'PrepareStitchingOverdue'},
    ]
    if choice['Choices'][1:] != failures:
        raise RuntimeError('Connector failure contract mismatch')
    for name, error in [('PrepareStitchingFailure', 'StitchingJobFailed'),
                        ('PrepareStitchingOverdue', 'StitchingCompletionOverdueWorkRetained')]:
        bridge = {'Type': 'Pass', 'Result': {'Error': error},
                  'ResultPath': '$.error', 'Next': 'PrepareFailureResult'}
        if states.get(name) != bridge:
            raise RuntimeError('Connector failure contract mismatch: ' + name)
    if state.get('Catch') != [{'ErrorEquals': ['States.ALL'], 'ResultPath': '$.error',
                               'Next': 'PrepareFailureResult'}]:
        raise RuntimeError('Connector failure contract mismatch: poll exception')
    cutover=lambda_config['LastModified']
    if isinstance(cutover,str):cutover=datetime.fromisoformat(cutover.replace('Z','+00:00'))
    age=(datetime.now(timezone.utc)-cutover).total_seconds()
    if age <= int(lambda_config['Timeout']):
        raise RuntimeError('Old in-flight ingestion invocations may still launch; wait their full configured timeout')
    if running_executions:
        raise RuntimeError('Drain existing direct workflow executions before activation')
    if not runner_paused:
        raise RuntimeError('Old recovery dispatcher has not been paused at a safe boundary')
    return True


def verify_template_scope(before, after, name):
    allowed={'CdpIngestionData':{'LambdaFunctionBF21E41F':{'Code','Environment'}},'CdpDatabaseConnectorRuntime':{'ConnectorStateMachine':{'Definition'},'ConnectorStateMachineRoleDefaultPolicy102C00B5':{'PolicyDocument'}}}
    if not set(before['Resources'])<=set(after['Resources']):
        raise RuntimeError('Resource deletion prohibited')
    for key,value in before['Resources'].items():
        new=after['Resources'][key]
        for field in set(value)|set(new):
            if field not in {'Properties','Metadata'} and value.get(field)!=new.get(field):
                raise RuntimeError('Non-property resource change prohibited: '+key)
        oldprops=value.get('Properties',{});newprops=new.get('Properties',{})
        changed={p for p in set(oldprops)|set(newprops) if oldprops.get(p)!=newprops.get(p)}
        if not changed<=allowed[name].get(key,set()):
            raise RuntimeError('Unexpected property change: '+key)
        if 'Environment' in changed:
            oldenv=oldprops['Environment']['Variables'];newenv=newprops['Environment']['Variables']
            changedenv={p for p in set(oldenv)|set(newenv) if oldenv.get(p)!=newenv.get(p)}
            if not changedenv<={'STITCHING_ADMISSION_ENABLED','STITCHING_ADMISSION_NOT_BEFORE'}:
                raise RuntimeError('Unrelated Lambda environment change prohibited')
