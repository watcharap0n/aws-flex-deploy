from datetime import datetime,timezone,timedelta
import pytest
from database.stitching_release_gate import verify_activation_gate


@pytest.fixture
def cutover():
    arn = 'arn:aws:lambda:ap-southeast-7:111111111111:function:ingestion'
    config = {'FunctionArn': arn, 'Environment': {'Variables': {
        'STITCHING_ADMISSION_ENABLED': 'false'}}, 'Timeout': 900,
        'LastModified': datetime.now(timezone.utc) - timedelta(seconds=901)}
    states = {
        'RunImportedIdentityStitcher': {
            'Type': 'Task', 'Resource': 'arn:aws:states:::lambda:invoke',
            'Parameters': {'FunctionName': arn, 'Payload': {
                'action': 'stitching_job_status',
                'ingestion_batch_id.$': '$.ingestion_batch_id',
                'stitching_job_id.$': '$.stitching_job_id'}},
            'Next': 'CheckStitchingCompletion', 'ResultPath': '$.stitching_result',
            'Catch': [{'ErrorEquals': ['States.ALL'], 'ResultPath': '$.error',
                       'Next': 'PrepareFailureResult'}]},
        'WaitForStitchingAdmission': {'Type': 'Wait', 'Seconds': 60,
                                    'Next': 'RunImportedIdentityStitcher'},
        'CheckStitchingCompletion': {'Type': 'Choice', 'Default': 'WaitForStitchingAdmission',
            'Choices': [
                {'Variable': '$.stitching_result.Payload.status', 'StringEquals': 'SUCCEEDED', 'Next': 'PrepareSuccessResult'},
                {'Variable': '$.stitching_result.Payload.status', 'StringEquals': 'FAILED', 'Next': 'PrepareStitchingFailure'},
                {'Variable': '$.stitching_result.Payload.status', 'StringEquals': 'BLOCKED', 'Next': 'PrepareStitchingFailure'},
                {'Variable': '$.stitching_result.Payload.overdue', 'BooleanEquals': True, 'Next': 'PrepareStitchingOverdue'}]},
        'PrepareStitchingFailure': {'Type': 'Pass', 'Result': {'Error': 'StitchingJobFailed'},
                                   'ResultPath': '$.error', 'Next': 'PrepareFailureResult'},
        'PrepareStitchingOverdue': {'Type': 'Pass', 'Result': {'Error': 'StitchingCompletionOverdueWorkRetained'},
                                   'ResultPath': '$.error', 'Next': 'PrepareFailureResult'},
    }
    return config, {'States': states}


def test_cutover_fences_old_direct_callers(cutover):
    config, definition = cutover
    direct = {'States': {'RunImportedIdentityStitcher': {'Resource': 'arn:aws:states:::ecs:runTask.sync'}}}
    with pytest.raises(RuntimeError, match='direct launch'):
        verify_activation_gate(config, direct, [], True)
    config['LastModified'] = datetime.now(timezone.utc)
    with pytest.raises(RuntimeError, match='Old in-flight'):
        verify_activation_gate(config, definition, [], True)
    config['LastModified'] -= timedelta(seconds=901)
    with pytest.raises(RuntimeError, match='Drain'):
        verify_activation_gate(config, definition, [{}], True)
    with pytest.raises(RuntimeError, match='recovery'):
        verify_activation_gate(config, definition, [], False)
    assert verify_activation_gate(config, definition, [], True)


@pytest.mark.parametrize('field', ['ingestion_batch_id.$', 'stitching_job_id.$'])
@pytest.mark.parametrize('mutation', ['missing', 'wrong_path'])
def test_poll_payload_requires_exact_lineage(cutover, field, mutation):
    config, definition = cutover
    payload = definition['States']['RunImportedIdentityStitcher']['Parameters']['Payload']
    if mutation == 'missing':
        payload.pop(field)
    else:
        payload[field] = '$.other_job'
    with pytest.raises(RuntimeError, match='payload contract'):
        verify_activation_gate(config, definition, [], True)


@pytest.mark.parametrize('mutation', ['async', 'wrong_function', 'missing_function_arn', 'input_path', 'result_selector', 'output_path'])
def test_poll_requires_synchronous_unmodified_lambda_result(cutover, mutation):
    config, definition = cutover
    state = definition['States']['RunImportedIdentityStitcher']
    if mutation == 'async':
        state['Parameters']['InvocationType'] = 'Event'
    elif mutation == 'wrong_function':
        state['Parameters']['FunctionName'] = 'other'
    elif mutation == 'missing_function_arn':
        config.pop('FunctionArn')
    else:
        state[{'input_path': 'InputPath', 'result_selector': 'ResultSelector', 'output_path': 'OutputPath'}[mutation]] = '$.other'
    with pytest.raises(RuntimeError):
        verify_activation_gate(config, definition, [], True)


@pytest.mark.parametrize('index', [1, 2, 3])
@pytest.mark.parametrize('mutation', ['missing', 'success_target', 'wrong_condition'])
def test_terminal_failures_cannot_be_hidden_or_reported_as_success(cutover, index, mutation):
    config, definition = cutover
    choices = definition['States']['CheckStitchingCompletion']['Choices']
    if mutation == 'missing':
        choices.pop(index)
    elif mutation == 'success_target':
        choices[index]['Next'] = 'PrepareSuccessResult'
    else:
        choices[index]['Variable'] = '$.other'
    with pytest.raises(RuntimeError, match='failure contract'):
        verify_activation_gate(config, definition, [], True)


@pytest.mark.parametrize('name', ['PrepareStitchingFailure', 'PrepareStitchingOverdue'])
@pytest.mark.parametrize('mutation', ['missing', 'success_target', 'wrong_error'])
def test_failure_bridges_preserve_failure_result(cutover, name, mutation):
    config, definition = cutover
    states = definition['States']
    if mutation == 'missing':
        states.pop(name)
    elif mutation == 'success_target':
        states[name]['Next'] = 'PrepareSuccessResult'
    else:
        states[name]['ResultPath'] = '$.ignored'
    with pytest.raises(RuntimeError, match='failure contract'):
        verify_activation_gate(config, definition, [], True)


@pytest.mark.parametrize('mutation', ['missing', 'success_target'])
def test_poll_exception_uses_existing_failure_publisher(cutover, mutation):
    config, definition = cutover
    state = definition['States']['RunImportedIdentityStitcher']
    if mutation == 'missing':
        state.pop('Catch')
    else:
        state['Catch'][0]['Next'] = 'PrepareSuccessResult'
    with pytest.raises(RuntimeError, match='failure contract'):
        verify_activation_gate(config, definition, [], True)


@pytest.mark.parametrize('field,value',[('Type','AWS::S3::Bucket'),('Condition','other'),('DeletionPolicy','Delete')])
def test_scope_rejects_non_property_mutation(field,value):
    import copy
    from database.stitching_release_gate import verify_template_scope
    before={'Resources':{'LambdaFunctionBF21E41F':{'Type':'AWS::Lambda::Function','Properties':{}}}}
    after=copy.deepcopy(before);after['Resources']['LambdaFunctionBF21E41F'][field]=value
    with pytest.raises(RuntimeError,match='Non-property'):verify_template_scope(before,after,'CdpIngestionData')


def test_scope_rejects_delete_and_unrelated_environment():
    import copy
    from database.stitching_release_gate import verify_template_scope
    before={'Resources':{'LambdaFunctionBF21E41F':{'Type':'AWS::Lambda::Function','Properties':{'Environment':{'Variables':{'DB_SECRET_ID':'old'}}}}}}
    with pytest.raises(RuntimeError,match='deletion'):verify_template_scope(before,{'Resources':{}},'CdpIngestionData')
    after=copy.deepcopy(before);after['Resources']['LambdaFunctionBF21E41F']['Properties']['Environment']['Variables']['DB_SECRET_ID']='new'
    with pytest.raises(RuntimeError,match='Unrelated'):verify_template_scope(before,after,'CdpIngestionData')


def test_optimized_python_keeps_scope_guard():
    import subprocess,sys
    result=subprocess.run([sys.executable,'-O','-c',"from database.stitching_release_gate import verify_template_scope; verify_template_scope({'Resources':{'x':{}}},{'Resources':{}},'CdpIngestionData')"],capture_output=True,text=True)
    if result.returncode==0 or 'Resource deletion prohibited' not in result.stderr:
        pytest.fail('Optimized Python bypassed explicit release guard')
