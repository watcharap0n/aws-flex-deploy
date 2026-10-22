"""Bounded transport helpers for the landing Lambda and callable ECS worker.

No database schema or job/lease policy lives here. COPY uses the existing typed
relation and all of its constraints; its caller owns delete/insert/complete commit.
"""
from __future__ import annotations

import json


def _copy_text(value):
    if value is None:
        return r'\N'
    return (str(value).replace('\\', '\\\\').replace('\t', '\\t')
            .replace('\n', '\\n').replace('\r', '\\r'))


class EvidenceCopyStream:
    """A one-pass COPY FROM stream: memory is O(read size + largest row)."""
    def __init__(self, rows, batch_id, file_id):
        self.rows = iter(rows)
        self.batch_id = batch_id
        self.file_id = file_id
        self.pending = ''
        self.count = 0

    def read(self, size=65536):
        if size < 0:
            raise ValueError('COPY reader requires bounded read size')
        parts = []
        remaining = size
        while remaining:
            if not self.pending:
                row = next(self.rows, None)
                if row is None:
                    break
                self.pending = '\t'.join(_copy_text(value) for value in (
                    self.batch_id, self.file_id, row.source_row_number, row.source_key,
                    json.dumps(row.raw_payload, ensure_ascii=False),
                    json.dumps(row.validation_errors, ensure_ascii=False) if row.validation_errors else None,
                )) + '\n'
                self.count += 1
            piece, self.pending = self.pending[:remaining], self.pending[remaining:]
            parts.append(piece)
            remaining -= len(piece)
        return ''.join(parts)


class SpooledCSV:
    """Seekable single-PUT body plus a checksum of the exact BOM/CSV bytes."""
    def __init__(self, spool_limit):
        import hashlib
        import tempfile
        self.body = tempfile.SpooledTemporaryFile(max_size=spool_limit, mode='w+b')
        self.digest = hashlib.sha256()
        self.size = 0
        self.row_count = 0
        self._write(b'\xef\xbb\xbf')

    def _write(self, data):
        self.body.write(data)
        self.digest.update(data)
        self.size += len(data)

    def write(self, text):
        self._write(text.encode('utf-8'))
        return len(text)

    @property
    def sha256(self):
        return self.digest.hexdigest()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.body.close()


class EvidenceStore:
    """Disk-backed, reiterable evidence with exact global duplicate annotation.

    SQLite BINARY keys intentionally match Python's exact string Counter (not
    a case-folding database collation). Scratch data is not durable job state.
    """
    def __init__(self, row_type, required_key, has_key):
        import sqlite3
        import tempfile
        self.directory = tempfile.TemporaryDirectory(prefix='landing-evidence-')
        self.db = sqlite3.connect(self.directory.name + '/evidence.sqlite')
        self.db.execute('PRAGMA cache_size=-8192')
        self.db.execute('PRAGMA temp_store=FILE')
        self.db.execute('CREATE TABLE evidence (n INTEGER PRIMARY KEY, key TEXT, payload TEXT NOT NULL, notes TEXT)')
        self.row_type, self.required_key, self.has_key = row_type, required_key, has_key
        self.count = self.blank_count = self.duplicate_count = 0

    def append(self, row):
        # Row notes (e.g. derived_key:phone) travel with the row into the quality ledger.
        self.db.execute('INSERT INTO evidence VALUES (?,?,?,?)',
                        (row.source_row_number, row.source_key, json.dumps(row.raw_payload, ensure_ascii=False),
                         json.dumps(row.validation_errors, ensure_ascii=False) if row.validation_errors else None))
        self.count += 1
        if self.has_key and row.source_key is None:
            self.blank_count += 1

    def finish(self):
        self.db.execute('CREATE INDEX evidence_key ON evidence(key)')
        self.db.execute('CREATE TABLE duplicates (key TEXT PRIMARY KEY, n INTEGER NOT NULL)')
        self.db.execute('INSERT INTO duplicates SELECT key,count(*) FROM evidence WHERE key IS NOT NULL GROUP BY key HAVING count(*)>1')
        self.duplicate_count = self.db.execute('SELECT coalesce(sum(n),0) FROM duplicates').fetchone()[0]
        self.db.commit()

    def __len__(self):
        return self.count

    def __iter__(self):
        for number, key, payload, notes, duplicate in self.db.execute(
            'SELECT e.n,e.key,e.payload,e.notes,d.n FROM evidence e LEFT JOIN duplicates d ON d.key=e.key ORDER BY e.n'
        ):
            errors = json.loads(notes) if notes else []
            if not self.has_key:
                errors.append('missing_required_column:' + self.required_key)
            elif key is None:
                errors.append('missing_or_blank_key:' + self.required_key)
            elif duplicate:
                errors.append('duplicate_key:' + self.required_key)
            yield self.row_type(number, key, json.loads(payload), errors or None)

    def close(self):
        self.db.close()
        self.directory.cleanup()


