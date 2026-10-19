"""Pinned connector Parquet evidence validator; no DB or AWS writes.

Keeps strings for PostgreSQL lower/date/timestamptz casts in a future recovery
adapter. Never route through canonical CSV (raw_payload differs).
"""
import hashlib
import json
import re
import pyarrow as pa
import pyarrow.parquet as pq

PARSER_VERSION='connector-parquet-reconstruction-v1'
PARQUET_COLUMNS=('source_row_number','source_customer_id','external_id','first_name','middle_name','last_name','full_name','nickname','gender','date_of_birth','birthday','age','email','phone','line_user_id','facebook_id','google_id','apple_id','member_code','customer_type','status','language','address_line_1','address_line_2','district','province','postal_code','country','source','source_system','external_ids','attributes','raw_payload','source_created_at','source_updated_at')
PARQUET_SCHEMA=pa.schema([pa.field(n,pa.int64() if n in ('source_row_number','age') else pa.string()) for n in PARQUET_COLUMNS])

def _check(ok,code):
    if not ok:raise ValueError(code)

def _sha(body,expected,code):
    _check(isinstance(expected,str) and re.fullmatch('[0-9a-f]{64}',expected) and hashlib.sha256(body).hexdigest()==expected,code)

def parse_manifest(body,checksum,expected_selected,fetch_part):
    _sha(body,checksum,'MANIFEST_CHECKSUM')
    try:m=json.loads(body)
    except Exception:raise ValueError('MANIFEST_JSON') from None
    _check(isinstance(m,dict) and m.get('manifest_version')==1 and m.get('format')=='parquet','MANIFEST_FORMAT')
    parts=m.get('parts');_check(isinstance(parts,list) and parts,'MANIFEST_PARTS')
    selected={};numbers=set();part_ids=set();locations=set();rows=0;byte_count=0;proof=[]
    for part in parts:
        _check(isinstance(part,dict),'PART_METADATA')
        number=part.get('part_number');_check(type(number) is int and number>0 and number not in part_ids,'PART_NUMBER');part_ids.add(number)
        location=(part.get('bucket'),part.get('key'));_check(all(isinstance(x,str) and x for x in location) and location not in locations,'PART_LOCATION');locations.add(location)
        data=fetch_part(part);_sha(data,part.get('checksum_sha256'),'PART_CHECKSUM');_check(len(data)==part.get('byte_count'),'PART_BYTES')
        try:parquet=pq.ParquetFile(pa.BufferReader(data))
        except Exception:raise ValueError('PART_PARSE') from None
        _check(parquet.schema_arrow.equals(PARQUET_SCHEMA,check_metadata=False),'PART_SCHEMA');_check(parquet.metadata.num_rows==part.get('row_count'),'PART_ROWS')
        rows+=parquet.metadata.num_rows;byte_count+=len(data)
        for batch in parquet.iter_batches():
            for row in batch.to_pylist():
                num=row['source_row_number'];key=row['source_customer_id']
                _check(type(num) is int and num>0 and num not in numbers,'SOURCE_ROW_NUMBER');numbers.add(num)
                _check(isinstance(key,str) and key!='','SOURCE_KEY')
                for name in ('external_ids','attributes','raw_payload'):
                    try:row[name]=json.loads(row[name])
                    except Exception:raise ValueError('JSON_FIELD') from None
                    _check(isinstance(row[name],dict),'JSON_OBJECT')
                if key not in selected or selected[key]['source_row_number']<num:selected[key]=row
        proof.append({k:part[k] for k in ('part_number','bucket','key','checksum_sha256','row_count','byte_count')})
    _check(rows==m.get('records_published'),'MANIFEST_ROWS');_check(byte_count==m.get('bytes_written'),'MANIFEST_BYTES');_check(len(selected)==expected_selected,'SELECTED_ROWS')
    return list(selected.values()),{'manifest_sha256':checksum,'parts':proof,'selected_rows':len(selected),'published_rows':rows,'parser_version':PARSER_VERSION}
