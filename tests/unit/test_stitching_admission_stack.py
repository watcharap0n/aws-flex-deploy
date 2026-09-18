"""Infrastructure contracts for shared durable stitching admission."""
import aws_cdk as cdk
from aws_cdk.assertions import Template
from cdk_stacks.lambda_stack import LambdaStack
from cdk_stacks.connector_runtime_stack import ConnectorRuntimeStack
import pytest
from tests.unit.test_connector_runtime_stack import _config


def test_connector_only_requires_admission_arn():
    config=_config();config.pop('stitching_admission_function_arn')
    with pytest.raises(ValueError,match='stitching_admission_function_arn'):
        ConnectorRuntimeStack(cdk.App(),'MissingAdmission',project_config=config,
            env=cdk.Environment(account='111111111111',region='ap-southeast-1'))


def test_connector_waits_for_committed_stitching_completion():
    config=_config()
    config['stitching_admission_function_arn']='arn:aws:lambda:ap-southeast-1:111111111111:function:ingestion'
    stack=ConnectorRuntimeStack(cdk.App(),'AdmissionConnector',project_config=config,
        env=cdk.Environment(account='111111111111',region='ap-southeast-1'))
    resources=Template.from_stack(stack).to_json()['Resources']
    states=next(v['Properties']['Definition']['States'] for v in resources.values() if v['Type']=='AWS::StepFunctions::StateMachine')
    assert states['RunImportedIdentityStitcher']['Resource']=='arn:aws:states:::lambda:invoke'
    assert states['RunImportedIdentityStitcher']['Parameters']['Payload']['action']=='stitching_job_status'
    choice=states['CheckStitchingCompletion']
    assert choice['Default']=='WaitForStitchingAdmission'
    assert choice['Choices'][0]=={'Variable':'$.stitching_result.Payload.status','StringEquals':'SUCCEEDED','Next':'PrepareSuccessResult'}
    assert choice['Choices'][1]['StringEquals']=='FAILED'
    assert states['WaitForStitchingAdmission']['Type']=='Wait'
    assert 'lambda:InvokeFunction' in str(resources)
    run_policies=[v for v in resources.values() if v['Type']=='AWS::IAM::Policy' and 'ecs:RunTask' in str(v)]
    assert all('identity-stitcher:8' not in str(v) for v in run_policies)


def test_ingestion_enabled_synth_requires_checked_release(monkeypatch):
    monkeypatch.delenv('STITCHING_CHECKED_ACTIVATION',raising=False)
    with pytest.raises(ValueError,match='checked activation'):
        LambdaStack(cdk.App(),'UncheckedActivation',project_config={
            'code_from_asset':'./_lambda/ingestion_data','function_handler':'lambda_function.lambda_handler',
            'environments':{'STITCHING_ADMISSION_ENABLED':'true','STITCHING_ADMISSION_NOT_BEFORE':'2026-09-17T00:00:00+07:00','ECS_CLUSTER':'test'},
        },env=cdk.Environment(account='111111111111',region='ap-southeast-1'))


def test_ingestion_has_scheduled_reconciler_and_failure_alarm():
    stack=LambdaStack(cdk.App(),'AdmissionLambda',project_config={
        'lambda_function_name':'test-ingestion','code_from_asset':'_lambda/ingestion_data',
        'function_handler':'lambda_function.lambda_handler','stitching_admission_enabled':True,
        'environments': {'ECS_CLUSTER': 'test-cluster', 'STITCHING_ADMISSION_NOT_BEFORE':'2026-09-17T00:00:00+07:00'},
    },env=cdk.Environment(account='111111111111',region='ap-southeast-1'))
    resources=Template.from_stack(stack).to_json()['Resources']
    assert any(v['Type']=='AWS::Events::Rule' and v['Properties'].get('ScheduleExpression')=='rate(1 minute)' for v in resources.values())
    assert any(v['Type']=='AWS::CloudWatch::Alarm' for v in resources.values())
    assert 'stitching_admission_tick' in str(resources)
    assert 'ecs:ListTasks' in str(resources) and 'ecs:DescribeTasks' in str(resources)
