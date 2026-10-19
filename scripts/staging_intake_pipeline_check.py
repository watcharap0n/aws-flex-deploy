"""Read the specific HTTPS intake smoke batch through staging publication."""
import json
import subprocess
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
OUT=ROOT/'outputs/staging-intake-deployment-20261008'


def main():
    smoke=json.loads((OUT/'upload-smoke.json').read_text())
    # Synthetic batch identifiers contain only the landing contract characters.
    batch_ref=smoke['batch_ref']
    assert all(c.isalnum() or c in '-._' for c in batch_ref)
    landing="SELECT batch_id FROM control.source_landing_batch WHERE batch_ref='"+batch_ref+"'"
    ingestion='SELECT batch_id FROM control.ingestion_batch WHERE landing_batch_id IN ('+landing+')'
    sql=f'''SELECT json_build_object('stage','landing','batch_id',batch_id,'status',status,'output_status',output_status,'rows',output_row_count,'error',coalesce(last_error,output_error)) FROM control.source_landing_batch WHERE batch_ref='{batch_ref}';
SELECT json_build_object('stage','ingestion','batch_id',batch_id,'app_id',app_id,'status',status,'received',records_received,'loaded',records_loaded,'rejected',records_rejected,'error',last_error) FROM control.ingestion_batch WHERE batch_id IN ({ingestion});
SELECT json_build_object('stage','stitching','job_id',job_id,'batch_id',ingestion_batch_id,'status',status,'error',error_code,'ecs_task_arn',ecs_task_arn) FROM control.stitching_job WHERE ingestion_batch_id IN ({ingestion});
SELECT json_build_object('stage','publication','batch_id',ingestion_batch_id,'stitching_status',stitching_status,'overall_status',overall_status,'transfer_status',transfer_status,'error',transfer_error_code) FROM control.customer_publication_status WHERE ingestion_batch_id IN ({ingestion});'''
    code=(ROOT/'scripts/staging_credential_alignment_remote.py').read_text().split("if __name__")[0]
    code+='\nprint(pg('+repr(sql)+',db="postgres"))\n'
    code+='print(clickhouse("SELECT count() profiles,countIf(customer_entity_id IS NOT NULL) identified_profiles,countIf(publication_seq IS NOT NULL) published_profiles FROM cdp_customer.customer_profile WHERE app_id=\\\'990008\\\' FORMAT JSON"))\n'
    r=subprocess.run(['ssh','-o','ConnectTimeout=10','-i','/Users/kmac15/Downloads/buzz-mk-automation.pem',
        'ec2-user@ec2-43-210-31-231.ap-southeast-7.compute.amazonaws.com','python3 -'],
        input=code,text=True,capture_output=True,timeout=120)
    if r.returncode:
        raise RuntimeError('Staging pipeline read failed: '+r.stderr[-1200:])
    pg_text,ch_text=r.stdout.split('{\n',1)
    report={'pipeline':[json.loads(x) for x in pg_text.splitlines() if x.startswith('{')],
            'clickhouse':json.loads('{\n'+ch_text)['data']}
    (OUT/'pipeline-smoke.json').write_text(json.dumps(report,indent=2))
    print(json.dumps(report,indent=2))


if __name__=='__main__':
    main()
