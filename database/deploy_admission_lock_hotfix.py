"""One-file, exact-byte hotfix for the currently installed admission Lambda.

Run with --apply only after a RED/PASS real-PG admission/publication-lock test.
Never packages the dirty checkout: downloads and pins the exact live archive,
changes the single lock literal, preserves the original ZIP for rollback.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import re
import time
import urllib.request
import zipfile

import boto3

ACCOUNT = '538783577018'
REGION = 'ap-southeast-7'
NAME = 'cdp-ingestion-data'
BASELINE_FILE_SHA = 'a06712b0787ade2ccb6016f03d023f1482233ca7a798579d48606583e8e5d23b'
OLD = b'ADMISSION_LOCK = 7413220180\n'
NEW = b'ADMISSION_LOCK = 7413220181\n'
BASELINE_ARCHIVE_SHA = '7390d54461eea2fb117e8fdbdc95b8dc0764d2ecd4a9e02dae29cad3abb605b6'


def write_private(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, 'wb') as f:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())


def download_archive(client) -> tuple[dict, bytes]:
    result = client.get_function(FunctionName=NAME)
    cfg = result['Configuration']
    with urllib.request.urlopen(result['Code']['Location'], timeout=30) as response:
        blob = response.read()
    digest = base64.b64encode(hashlib.sha256(blob).digest()).decode()
    if cfg['CodeSha256'] != digest:
        raise RuntimeError('Live Lambda CodeSha256 changed while downloading')
    return cfg, blob


def replace_only_module(blob: bytes) -> bytes:
    if hashlib.sha256(blob).hexdigest() != BASELINE_ARCHIVE_SHA:
        raise RuntimeError('Unknown deployed Lambda archive; no update')
    before = zipfile.ZipFile(io.BytesIO(blob))
    if before.namelist() != ['customer_bulk.py', 'customer_ingestion_runtime.py', 'lambda_function.py', 'stitching_admission.py']:
        raise RuntimeError('Unknown Lambda archive entries')
    source = before.read('stitching_admission.py')
    if hashlib.sha256(source).hexdigest() != BASELINE_FILE_SHA or source.count(OLD) != 1:
        raise RuntimeError('Unknown deployed admission source; no update')
    proposed = source.replace(OLD, NEW)
    compile(proposed, 'stitching_admission.py', 'exec')
    target = io.BytesIO()
    with zipfile.ZipFile(target, 'w') as archive:
        for entry in before.infolist():
            archive.writestr(entry, proposed if entry.filename == 'stitching_admission.py' else before.read(entry.filename))
    out = target.getvalue()
    with zipfile.ZipFile(io.BytesIO(out)) as after:
        if after.namelist() != before.namelist():
            raise RuntimeError('Lambda archive entry list changed')
        for name in before.namelist():
            if after.read(name) != (proposed if name == 'stitching_admission.py' else before.read(name)):
                raise RuntimeError(f'Unexpected archive delta: {name}')
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--dir', type=Path, required=True)
    args = parser.parse_args()
    session = boto3.Session(profile_name='buzz', region_name=REGION)
    if session.client('sts').get_caller_identity()['Account'] != ACCOUNT:
        raise RuntimeError('Wrong AWS account')
    client = session.client('lambda')
    cfg, baseline = download_archive(client)
    if cfg['State'] != 'Active' or cfg['LastUpdateStatus'] != 'Successful':
        raise RuntimeError('Lambda not stable')
    candidate = replace_only_module(baseline)
    release_dir = args.dir.resolve()
    release_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
    os.chmod(release_dir, 0o700)
    write_private(release_dir / 'rollback-live.zip', baseline)
    write_private(release_dir / 'admission-lock-candidate.zip', candidate)
    info = {'account': ACCOUNT, 'region': REGION, 'function': NAME,
            'original_revision': cfg['RevisionId'],
            'original_code_sha256': cfg['CodeSha256'],
            'candidate_code_sha256': base64.b64encode(hashlib.sha256(candidate).digest()).decode(),
            'candidate_module_sha256': hashlib.sha256(zipfile.ZipFile(io.BytesIO(candidate)).read('stitching_admission.py')).hexdigest(),
            'applied': False}
    if args.apply:
        client.update_function_code(FunctionName=NAME, ZipFile=candidate, RevisionId=cfg['RevisionId'], Publish=False)
        updated = None
        for attempt in range(20):
            updated = client.get_function_configuration(FunctionName=NAME)
            if updated['LastUpdateStatus'] in ('Failed', 'Successful'):
                break
            time.sleep(1)
        if updated is None or updated['LastUpdateStatus'] != 'Successful':
            raise RuntimeError('Lambda update incomplete; reconcile CodeSha256 before any retry')
        current, installed = download_archive(client)
        if installed != candidate or current['CodeSha256'] != info['candidate_code_sha256']:
            raise RuntimeError('Lambda readback differs; reconcile before any retry')
        for key in ('Environment', 'Role', 'Timeout', 'MemorySize', 'VpcConfig', 'Handler', 'Runtime'):
            if current.get(key) != cfg.get(key):
                raise RuntimeError(f'Lambda configuration drift after update: {key}')
        info.update(applied=True, installed_revision=current['RevisionId'],
                    installed_code_sha256=current['CodeSha256'],
                    installed_module_sha256=hashlib.sha256(zipfile.ZipFile(io.BytesIO(installed)).read('stitching_admission.py')).hexdigest())
    write_private(release_dir / 'receipt.json', (json.dumps(info, indent=2) + '\n').encode())
    print(json.dumps(info, sort_keys=True))
    print(f'rollback_zip={release_dir / "rollback-live.zip"}')


if __name__ == '__main__':
    main()
