"""Isolated runtime synth: no existing ingest/stitcher configs are mutated."""
import importlib.util
from pathlib import Path
import sys

import aws_cdk as cdk
from aws_cdk.assertions import Template
import pytest

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))


def config():
    return dict(image_uri='123456789012.dkr.ecr.ap-southeast-7.amazonaws.com/ingestion@sha256:'+'a'*64,
        cluster_name='existing',db_secret_arn='arn:aws:secretsmanager:ap-southeast-7:123456789012:secret:warehouse-abcdef',
        ingestion_function_arn='arn:aws:lambda:ap-southeast-7:123456789012:function:ingestion',
        ingestion_role_arn='arn:aws:iam::123456789012:role/ingestion',
        bucket_arns=['arn:aws:s3:::customer-bucket'],
        vpc=dict(vpc_id='vpc-test',availability_zones=['ap-southeast-7a'],private_subnet_ids=['subnet-test'],security_group_ids=['sg-test']))


def test_runtime_synth_pins_image_and_limits_warm_capacity(tmp_path):
    assert (ROOT/'cdk_stacks/customer_ingestion_runtime_stack.py').exists(), 'runtime stack missing'
    from cdk_stacks.customer_ingestion_runtime_stack import CustomerIngestionRuntimeStack
    cfg=config()
    cfg['warm_count']=1
    app=cdk.App(outdir=str(tmp_path/'cdk.out'))
    stack=CustomerIngestionRuntimeStack(app,'Runtime',project_config=cfg,env=cdk.Environment(account='123456789012',region='ap-southeast-7'))
    template=Template.from_stack(stack)
    template.has_resource_properties('AWS::ECS::TaskDefinition',{'Cpu':'4096','Memory':'16384'})
    template.has_resource_properties('AWS::ECS::Service',{'DesiredCount':1,'DeploymentConfiguration':{'MaximumPercent':100,'MinimumHealthyPercent':0}})
    template.has_resource_properties('AWS::Events::Rule',{'ScheduleExpression':'rate(1 minute)'})
    template.has_resource_properties('AWS::Events::Rule',{'EventPattern':{'source':['aws.ecs'],'detail-type':['ECS Task State Change'],'detail':{'lastStatus':['STOPPED']}}})
    template.resource_count_is('AWS::Lambda::Function',0)
    template.resource_count_is('AWS::ApplicationAutoScaling::ScalableTarget',0)
    app.synth()


def test_ingress_run_task_grant_survives_task_revision(tmp_path):
    """A new ECS revision must not strand queued customer uploads."""
    from cdk_stacks.customer_ingestion_runtime_stack import CustomerIngestionRuntimeStack
    stack = CustomerIngestionRuntimeStack(
        cdk.App(outdir=str(tmp_path/'out')), 'Runtime', project_config=config(),
        env=cdk.Environment(account='123456789012', region='ap-southeast-7'))
    resources = Template.from_stack(stack).to_json()['Resources']
    policy = next(r['Properties']['PolicyDocument'] for r in resources.values()
                  if r['Type'] == 'AWS::IAM::Policy' and
                  r['Properties'].get('Roles') == ['ingestion'])
    grant = next(s for s in policy['Statement'] if s['Action'] == 'ecs:RunTask')
    resource = grant['Resource']
    assert isinstance(resource, dict) and 'Fn::Join' in resource, resource
    arn_suffix = ''.join(x for x in resource['Fn::Join'][1] if isinstance(x, str))
    assert arn_suffix.endswith('task-definition/RuntimeTask00FCCE15:*'), arn_suffix
    assert grant['Condition']['ArnEquals']['ecs:cluster']


@pytest.mark.parametrize('change',[{'warm_count':9},{'image_uri':'repo:latest'}])
def test_runtime_config_rejects_unbounded_or_unpinned(change,tmp_path):
    assert (ROOT/'cdk_stacks/customer_ingestion_runtime_stack.py').exists(), 'runtime stack missing'
    from cdk_stacks.customer_ingestion_runtime_stack import CustomerIngestionRuntimeStack
    cfg=dict(config(),**change)
    with pytest.raises(ValueError):
        CustomerIngestionRuntimeStack(cdk.App(outdir=str(tmp_path/'out')),'BadRuntime',project_config=cfg)


def test_runtime_only_app_synth_and_image_recipe(tmp_path):
    import os
    import subprocess
    import yaml
    cfg={'aws_region':'ap-southeast-7','aws_account_id':'123456789012',
         'customer_ingestion_runtime_stack':{'stack_name':'CustomerRuntime','project_config':config()}}
    conf=tmp_path/'runtime.yaml'
    conf.write_text(yaml.safe_dump(cfg))
    result=subprocess.run([sys.executable,str(ROOT/'app.py')],cwd=ROOT,env=dict(os.environ,CONFIG_FILE=str(conf),CDK_OUTDIR=str(tmp_path/'synth')),capture_output=True,text=True)
    assert result.returncode==0,result.stderr
    assert (tmp_path/'synth/CustomerRuntime.template.json').exists()
    docker=ROOT/'_workers/customer_ingestion/Dockerfile'
    assert docker.exists(),'runtime image recipe missing'
    recipe=docker.read_text()
    assert 'COPY _lambda/ingestion_data/' in recipe
    assert 'COPY _workers/customer_ingestion/' in recipe
    assert 'USER 10001' in recipe
    assert 'ENTRYPOINT ["python", "worker.py"]' in recipe
    assert 'COPY _lambda/landing_data/' in recipe


def test_landing_enabled_image_permissions_and_config(tmp_path):
    from cdk_stacks.customer_ingestion_runtime_stack import CustomerIngestionRuntimeStack
    cfg=config()
    cfg.update(landing_enabled=True,landing_max_parse_bytes=256*1024**2,
        landing_max_csv_bytes=1024**3,
        landing_max_xlsx_expanded_bytes=512*1024**2,
        landing_role_arn='arn:aws:iam::123456789012:role/landing',
        kms_key_arns=['arn:aws:kms:ap-southeast-7:123456789012:key/example'])
    stack=CustomerIngestionRuntimeStack(cdk.App(outdir=str(tmp_path/'out')),'LandingRuntime',project_config=cfg,
        env=cdk.Environment(account='123456789012',region='ap-southeast-7'))
    text=str(Template.from_stack(stack).to_json())
    assert 's3:PutObject' in text
    assert '/data_sources/*' in text
    assert 'LANDING_MAX_PARSE_BYTES' in text
    assert 'CUSTOMER_LANDING_ENABLED' in text
    assert 'LANDING_MAX_XLSX_EXPANDED_BYTES' in text
    assert 'lambda:InvokeFunction' in text and 'landing' in text
    assert 'kms:GenerateDataKey' in text
    resources=Template.from_stack(stack).to_json()['Resources']
    definition=next(r['Properties'] for r in resources.values() if r['Type']=='AWS::ECS::TaskDefinition')
    environment={e['Name']:e['Value'] for e in definition['ContainerDefinitions'][0]['Environment']}
    assert int(environment['LANDING_MAX_PARSE_BYTES']) > 137*1024**2
    assert int(environment['LANDING_MAX_CSV_BYTES']) == 1024**3
    assert int(environment['LANDING_MAX_XLSX_EXPANDED_BYTES'])==512*1024**2


def test_landing_runtime_csv_cap_is_independent_of_whole_frame_cap(tmp_path):
    from cdk_stacks.customer_ingestion_runtime_stack import CustomerIngestionRuntimeStack
    cfg = config()
    cfg.update(landing_enabled=True, landing_max_parse_bytes=256*1024**2,
        landing_max_csv_bytes=1024**3,
        landing_max_xlsx_expanded_bytes=512*1024**2,
        landing_role_arn='arn:aws:iam::123456789012:role/landing')
    stack = CustomerIngestionRuntimeStack(cdk.App(outdir=str(tmp_path/'out')),
        'CsvRuntime', project_config=cfg,
        env=cdk.Environment(account='123456789012', region='ap-southeast-7'))
    resources = Template.from_stack(stack).to_json()['Resources']
    definition = next(r['Properties'] for r in resources.values() if r['Type'] == 'AWS::ECS::TaskDefinition')
    environment = {e['Name']: e['Value'] for e in definition['ContainerDefinitions'][0]['Environment']}
    assert int(environment['LANDING_MAX_CSV_BYTES']) >= 562 * 1024**2
    assert int(environment['LANDING_MAX_PARSE_BYTES']) == 256 * 1024**2


def test_enabled_landing_requires_explicit_parser_caps(tmp_path):
    from cdk_stacks.customer_ingestion_runtime_stack import CustomerIngestionRuntimeStack
    with pytest.raises(ValueError,match='landing_max_parse_bytes'):
        CustomerIngestionRuntimeStack(cdk.App(outdir=str(tmp_path/'out')),'NoCaps',
            project_config=dict(config(),landing_enabled=True))
