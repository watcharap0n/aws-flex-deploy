"""CDK entry point for deploying only intake from env.staging.yaml."""
from pathlib import Path
import sys
import aws_cdk as cdk
import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from cdk_stacks.staging_intake_stack import StagingIntakeStack

config=yaml.safe_load((ROOT/'env.staging.yaml').read_text())
intake=config['staging_intake_stack']
app=cdk.App()
StagingIntakeStack(app,intake['stack_name'],project_config=intake['project_config'],
    env=cdk.Environment(account=config['aws_account_id'],region=config['aws_region']),
    tags={'project':config['project_tag'],'environment':config['environment_tag']})
app.synth()
