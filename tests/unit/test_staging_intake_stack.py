"""Guard staging upload bindings and preserve the existing landing bucket."""
from copy import deepcopy
from pathlib import Path
import sys
import aws_cdk as cdk
from aws_cdk.assertions import Template
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from cdk_stacks.staging_intake_stack import StagingIntakeStack


def config():
    return yaml.safe_load((ROOT/'env.staging.yaml').read_text())['staging_intake_stack']['project_config']


def test_staging_upload_preserves_bucket_and_isolates_permissions(tmp_path):
    c=config()
    stack=StagingIntakeStack(cdk.App(outdir=str(tmp_path)),'CdpStgIntakeTest',
        project_config=c,env=cdk.Environment(account='538783577018',region='ap-southeast-7'))
    resources=Template.from_stack(stack).to_json()['Resources']
    assert not any(r['Type']=='AWS::S3::Bucket' for r in resources.values())
    funcs=[r['Properties'] for r in resources.values() if r['Type']=='AWS::Lambda::Function']
    upload=next(f for f in funcs if f.get('FunctionName')==c['function_name'])
    assert upload['Environment']['Variables']['BUCKET_NAME']==c['bucket_name']
    for resource in resources.values():
        if resource['Type']!='AWS::IAM::Policy':continue
        for statement in resource['Properties']['PolicyDocument']['Statement']:
            actions=statement['Action']
            if isinstance(actions,str):actions=[actions]
            if any(a.startswith('s3:') for a in actions):
                granted=statement['Resource']
                if isinstance(granted,str):granted=[granted]
                assert all(r.startswith('arn:aws:s3:::'+c['bucket_name']) for r in granted)
    methods=[r['Properties'] for r in resources.values() if r['Type']=='AWS::ApiGateway::Method']
    assert len([m for m in methods if m['HttpMethod']=='POST'])==5
    cors=next(r['Properties'] for r in resources.values() if r['Type']=='AWS::CloudFormation::CustomResource')
    assert cors['BucketName']==c['bucket_name']
    assert cors['CorsRules'][0]['ExposeHeaders']==['ETag']
    cert=next(r['Properties'] for r in resources.values() if r['Type']=='AWS::CertificateManager::Certificate')
    assert cert['DomainName']==c['domain_name']


@pytest.mark.parametrize('field,value',[
    ('bucket_name','cdp-538783577018-ap-southeast-7'),
    ('domain_name','cdp-intake.buzzsprint.dev'),
])
def test_reject_production_upload_bindings(field,value,tmp_path):
    c=deepcopy(config());c[field]=value
    with pytest.raises(ValueError):
        StagingIntakeStack(cdk.App(outdir=str(tmp_path)),'CdpStgIntakeTest',
            project_config=c,env=cdk.Environment(account='538783577018',region='ap-southeast-7'))
