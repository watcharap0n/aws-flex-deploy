"""Pinned Parquet evidence parsing must verify all parts and last-row selection."""
import hashlib,json,sys
from pathlib import Path
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT/'scripts'))

def test_parquet_parser_verifies_bytes_and_selects_last_source_row():
    import importlib.util
    assert importlib.util.find_spec('history_parquet_parser_v1'), 'Pinned Parquet recovery parser absent'
    from history_parquet_parser_v1 import parse_manifest,PARQUET_SCHEMA
    rows=[]
    for num,email in [(1,'OLD@EXAMPLE.COM'),(2,'NEW@EXAMPLE.COM')]:
        r={f.name:None for f in PARQUET_SCHEMA};r.update(source_row_number=num,source_customer_id='same',email=email,external_ids='{}',attributes='{}',raw_payload='{"version":%d}'%num);rows.append(r)
    sink=pa.BufferOutputStream();pq.write_table(pa.Table.from_pylist(rows,schema=PARQUET_SCHEMA),sink);body=sink.getvalue().to_pybytes();digest=hashlib.sha256(body).hexdigest()
    manifest=json.dumps({'manifest_version':1,'format':'parquet','records_published':2,'bytes_written':len(body),'parts':[{'part_number':1,'bucket':'b','key':'part','checksum_sha256':digest,'row_count':2,'byte_count':len(body)}]}).encode()
    parsed,proof=parse_manifest(manifest,hashlib.sha256(manifest).hexdigest(),1,lambda part:body)
    assert len(parsed)==1 and parsed[0]['email']=='NEW@EXAMPLE.COM' and parsed[0]['source_row_number']==2
    assert parsed[0]['raw_payload']=={'version':2} and proof['selected_rows']==1
    with pytest.raises(ValueError,match='PART_CHECKSUM'):
        parse_manifest(manifest,hashlib.sha256(manifest).hexdigest(),1,lambda part:body+b'x')
