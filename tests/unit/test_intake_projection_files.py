"""Landing reads uploads from a SpooledTemporaryFile, which on the Lambda's Python 3.10 lacks seekable()."""
import csv
import io
import sys
import tempfile
from pathlib import Path

import pytest
from openpyxl import Workbook

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "_lambda" / "landing_data"))
from intake_projection import iter_rows  # noqa: E402


def xlsx_bytes(rows):
    book = Workbook()
    for row in rows:
        book.active.append(row)
    out = io.BytesIO()
    book.save(out)
    return out.getvalue()


def csv_bytes(rows):
    out = io.StringIO()
    csv.writer(out).writerows(rows)
    return out.getvalue().encode()


# max_size 1 spills to a real file at once; the large size stays in memory. Landing can hit either.
@pytest.mark.parametrize("max_size", [1, 10_000_000])
@pytest.mark.parametrize("file_format,build", [("xlsx", xlsx_bytes), ("csv", csv_bytes)])
def test_uploads_are_readable_from_a_spooled_file(file_format, build, max_size):
    content = tempfile.SpooledTemporaryFile(max_size=max_size)
    content.write(build([["customer_id", "full_name"], ["1", "สมชาย ใจดี"], ["2", "Ann Lee"]]))

    headers, rows = iter_rows(content, file_format)

    assert headers == ["customer_id", "full_name"]
    assert [r["full_name"] for r in rows] == ["สมชาย ใจดี", "Ann Lee"]
