"""Exercise staging HTTPS multipart upload and browser CORS without logging signed URLs."""
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import boto3
import requests

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT/'outputs/staging-intake-deployment-20261008'
BASE = 'https://cdp-intake-dev.buzzsprint.dev'
BUCKET = 'cdp-stg-538783577018-ap-southeast-7'
ORIGIN = 'https://staging-upload-test.buzzsprint.dev'


def http(method, url, **kwargs):
    try:
        r = requests.request(method, url, timeout=30, **kwargs)
    except requests.RequestException as error:
        raise RuntimeError('HTTP request failed: '+type(error).__name__) from None
    if not 200 <= r.status_code < 300:
        # Never echo a presigned URL or its response request metadata.
        raise RuntimeError(f'{method} returned HTTP {r.status_code}')
    return r


def post(action, payload):
    return http('POST',BASE+'/multipart-upload/'+action,json=payload,
                headers={'Origin':ORIGIN}).json()


def main():
    s = boto3.Session(profile_name='buzz',region_name='ap-southeast-7')
    assert s.client('sts').get_caller_identity()['Account']=='538783577018'
    s3 = s.client('s3')
    token=uuid4().hex[:12]
    now=datetime.now(timezone.utc)
    source='intake_smoke_'+token
    app='990008'
    ref=now.strftime('%Y-%m-%d-%H%M%S')+'-'+app+'-'+token
    key=f'data_sources/{source}/{ref}/customers/customer.csv'
    content=('source_customer_id,first_name,last_name,email,phone\n'
             f'intake-{token},Staging,Intake,staging-{token}@example.invalid,+66900000008\n').encode()
    report={'base_url':BASE,'bucket':BUCKET,'source_code':source,'batch_ref':ref,
            'app_id':app,'key':key,'started_at':now.isoformat()}
    preflight=http('OPTIONS',BASE+'/multipart-upload/initiate',headers={
        'Origin':ORIGIN,'Access-Control-Request-Method':'POST',
        'Access-Control-Request-Headers':'content-type'})
    assert preflight.headers.get('Access-Control-Allow-Origin')=='*'
    assert 'POST' in preflight.headers.get('Access-Control-Allow-Methods','')
    report['api_cors']=True
    initiated=post('initiate',{'key':key,'contentType':'text/csv'})
    assert initiated['bucket']==BUCKET and initiated['key']==key
    upload_id=initiated['uploadId']
    completed=False
    try:
        signed=post('presign-part',{'key':key,'uploadId':upload_id,'partNumber':1})
        assert signed['partNumber']==1
        cors=http('OPTIONS',signed['url'],headers={
            'Origin':ORIGIN,'Access-Control-Request-Method':'PUT',
            'Access-Control-Request-Headers':'content-type'})
        assert cors.headers.get('Access-Control-Allow-Origin')=='*'
        assert 'etag' in cors.headers.get('Access-Control-Expose-Headers','').lower()
        report['s3_cors']=True
        put=http('PUT',signed['url'],data=content,headers={'Content-Type':'text/csv','Origin':ORIGIN})
        etag=put.headers['ETag']
        assert 'etag' in put.headers.get('Access-Control-Expose-Headers','').lower()
        parts=post('list-parts',{'key':key,'uploadId':upload_id})['parts']
        assert len(parts)==1 and parts[0]['size']==len(content) and parts[0]['etag']==etag
        done=post('complete',{'key':key,'uploadId':upload_id,
            'parts':[{'partNumber':1,'etag':etag}]})
        completed=True
        assert done['bucket']==BUCKET and done['key']==key
        actual=s3.get_object(Bucket=BUCKET,Key=key)['Body'].read()
        assert actual==content
        report['multipart_upload']=True
        report['bytes']=len(content)
        report['sha256']=hashlib.sha256(actual).hexdigest()
    finally:
        if not completed:
            post('abort',{'key':key,'uploadId':upload_id})
    abort_key=f'intake-smoke/abort-{token}.csv'
    abort=post('initiate',{'key':abort_key,'contentType':'text/csv'})
    post('abort',{'key':abort_key,'uploadId':abort['uploadId']})
    remaining=s3.list_multipart_uploads(Bucket=BUCKET,Prefix=abort_key).get('Uploads',[])
    assert not remaining
    report['abort']=True
    OUT.mkdir(exist_ok=True,parents=True)
    (OUT/'upload-smoke.json').write_text(json.dumps(report,indent=2))
    print(json.dumps(report,indent=2))


if __name__=='__main__':
    main()
