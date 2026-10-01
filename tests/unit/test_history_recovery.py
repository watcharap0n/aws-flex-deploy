"""Offline recovery preflight tests; synthetic data only."""
import importlib.util
from pathlib import Path
import sys
import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts'))


def test_checksum_mismatch_is_rejected_before_parser():
    import recover_customer_history as recovery
    with pytest.raises(recovery.RecoveryError, match='ARTIFACT_CHECKSUM'):
        recovery.parse_artifact(b'synthetic', '0' * 64, 1)


@pytest.mark.parametrize('body,count',[
    (b'source_customer_id\nk\nk\n',1),
    (b'source_customer_id,date_of_birth\nk,not-a-date\n',0),
    (b'source_customer_id\nk\n',2),
])
def test_rejects_duplicate_invalid_and_count_mismatch(body,count):
    import hashlib
    import recover_customer_history as recovery
    with pytest.raises(recovery.RecoveryError,match='ARTIFACT_ROW_COUNT'):
        recovery.parse_artifact(body,hashlib.sha256(body).hexdigest(),count)


def test_pinned_parser_types_and_hash():
    import datetime
    import hashlib
    import recover_customer_history as recovery
    body=b'source_customer_id,email,date_of_birth,source_created_at,age\nk,UPPER@EXAMPLE.TEST,2001-02-03,2020-01-01T07:00:00+07:00,25\n'
    row=recovery.parse_artifact(body,hashlib.sha256(body).hexdigest(),1)[0]
    assert row['date_of_birth']==datetime.date(2001,2,3)
    assert row['email']=='upper@example.test'
    assert row['age']==25
    assert row['source_created_at'].hour==0
    assert recovery.PARSER_SHA256==hashlib.sha256((ROOT/'scripts/history_csv_parser_v1.py').read_bytes()).hexdigest()


def test_duplicate_headers_fail_closed():
    import hashlib
    import recover_customer_history as recovery
    body=b'source_customer_id,email,email\nk,first,last\n'
    with pytest.raises(recovery.RecoveryError,match='ARTIFACT_HEADER'):
        recovery.parse_artifact(body,hashlib.sha256(body).hexdigest(),1)


def test_cli_help_is_available_without_credentials():
    import subprocess
    result=subprocess.run([sys.executable,str(ROOT/'scripts/recover_customer_history.py'),'--help'],capture_output=True,text=True)
    assert result.returncode==0
    assert '--artifact-version-id' in result.stdout
    assert '--apply' in result.stdout
