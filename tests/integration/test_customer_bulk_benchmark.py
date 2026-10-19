"""Bounded harness smoke test; full test module owns the disposable DB."""
from pathlib import Path
import subprocess
import sys
import json
import shutil
import pytest

ROOT = Path(__file__).resolve().parents[2]


def test_core_benchmark_reports_committed_equal_outputs(tmp_path):
    script = ROOT / 'benchmarks/customer_ingestion/benchmark_bulk_core.py'
    assert script.exists(), 'Comparable committed core benchmark is missing'
    if shutil.disk_usage(ROOT).free < 512 * 1024**2 + 12 * 40000:
        pytest.skip('Local disk below benchmark safety reserve; no acceptance measurement')
    output = tmp_path / 'result.json'
    subprocess.run([sys.executable, str(script), '--rows', '12', '--output', str(output)],
                   cwd=ROOT, check=True, timeout=90)
    result = json.loads(output.read_text())
    assert result['acceptance_1m_under_600s'] is False
    assert result['canonical_s3_clock_measured'] is False
    assert [r['mode'] for r in result['runs']] == ['baseline', 'bulk', 'bulk', 'baseline']
    assert len({r['typed_digest'] for r in result['runs']}) == 1
    for run in result['runs']:
        assert run['counts'] == [18, 24, 24]
        assert run['commit_seconds'] >= 0
        assert run['phases']['A']['write_seconds'] > 0
