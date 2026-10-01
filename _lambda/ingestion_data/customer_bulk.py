"""Bounded-memory typed COPY customer writer; requires migration 022.

Accepts existing prepared customer dictionaries or a one-pass iterable. Never
commits: all pages, lineage and completion must share the caller's transaction.
The iterable must contain the canonical selected (unique) source keys. Duplicate
keys fail closed, including across pages. Log timings are uncommitted work only.
"""
import json
import logging
from time import perf_counter

LOGGER = logging.getLogger(__name__)
CUSTOMER_FIELDS = (
    'source_customer_id', 'external_id', 'first_name', 'middle_name', 'last_name',
    'full_name', 'nickname', 'gender', 'date_of_birth', 'birthday', 'age', 'email',
    'phone', 'line_user_id', 'facebook_id', 'google_id', 'apple_id', 'member_code',
    'customer_type', 'status', 'language', 'address_line_1', 'address_line_2',
    'district', 'province', 'postal_code', 'country', 'source', 'source_system',
    'external_ids', 'attributes', 'raw_payload', 'source_created_at', 'source_updated_at',
)
JSON_FIELDS = frozenset(('external_ids', 'attributes', 'raw_payload'))


def _copy_text(value):
    if value is None:
        return r'\N'
    return str(value).replace('\\', '\\\\').replace('\t', r'\t').replace('\n', r'\n').replace('\r', r'\r')


class _CustomerCopyStream:
    """COPY read(size) adapter; materializes at most one encoded input record."""
    def __init__(self, customers, batch_id, source_system_id):
        self.rows = iter(customers)
        self.prefix = (batch_id, source_system_id)
        self.pending = ''
        self.count = 0

    def read(self, size=65536):
        if size < 0:
            raise ValueError('COPY must request bounded reads')
        chunks = []
        remaining = size
        while remaining:
            if not self.pending:
                try:
                    customer = next(self.rows)
                except StopIteration:
                    break
                values = list(self.prefix)
                for field in CUSTOMER_FIELDS:
                    value = customer.get(field)
                    if field in JSON_FIELDS:
                        # Match psycopg2.Json(None): JSON null, not SQL NULL.
                        value = json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
                    values.append(value)
                self.pending = '\t'.join(_copy_text(v) for v in values) + '\n'
                self.count += 1
            chunks.append(self.pending[:remaining])
            remaining -= len(chunks[-1])
            self.pending = self.pending[len(chunks[-1]):]
        return ''.join(chunks)


def upsert_customers(cursor, customers, batch_id, source_system_id, *, forward=False):
    """COPY selected customers and merge history/current/membership atomically.

    Returns selected row count (callers formerly ignoring None remain compatible).
    The source advisory lock is acquired by SQL merge and held until COMMIT.
    No transaction control, history bypass, or per-row SQL is used here.
    """
    start = perf_counter()
    cursor.execute('CREATE TEMP TABLE IF NOT EXISTS customer_bulk_stage '
                   '(LIKE raw.customer_current INCLUDING DEFAULTS INCLUDING CONSTRAINTS) ON COMMIT DROP')
    cursor.execute('TRUNCATE pg_temp.customer_bulk_stage')
    cursor.execute('CREATE UNIQUE INDEX IF NOT EXISTS customer_bulk_stage_source_key '
                   'ON pg_temp.customer_bulk_stage(source_customer_id)')
    stream = _CustomerCopyStream(customers, batch_id, source_system_id)
    columns = ','.join(('batch_id', 'source_system_id') + CUSTOMER_FIELDS)
    copied = perf_counter()
    cursor.copy_expert(f'COPY pg_temp.customer_bulk_stage({columns}) FROM STDIN', stream, size=65536)
    merged = perf_counter()
    merge = ('raw.apply_customer_forward_bulk' if forward else 'raw.apply_customer_bulk')
    cursor.execute(f'SELECT {merge}(%s::uuid,%s::bigint)', (batch_id, source_system_id))
    selected = int(cursor.fetchone()[0])
    if selected != stream.count:
        raise RuntimeError(f'Customer bulk selected {selected} of {stream.count} rows')
    finished = perf_counter()
    LOGGER.info(json.dumps(dict(event='customer_bulk_uncommitted', records=selected,
                                prepare_seconds=copied-start, copy_seconds=merged-copied,
                                merge_seconds=finished-merged, total_seconds=finished-start), sort_keys=True))
    return selected
