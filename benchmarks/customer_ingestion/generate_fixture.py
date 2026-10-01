"""Generate explicitly synthetic, wide customer CSV without row materialization.

Generate in an isolated benchmark workspace, never an ordinary ingestion prefix.
A/B fixtures use the same source keys but changed email/name/attributes.
"""
from __future__ import annotations

import argparse
import csv
from datetime import date, timedelta
import hashlib
import json
from pathlib import Path

VERSION = 'synthetic-wide-customer-v1'
FIELDS: tuple[str, ...] = (
    'source_customer_id', 'external_id', 'first_name', 'middle_name', 'last_name',
    'full_name', 'nickname', 'email', 'phone', 'gender', 'date_of_birth', 'birthday',
    'age', 'line_user_id', 'facebook_id', 'google_id', 'apple_id', 'member_code',
    'customer_type', 'status', 'language', 'address_line_1', 'address_line_2',
    'district', 'province', 'postal_code', 'country', 'source', 'source_system',
    'external_ids', 'attributes', 'source_created_at', 'source_updated_at',
)


def fixture_row(index: int, revision: str, padding: int) -> dict[str, str]:
    key = f'BENCH-{index:010d}'
    birthday = (date(1960, 1, 1) + timedelta(days=index % 18250)).isoformat()
    first = f'ข้อมูลทดสอบ{revision}{index}'
    last = f'สังเคราะห์{index % 997}'
    compact = lambda value: json.dumps(value, ensure_ascii=False, separators=(',', ':'))
    # Vary padding to avoid an unrealistic identical highly-compressible blob.
    seed = hashlib.sha256(f'{VERSION}:{index}:{revision}'.encode()).hexdigest()
    filler = (seed * ((padding + len(seed) - 1) // len(seed)))[:padding]
    return {
        'source_customer_id': key, 'external_id': f'ext-{index:010d}',
        'first_name': first, 'middle_name': '', 'last_name': last,
        'full_name': f'{first} {last}', 'nickname': f'ทดสอบ{index % 300}',
        'email': f'synthetic.{index}.{revision.lower()}@example.com',
        'phone': f'08{index % 100000000:08d}', 'gender': ('male', 'female')[index % 2],
        'date_of_birth': birthday, 'birthday': birthday, 'age': '',
        'line_user_id': f'line-{index:010d}', 'facebook_id': '', 'google_id': '',
        'apple_id': '', 'member_code': f'member-{index:010d}', 'customer_type': 'member',
        'status': 'SUSPENDED' if index % 17 == 0 else 'ACTIVE', 'language': 'th',
        'address_line_1': f'{index % 10000} ถนนข้อมูลสังเคราะห์ แขวงทดสอบ',
        'address_line_2': '', 'district': 'เขตทดสอบ', 'province': 'กรุงเทพมหานคร',
        'postal_code': '10110', 'country': 'TH', 'source': 'synthetic-benchmark',
        'source_system': 'synthetic-benchmark',
        'external_ids': compact({'external_id': f'ext-{index:010d}', 'member_code': f'member-{index:010d}'}),
        'attributes': compact({'synthetic': True, 'fixture': VERSION, 'revision': revision,
                               'index': index, 'tier': ('silver', 'gold', 'vip')[index % 3],
                               'preferences': {'language': 'th', 'channels': ['email', 'line']},
                               'padding': filler}),
        'source_created_at': '2026-01-01T00:00:00Z',
        'source_updated_at': '2026-01-02T00:00:00Z' if revision == 'A' else '2026-01-03T00:00:00Z',
    }


def generate(path: Path, *, rows: int, start: int = 0, revision: str = 'A', padding: int = 256) -> dict:
    if rows <= 0 or start < 0 or padding < 0 or revision not in {'A', 'B'}:
        raise ValueError('Positive rows, nonnegative offset/padding and A/B revision required')
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('x', encoding='utf-8-sig', newline='') as output:
        writer = csv.DictWriter(output, fieldnames=FIELDS, lineterminator='\n')
        writer.writeheader()
        for index in range(start, start + rows):
            writer.writerow(fixture_row(index, revision, padding))
    digest = hashlib.sha256()
    size = 0
    with path.open('rb') as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return {
        'synthetic': True, 'generator_version': VERSION,
        'generator_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'path': str(path.resolve()), 'rows': rows, 'unique_source_keys': rows,
        'key_start': start, 'key_end_exclusive': start + rows, 'revision': revision,
        'padding': padding, 'bytes': size, 'sha256': digest.hexdigest(),
        'bytes_per_record_including_header': size / rows,
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('path', type=Path)
    parser.add_argument('--rows', type=int, required=True)
    parser.add_argument('--start', type=int, default=0)
    parser.add_argument('--revision', choices=['A', 'B'], default='A')
    parser.add_argument('--padding', type=int, default=256)
    args = parser.parse_args()
    result = generate(args.path, rows=args.rows, start=args.start,
                      revision=args.revision, padding=args.padding)
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
