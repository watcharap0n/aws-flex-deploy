import json,pathlib,zipfile,io,hashlib,boto3,sys,time,os,urllib.request,base64

def require(condition,message):
 if not condition:raise RuntimeError(message)
r=pathlib.Path(os.environ['STITCHING_RELEASE_EVIDENCE']);out=r/'cdk.out';s=boto3.Session(profile_name='buzz',region_name='ap-southeast-7');require(s.client('sts').get_caller_identity()['Account'] == '538783577018','Release verification failed')
cf=s.client('cloudformation');s3=s.client('s3')
def attest_lambda():
 function=s.client('lambda').get_function(FunctionName='cdp-ingestion-data');cfg=function['Configuration']
 data=urllib.request.urlopen(function['Code']['Location']).read()
 require(base64.b64encode(hashlib.sha256(data).digest()).decode()==cfg['CodeSha256'],'Deployed package mismatch')
 source={}
 with zipfile.ZipFile(io.BytesIO(data)) as archive:
  for filename in ['lambda_function.py','stitching_admission.py']:
   expected=(pathlib.Path(__file__).resolve().parents[1]/'_lambda/ingestion_data'/filename).read_bytes()
   require(archive.read(filename)==expected,'Live source mismatch')
   source[filename]=hashlib.sha256(expected).hexdigest()
 return cfg,{**{k:cfg[k] for k in ['RevisionId','CodeSha256','LastModified']},'source_sha256':source}

name=sys.argv[1]; require(name in ['CdpIngestionData', 'CdpDatabaseConnectorRuntime'],'Release verification failed')
# Publish exact synthesized assets, including the CDK ingestion package: no hotfix drift.
# Preserve identical resource metadata from the deployed CDK toolchain to avoid
# spurious dependency updates; functional properties remain exact synthesized values.
for stack_name in ['CdpIngestionData','CdpDatabaseConnectorRuntime']:
 path=out/f'{stack_name}.template.json'
 old=json.loads((r/f'before-{stack_name}.json').read_text());new=json.loads(path.read_text())
 for key,value in new['Resources'].items():
  if key in old['Resources']:
   if 'Metadata' in old['Resources'][key]:value['Metadata']=old['Resources'][key]['Metadata']
   else:value.pop('Metadata',None)
 path.write_text(json.dumps(new,indent=2))
assets=json.loads((out/f'{name}.assets.json').read_text())
before=cf.get_template(StackName=name)['TemplateBody'];after=json.loads((out/f'{name}.template.json').read_text())
sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]))
from database.stitching_release_gate import verify_template_scope
verify_template_scope(before,after,name)
allowed_properties={'CdpIngestionData':{'LambdaFunctionBF21E41F':{'Code','Environment'}},'CdpDatabaseConnectorRuntime':{'ConnectorStateMachine':{'Definition'},'ConnectorStateMachineRoleDefaultPolicy102C00B5':{'PolicyDocument'}}}
require(set(before['Resources'])<=set(after['Resources']),'Resource deletion prohibited')
for key,value in before['Resources'].items():
 changed={p for p in set(value.get('Properties',{}))|set(after['Resources'][key].get('Properties',{})) if value.get('Properties',{}).get(p)!=after['Resources'][key].get('Properties',{}).get(p)}
 require(changed<=allowed_properties[name].get(key,set()),'Unexpected property delta '+key+' '+str(changed))
for h,a in assets['files'].items():
 path=out/a['source']['path']
 if a['source']['packaging']=='zip':
  buf=io.BytesIO()
  with zipfile.ZipFile(buf,'w',zipfile.ZIP_DEFLATED) as z:
   for p in sorted(path.rglob('*')):
    if p.is_file():z.write(p,p.relative_to(path))
  data=buf.getvalue()
 else:data=path.read_bytes()
 d=list(a['destinations'].values())[0]
 s3.put_object(Bucket=d['bucketName'],Key=d['objectKey'],Body=data)
 got=s3.get_object(Bucket=d['bucketName'],Key=d['objectKey'])['Body'].read();require(hashlib.sha256(got).digest() == hashlib.sha256(data).digest(),'Release verification failed')
 print('ASSET_VERIFIED',h,len(data),flush=True)
 if a['source']['packaging']=='zip':(r/f'{name}-deployed.zip').write_bytes(data)
change='stitching-admission-'+str(int(time.time()))
stack=cf.describe_stacks(StackName=name)['Stacks'][0]
args=dict(StackName=name,ChangeSetName=change,ChangeSetType='UPDATE',TemplateBody=json.dumps(json.loads((out/f'{name}.template.json').read_text()),separators=(',',':')),Capabilities=['CAPABILITY_NAMED_IAM'],Parameters=[{'ParameterKey':'BootstrapVersion','UsePreviousValue':True}])
if stack.get('RoleARN'):args['RoleARN']=stack['RoleARN']
res=cf.create_change_set(**args);(r/f'{name}-changeset.json').write_text(json.dumps(res,default=str,indent=2));print('CHANGESET',res['Id'],flush=True)
cf.get_waiter('change_set_create_complete').wait(ChangeSetName=res['Id'],WaiterConfig={'Delay':5,'MaxAttempts':60})
changes=cf.describe_change_set(ChangeSetName=res['Id']);(r/f'{name}-changeset-full.json').write_text(json.dumps(changes,default=str,indent=2))
for c in changes['Changes']:
 v=c['ResourceChange'];print('CHANGE',v['Action'],v['LogicalResourceId'],v.get('Replacement'),flush=True)
 require(v.get('Replacement') != 'True','Unexpected replacement')
 if v.get('Replacement') == 'Conditional':
  require(v['ResourceType'] in ['AWS::Lambda::Permission', 'Custom::S3BucketNotifications'],'Release verification failed')
  require(all((d['Evaluation'] == 'Dynamic' for d in v['Details'])),'Only unchanged ARN dependency propagation accepted')
allowed={'CdpIngestionData':{'LambdaFunctionBF21E41F','IAMROLELAMBDADefaultPolicy09646754','StitchingAdmissionErrors41503B11','StitchingAdmissionScheduleAllowEventRuleCdpIngestionDataLambdaFunction0CBEB86EC28E968D','StitchingAdmissionScheduleB65ECB56'},'CdpDatabaseConnectorRuntime':{'ConnectorStateMachine','ConnectorStateMachineRoleDefaultPolicy102C00B5'}}
allowed['CdpIngestionData'].update({'EcsTaskStoppedRuleAllowEventRuleCdpIngestionDataLambdaFunction0CBEB86EDC0D2D29','EcsTaskStoppedRuleD9812177','S3TriggerBucketAllowBucketNotificationsToCdpIngestionDataLambdaFunction0CBEB86E6552CBCB','S3TriggerBucketNotifications0399D6BE'})
for change in changes['Changes']:
 rc=change['ResourceChange'];key=rc['LogicalResourceId']
 if key not in allowed[name]:
  require(rc['Action']=='Modify' and before['Resources'][key]['Properties']==after['Resources'][key]['Properties'],'Unexpected resource change')
  require(all(d.get('Evaluation')=='Dynamic' and d.get('CausingEntity')=='ConnectorStateMachine.Arn' for d in rc['Details']),'Only verified unchanged connector ARN dependencies permitted')
if '--execute' not in sys.argv:print('CHANGESET_PREPARED_NOT_EXECUTED');sys.exit(0)
if name=='CdpIngestionData':
 template=json.loads((out/f'{name}.template.json').read_text())
 if template['Resources']['LambdaFunctionBF21E41F']['Properties']['Environment']['Variables'].get('STITCHING_ADMISSION_ENABLED')=='true':
  import subprocess
  sys.path.insert(0,str(pathlib.Path(__file__).resolve().parents[1]))
  from database.stitching_release_gate import verify_activation_gate
  current,attestation=attest_lambda()
  require(attestation==json.loads((r/'disabled-lambda-attestation.json').read_text()),'Disabled queue-only code/revision changed')
  sf=s.client('stepfunctions');arn='arn:aws:states:ap-southeast-7:538783577018:stateMachine:ConnectorStateMachine-fGY0NCdcUNlL'
  machine=sf.describe_state_machine(stateMachineArn=arn)
  sm=json.loads(machine['definition'])
  task='arn:aws:ecs:ap-southeast-7:538783577018:task-definition/cdp-stitcher-task:9'
  simulation=s.client('iam').simulate_principal_policy(PolicySourceArn=machine['roleArn'],ActionNames=['ecs:RunTask'],ResourceArns=[task],ContextEntries=[{'ContextKeyName':'ecs:cluster','ContextKeyValues':['arn:aws:ecs:ap-southeast-7:538783577018:cluster/cdp-stitcher-cluster'],'ContextKeyType':'string'}])
  require(all((x['EvalDecision'] != 'allowed' and (not x.get('MissingContextValues')) for x in simulation['EvaluationResults'])),'Connector still has effective direct RunTask permission')
  (r/'activation-iam-simulation.json').write_text(json.dumps(simulation,default=str,indent=2))
  running=[]
  for page in sf.get_paginator('list_executions').paginate(stateMachineArn=arn,statusFilter='RUNNING'):running.extend(page['executions'])
  pid=os.environ['STITCHING_PAUSED_RUNNER_PID'];pause=json.loads((r/'runner-pause.json').read_text())
  identity=subprocess.check_output(['ps','-p',pid,'-o','lstart=','-o','command='],text=True).strip()
  require(int(pid)==pause['parent_pid'] and identity==pause['process_identity'] and 'cdp_recover_all.py' in identity,'Paused runner identity mismatch')
  paused=subprocess.check_output(['ps','-p',pid,'-o','state='],text=True).strip().startswith('T')
  require(verify_activation_gate(current, sm, running, paused),'Release verification failed')
  (r/'activation-gate.json').write_text(json.dumps({'lambda_revision':current['RevisionId'],'last_modified':current['LastModified'],'checked_at':time.time(),'running_executions':len(running),'runner_paused':paused},indent=2))
cf.execute_change_set(ChangeSetName=res['Id']);print('EXECUTED',flush=True)
cf.get_waiter('stack_update_complete').wait(StackName=name,WaiterConfig={'Delay':10,'MaxAttempts':90})
live=cf.get_template(StackName=name)['TemplateBody'];new=json.loads((out/f'{name}.template.json').read_text());require(live['Resources'] == new['Resources'],'Release verification failed')
(r/f'{name}-deployed-template.json').write_text(json.dumps(live,indent=2));print('DEPLOYED_TEMPLATE_VERIFIED',name,flush=True)
if name=='CdpIngestionData':
 config,attestation=attest_lambda()
 if config['Environment']['Variables'].get('STITCHING_ADMISSION_ENABLED')=='false':
  (r/'disabled-lambda-attestation.json').write_text(json.dumps(attestation,indent=2))
