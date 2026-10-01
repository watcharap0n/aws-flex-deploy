"""Admission and ClickHouse publication must not share a session lock."""

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
# Contract of the separately deployed identity_stitcher.publication worker.
PUBLICATION_LOCK_ID = 7_413_220_180
WRITER_LOCK_ID = 7_413_220_179


def test_admission_lock_is_not_the_publisher_or_writer_lock():
    path = ROOT / "_lambda/ingestion_data/stitching_admission.py"
    spec = importlib.util.spec_from_file_location("admission_lock_contract", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    assert module.ADMISSION_LOCK not in {PUBLICATION_LOCK_ID, WRITER_LOCK_ID}
    assert module.WRITER_LOCK == WRITER_LOCK_ID
