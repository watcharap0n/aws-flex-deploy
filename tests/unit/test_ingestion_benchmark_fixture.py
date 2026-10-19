"""Synthetic benchmark fixture contract (not a throughput result)."""
import csv
import hashlib
import importlib.util
import io
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MODULE = ROOT / 'benchmarks/customer_ingestion/generate_fixture.py'


def load_generator():
    assert MODULE.exists(), 'Streaming deterministic benchmark generator is missing'
    spec = importlib.util.spec_from_file_location('ingestion_fixture', MODULE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_fixture_is_deterministic_and_wide_with_distinct_keys(tmp_path):
    generator = load_generator()
    first = tmp_path / 'first.csv'
    second = tmp_path / 'second.csv'
    one = generator.generate(first, rows=100, start=0, revision='A', padding=256)
    two = generator.generate(second, rows=100, start=0, revision='A', padding=256)
    assert first.read_bytes() == second.read_bytes()
    assert one['sha256'] == two['sha256'] == hashlib.sha256(first.read_bytes()).hexdigest()
    assert one['rows'] == 100 and one['synthetic'] is True
    rows = list(csv.DictReader(io.StringIO(first.read_text(encoding='utf-8-sig'))))
    assert len({row['source_customer_id'] for row in rows}) == 100
    assert len(first.read_bytes()) / 100 > 800
    assert set(row['status'] for row in rows) == {'ACTIVE', 'SUSPENDED'}
    assert all(json.loads(row['attributes'])['synthetic'] for row in rows)


def test_offset_fixture_has_explicit_overlap_and_distinct_versions(tmp_path):
    generator = load_generator()
    first = tmp_path / 'base.csv'
    second = tmp_path / 'update.csv'
    generator.generate(first, rows=100, start=0, revision='A', padding=256)
    generator.generate(second, rows=100, start=50, revision='B', padding=256)
    def indexed(path):
        with path.open(encoding='utf-8-sig', newline='') as stream:
            return {row['source_customer_id']: row for row in csv.DictReader(stream)}
    old, new = indexed(first), indexed(second)
    overlap = set(old) & set(new)
    assert len(overlap) == 50
    assert all(old[key]['email'] != new[key]['email'] for key in overlap)
    assert all(old[key]['phone'] == new[key]['phone'] for key in overlap)


def test_fixture_refuses_overwrite_and_invalid_ranges(tmp_path):
    import pytest
    generator = load_generator()
    target = tmp_path / 'customers.csv'
    target.write_bytes(b'keep')
    with pytest.raises(FileExistsError):
        generator.generate(target, rows=1, start=0, revision='A', padding=256)
    assert target.read_bytes() == b'keep'
    with pytest.raises(ValueError):
        generator.generate(tmp_path / 'bad.csv', rows=0, start=0, revision='A', padding=256)
