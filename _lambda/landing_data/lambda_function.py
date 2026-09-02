"""S3 landing Lambda: single-source customer evidence and CSV publication.

Input contract:
    data_sources/<source_code>/<batch_ref>/customers/<file>.(xlsx|csv|json)

Output contract (created after the one customer file is valid):
    raw/<source_code>/<landing_batch_id>/customer.csv

This Lambda owns only control.source_landing_*, raw.customer_landing_source,
and the output object. The downstream CSV Lambda owns control.source_system,
control.ingestion_batch, and raw.customer.

Python runtime: 3.10
Layer/package dependencies: pandas, openpyxl, psycopg2-binary
AWS Lambda provides boto3 and botocore.

Required environment variables:
    DB_SECRET_ID

Optional environment variables:
    INPUT_PREFIX          default: data_sources/
    OUTPUT_ROOT_PREFIX    default: raw/
    OUTPUT_BUCKET         default: event/source bucket
    UPSERT_PAGE_SIZE      default: 1000
"""

from __future__ import annotations

import base64
import csv
import hashlib
import io
import json
import logging
import math
import numbers
import os
import re
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import unquote_plus

import boto3
import pandas as pd
import psycopg2
from botocore.exceptions import ClientError
from psycopg2.extras import Json, execute_values

LOGGER = logging.getLogger()
LOGGER.setLevel(logging.INFO)

DEFAULT_INPUT_PREFIX = "data_sources/"
DEFAULT_OUTPUT_ROOT_PREFIX = "raw/"
SUPPORTED_FILE_FORMATS = {"xlsx", "csv", "json"}
SOURCE_CODE_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
BATCH_REF_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")

SOURCE_CONFIG = {
    "customers": {
        "required_key": "source_customer_id",
        "table": "raw.customer_landing_source",
        "key_column": "source_customer_id",
    },
}

# Canonical names are preferred. The aliases make the cut-over tolerant of the
# existing CRM profile export and the previous canonical CSV without bringing
# back the old two-file join.
COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "source_customer_id": ("source_customer_id", "customer_id", "StoreCode"),
    "external_id": ("external_id",),
    "first_name": ("first_name", "FirstName"),
    "middle_name": ("middle_name", "MiddleName"),
    "last_name": ("last_name", "LastName"),
    "full_name": ("full_name",),
    "nickname": ("nickname",),
    "email": ("email", "Email"),
    "phone": ("phone", "phone_number", "ContactNumber"),
    "gender": ("gender", "Gender"),
    "date_of_birth": ("date_of_birth", "dob"),
    "birthday": ("birthday",),
    "age": ("age",),
    "line_user_id": ("line_user_id", "LineUuid"),
    "facebook_id": ("facebook_id",),
    "google_id": ("google_id",),
    "apple_id": ("apple_id",),
    "member_code": ("member_code",),
    "customer_type": ("customer_type",),
    "status": ("status",),
    "language": ("language",),
    "address_line_1": ("address_line_1", "address", "StoreAddress"),
    "address_line_2": ("address_line_2",),
    "district": ("district", "city", "StoreDistrict"),
    "province": ("province", "state", "StoreProvince"),
    "postal_code": ("postal_code", "StorePostCode"),
    "country": ("country",),
    "source": ("source",),
    "source_system": ("source_system",),
    "external_ids": ("external_ids",),
    "attributes": ("attributes",),
    "source_created_at": ("source_created_at", "signup_date", "RegisterDate"),
    "source_updated_at": ("source_updated_at",),
}

# Read identifiers as text wherever these columns exist so phone, customer,
# device, and source identifiers never pass through a floating-point value.
IDENTIFIER_COLUMNS = {
    "CustCd",
    "StoreCode",
    "_id",
    "BzbsUserId",
    "LineUuid",
    "ContactNumber",
    "DcCode",
    "SalesmanCode",
    "SupervisorCd",
    "IDCard",
    "IDPostCode",
    "StorePostCode",
}

SUCCESS_FILE_STATUSES = {"INGESTED", "INGESTED_WITH_WARNINGS"}
TERMINAL_FILE_STATUSES = SUCCESS_FILE_STATUSES | {"INVALID"}

CSV_COLUMNS = [
    "source_customer_id",
    "external_id",
    "first_name",
    "middle_name",
    "last_name",
    "full_name",
    "nickname",
    "email",
    "phone",
    "gender",
    "date_of_birth",
    "birthday",
    "age",
    "line_user_id",
    "facebook_id",
    "google_id",
    "apple_id",
    "member_code",
    "customer_type",
    "status",
    "language",
    "address_line_1",
    "address_line_2",
    "district",
    "province",
    "postal_code",
    "country",
    "source",
    "source_system",
    "external_ids",
    "attributes",
    "source_created_at",
    "source_updated_at",
]


@dataclass(frozen=True)
class Config:
    db_secret_id: str
    input_prefix: str
    output_root_prefix: str
    output_bucket: str | None
    upsert_page_size: int


@dataclass(frozen=True)
class SourceObject:
    bucket: str
    key: str
    source_code: str
    batch_ref: str
    source_type: str
    file_format: str
    event_version_id: str | None


@dataclass(frozen=True)
class DownloadedObject:
    content: bytes
    version_id: str | None
    etag: str | None
    content_length: int
    content_sha256: str


@dataclass(frozen=True)
class EvidenceRow:
    source_row_number: int
    source_key: str | None
    raw_payload: dict[str, Any]
    validation_errors: list[str] | None


@dataclass(frozen=True)
class ParsedSourceFile:
    sheet_name: str | None
    column_names: list[str]
    rows: list[EvidenceRow]
    file_status: str
    file_error: str | None
    blank_key_rows: int
    duplicate_key_rows: int


class SourceConflictError(ValueError):
    """The immutable batch already has another file for this source type."""


class OutputConflictError(RuntimeError):
    """The deterministic output key exists with different content/metadata."""


def _normalise_name(value: Any) -> str:
    return str(value).strip().casefold()


def _normalise_prefix(value: str) -> str:
    prefix = value.strip().lstrip("/")
    if not prefix:
        raise ValueError("S3 prefix cannot be empty")
    return prefix if prefix.endswith("/") else f"{prefix}/"


def _required_environment(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"Missing required environment variable: {name}")
    return value


def _load_config() -> Config:
    page_size_raw = (os.getenv("UPSERT_PAGE_SIZE") or "1000").strip()
    try:
        page_size = int(page_size_raw)
    except ValueError as exc:
        raise ValueError("UPSERT_PAGE_SIZE must be an integer") from exc
    if page_size <= 0:
        raise ValueError("UPSERT_PAGE_SIZE must be greater than zero")

    output_bucket = (os.getenv("OUTPUT_BUCKET") or "").strip() or None
    return Config(
        db_secret_id=_required_environment("DB_SECRET_ID"),
        input_prefix=_normalise_prefix(
            os.getenv("INPUT_PREFIX", DEFAULT_INPUT_PREFIX)
        ),
        output_root_prefix=_normalise_prefix(
            os.getenv("OUTPUT_ROOT_PREFIX", DEFAULT_OUTPUT_ROOT_PREFIX)
        ),
        output_bucket=output_bucket,
        upsert_page_size=page_size,
    )


def _source_from_record(
        record: Mapping[str, Any], config: Config
) -> SourceObject | None:
    s3_data = record.get("s3") or {}
    bucket = str(((s3_data.get("bucket") or {}).get("name") or "")).strip()
    object_data = s3_data.get("object") or {}
    encoded_key = str(object_data.get("key") or "").strip()
    if not bucket or not encoded_key:
        return None

    key = unquote_plus(encoded_key).lstrip("/")
    if not key.startswith(config.input_prefix):
        return None
    filename = key.rsplit("/", 1)[-1]
    if "." not in filename:
        return None
    file_format = filename.rsplit(".", 1)[-1].casefold()
    if file_format not in SUPPORTED_FILE_FORMATS:
        return None

    relative_key = key[len(config.input_prefix):]
    parts = relative_key.split("/")
    if len(parts) != 4 or any(not part for part in parts):
        raise ValueError(
            "S3 key must be "
            f"{config.input_prefix}<source_code>/<batch_ref>/"
            "customers/<file>.(xlsx|csv|json)"
        )

    source_code, batch_ref, source_type, filename = parts
    if not SOURCE_CODE_PATTERN.fullmatch(source_code):
        raise ValueError(
            "source_code must be lowercase and use only a-z, 0-9, "
            "underscore, or hyphen (maximum 64 characters)"
        )
    if not BATCH_REF_PATTERN.fullmatch(batch_ref):
        raise ValueError(
            "batch_ref must be lowercase and use only a-z, 0-9, dot, "
            "underscore, or hyphen (maximum 128 characters)"
        )
    if source_type not in SOURCE_CONFIG:
        raise ValueError("source_type must be customers")
    if filename.rsplit(".", 1)[-1].casefold() not in SUPPORTED_FILE_FORMATS:
        return None

    return SourceObject(
        bucket=bucket,
        key=key,
        source_code=source_code,
        batch_ref=batch_ref,
        source_type=source_type,
        file_format=file_format,
        event_version_id=object_data.get("versionId"),
    )


def _download_object(s3_client: Any, source: SourceObject) -> DownloadedObject:
    request: dict[str, Any] = {"Bucket": source.bucket, "Key": source.key}
    if source.event_version_id:
        request["VersionId"] = source.event_version_id

    response = s3_client.get_object(**request)
    content = response["Body"].read()
    etag = response.get("ETag")
    if isinstance(etag, str):
        etag = etag.strip('"')

    return DownloadedObject(
        content=content,
        version_id=response.get("VersionId") or source.event_version_id,
        etag=etag,
        content_length=int(response.get("ContentLength", len(content))),
        content_sha256=hashlib.sha256(content).hexdigest(),
    )


def _is_missing(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (dict, list, tuple, set)):
        return False
    try:
        result = pd.isna(value)
        return bool(result) if isinstance(result, (bool, numbers.Integral)) else False
    except (TypeError, ValueError):
        return False


def _identifier_text(value: Any) -> str | None:
    if _is_missing(value):
        return None
    if isinstance(value, numbers.Integral):
        return str(int(value))
    if isinstance(value, numbers.Real):
        number = float(value)
        if math.isfinite(number) and number.is_integer():
            return str(int(number))

    text = str(value).strip()
    if not text:
        return None
    if re.fullmatch(r"[+-]?\d+\.0+", text):
        return text.split(".", 1)[0]
    return text


def _json_safe(value: Any) -> Any:
    if _is_missing(value):
        return None
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (pd.Timestamp, datetime, date)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, bytes):
        return base64.b64encode(value).decode("ascii")
    if hasattr(value, "item"):
        try:
            return _json_safe(value.item())
        except (TypeError, ValueError):
            pass
    return value


def _unique_headers(columns: Iterable[Any]) -> list[str]:
    counts: dict[str, int] = {}
    result: list[str] = []
    for index, column in enumerate(columns, start=1):
        base = str(column).strip() or f"column_{index}"
        normalised = _normalise_name(base)
        counts[normalised] = counts.get(normalised, 0) + 1
        occurrence = counts[normalised]
        result.append(base if occurrence == 1 else f"{base}__{occurrence}")
    return result


def _matching_columns(columns: Iterable[Any], expected: str) -> list[str]:
    wanted = {
        _normalise_name(alias)
        for alias in COLUMN_ALIASES.get(expected, (expected,))
    }
    return [
        str(column)
        for column in columns
        if _normalise_name(column) in wanted
    ]


def _is_identifier_header(value: Any) -> bool:
    normalised = _normalise_name(value)
    collapsed = re.sub(r"[^a-z0-9]", "", normalised)
    explicit = {_normalise_name(name) for name in IDENTIFIER_COLUMNS}
    return (
            normalised in explicit
            or "code" in collapsed
            or collapsed.endswith(("id", "uuid", "phone", "postcode"))
            or collapsed == "contactnumber"
    )


def _frame_to_evidence(
        frame: pd.DataFrame,
        required_key: str,
        *,
        sheet_name: str | None,
        row_number_start: int,
) -> ParsedSourceFile:
    """Convert a tabular source into the common evidence representation."""
    required_matches = _matching_columns(frame.columns, required_key)
    frame = frame.copy()
    frame.columns = _unique_headers(frame.columns)
    clean_key_matches = _matching_columns(frame.columns, required_key)
    key_column = clean_key_matches[0] if clean_key_matches else None

    file_errors: list[str] = []
    if not required_matches:
        file_errors.append(f"missing_required_column:{required_key}")
    elif len(required_matches) > 1:
        file_errors.append(f"ambiguous_required_column:{required_key}")
    if frame.empty:
        file_errors.append("no_data_rows")

    payloads: list[dict[str, Any]] = []
    source_keys: list[str | None] = []
    columns = list(frame.columns)
    for values in frame.itertuples(index=False, name=None):
        payload = {
            column: _json_safe(value)
            for column, value in zip(columns, values)
        }
        payloads.append(payload)
        source_keys.append(
            _identifier_text(payload.get(key_column)) if key_column else None
        )

    key_counts = Counter(key for key in source_keys if key is not None)
    rows: list[EvidenceRow] = []
    blank_key_rows = 0
    duplicate_key_rows = 0
    for row_number, (payload, source_key) in enumerate(
            zip(payloads, source_keys), start=row_number_start
    ):
        row_errors: list[str] = []
        if key_column is None:
            row_errors.append(f"missing_required_column:{required_key}")
        elif source_key is None:
            blank_key_rows += 1
            row_errors.append(f"missing_or_blank_key:{required_key}")
        elif key_counts[source_key] > 1:
            duplicate_key_rows += 1
            row_errors.append(f"duplicate_key:{required_key}")

        rows.append(
            EvidenceRow(
                source_row_number=row_number,
                source_key=source_key,
                raw_payload=payload,
                validation_errors=row_errors or None,
            )
        )

    if file_errors:
        file_status = "INVALID"
        file_error = ";".join(file_errors)
    elif blank_key_rows or duplicate_key_rows:
        file_status = "INGESTED_WITH_WARNINGS"
        file_error = None
    else:
        file_status = "INGESTED"
        file_error = None

    return ParsedSourceFile(
        sheet_name=sheet_name,
        column_names=columns,
        rows=rows,
        file_status=file_status,
        file_error=file_error,
        blank_key_rows=blank_key_rows,
        duplicate_key_rows=duplicate_key_rows,
    )


def parse_excel_evidence(content: bytes, required_key: str) -> ParsedSourceFile:
    """Auto-detect the worksheet and preserve identifiers as text."""
    excel = pd.ExcelFile(io.BytesIO(content), engine="openpyxl")
    if not excel.sheet_names:
        raise ValueError("Excel workbook contains no worksheets")

    selected_sheet = excel.sheet_names[0]
    selected_header: list[Any] = []

    for sheet_name in excel.sheet_names:
        header_frame = pd.read_excel(excel, sheet_name=sheet_name, nrows=0)
        header = list(header_frame.columns)
        matches = _matching_columns(header, required_key)
        if matches:
            selected_sheet = sheet_name
            selected_header = header
            break

    if not selected_header:
        header_frame = pd.read_excel(excel, sheet_name=selected_sheet, nrows=0)
        selected_header = list(header_frame.columns)

    text_dtypes = {
        str(column): "string"
        for column in selected_header
        if _is_identifier_header(column)
    }
    frame = pd.read_excel(
        excel,
        sheet_name=selected_sheet,
        dtype=text_dtypes or None,
    )
    return _frame_to_evidence(
        frame,
        required_key,
        sheet_name=selected_sheet,
        row_number_start=2,
    )


def parse_csv_evidence(content: bytes, required_key: str) -> ParsedSourceFile:
    """Read a UTF-8 CSV while preserving identifier columns as text."""
    buffer = io.BytesIO(content)
    try:
        header_frame = pd.read_csv(
            buffer,
            nrows=0,
            encoding="utf-8-sig",
            keep_default_na=False,
        )
    except pd.errors.EmptyDataError as exc:
        raise ValueError("CSV file is empty or contains no header") from exc

    headers = list(header_frame.columns)
    text_dtypes = {
        str(column): "string"
        for column in headers
        if _is_identifier_header(column)
    }
    buffer.seek(0)
    frame = pd.read_csv(
        buffer,
        dtype=text_dtypes or None,
        encoding="utf-8-sig",
        keep_default_na=False,
    )
    return _frame_to_evidence(
        frame,
        required_key,
        sheet_name=None,
        row_number_start=2,
    )


def parse_json_evidence(content: bytes, required_key: str) -> ParsedSourceFile:
    """Read a JSON array, one object, or an object containing records/data."""
    try:
        decoded = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("JSON file must use UTF-8 encoding") from exc
    try:
        payload = json.loads(decoded)
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Invalid JSON at line {exc.lineno}, column {exc.colno}"
        ) from exc

    if isinstance(payload, list):
        records = payload
    elif isinstance(payload, dict):
        container_key = next(
            (name for name in ("records", "data") if name in payload),
            None,
        )
        if container_key and not isinstance(payload[container_key], list):
            raise ValueError(f"JSON {container_key!r} must be an array of objects")
        records = payload[container_key] if container_key else [payload]
    else:
        raise ValueError(
            "JSON top level must be an object, an array of objects, "
            "or an object containing a records/data array"
        )

    if any(not isinstance(record, Mapping) for record in records):
        raise ValueError("Every JSON record must be an object")

    frame = pd.DataFrame([dict(record) for record in records])
    return _frame_to_evidence(
        frame,
        required_key,
        sheet_name=None,
        row_number_start=1,
    )


def parse_source_evidence(
        content: bytes,
        file_format: str,
        required_key: str,
) -> ParsedSourceFile:
    parsers = {
        "xlsx": parse_excel_evidence,
        "csv": parse_csv_evidence,
        "json": parse_json_evidence,
    }
    try:
        parser = parsers[file_format]
    except KeyError as exc:
        raise ValueError(f"Unsupported source file format: {file_format}") from exc
    return parser(content, required_key)


def _get_secret(secret_id: str) -> dict[str, Any]:
    response = boto3.client("secretsmanager").get_secret_value(SecretId=secret_id)
    if "SecretString" in response:
        secret_text = response["SecretString"]
    else:
        binary = response.get("SecretBinary")
        if binary is None:
            raise ValueError("Secrets Manager response contains no secret value")
        if isinstance(binary, str):
            binary = base64.b64decode(binary)
        secret_text = bytes(binary).decode("utf-8")

    secret = json.loads(secret_text)
    if not isinstance(secret, dict):
        raise ValueError("Database secret must be a JSON object")
    return secret


def _secret_value(secret: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        value = secret.get(key)
        if value is not None and str(value).strip():
            return value
    return None


def _database_connection(secret: Mapping[str, Any]) -> Any:
    host = _secret_value(secret, "host")
    database = _secret_value(secret, "dbname", "database")
    username = _secret_value(secret, "username", "user")
    password = _secret_value(secret, "password")
    missing = [
        label
        for label, value in (
            ("host", host),
            ("dbname/database", database),
            ("username/user", username),
            ("password", password),
        )
        if value is None
    ]
    if missing:
        raise ValueError(
            "Database secret is missing required key(s): " + ", ".join(missing)
        )

    return psycopg2.connect(
        host=host,
        port=int(_secret_value(secret, "port") or 5432),
        dbname=database,
        user=username,
        password=password,
        sslmode=str(_secret_value(secret, "sslmode") or "require"),
        connect_timeout=10,
        application_name="customer-excel-landing-lambda",
    )


def _resolve_batch(cursor: Any, source_code: str, batch_ref: str) -> str:
    lock_key = f"landing:{source_code}:{batch_ref}"
    cursor.execute(
        "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
        (lock_key,),
    )
    cursor.execute(
        """
        INSERT INTO control.source_landing_batch (source_code,
                                                  batch_ref,
                                                  status,
                                                  output_status,
                                                  created_at,
                                                  updated_at)
        VALUES (%s, %s, 'RECEIVING', 'PENDING', now(), now())
        ON CONFLICT (source_code, batch_ref) DO UPDATE
            SET updated_at = now()
        RETURNING batch_id
        """,
        (source_code, batch_ref),
    )
    return str(cursor.fetchone()[0])


def _register_file(
        cursor: Any,
        batch_id: str,
        source: SourceObject,
        downloaded: DownloadedObject,
) -> tuple[str, str]:
    """Return (file_id, action), where action is process/processed/in_progress."""
    cursor.execute(
        """
        SELECT file_id,
               content_sha256,
               status,
               updated_at < now() - interval '15 minutes' AS processing_is_stale
        FROM control.source_landing_file
        WHERE batch_id = %s
          AND source_type = %s
            FOR UPDATE
        """,
        (batch_id, source.source_type),
    )
    existing = cursor.fetchone()
    if existing:
        (
            file_id,
            old_sha256,
            old_status,
            processing_is_stale,
        ) = existing
        same_content = old_sha256 == downloaded.content_sha256
        if not same_content:
            raise SourceConflictError(
                f"Batch {source.source_code}/{source.batch_ref} already has a "
                f"different {source.source_type} file; use a new batch_ref"
            )
        if old_status in TERMINAL_FILE_STATUSES:
            return str(file_id), "processed"
        if old_status == "PROCESSING" and not processing_is_stale:
            return str(file_id), "in_progress"

        cursor.execute(
            """
            UPDATE control.source_landing_file
            SET status        = 'PROCESSING',
                error_message = NULL,
                updated_at    = now(),
                completed_at  = NULL
            WHERE file_id = %s
            """,
            (file_id,),
        )
        return str(file_id), "process"

    cursor.execute(
        """
        INSERT INTO control.source_landing_file (batch_id,
                                                 source_type,
                                                 s3_bucket,
                                                 s3_object_key,
                                                 s3_version_id,
                                                 s3_etag,
                                                 content_sha256,
                                                 file_size_bytes,
                                                 status,
                                                 created_at,
                                                 updated_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s,
                'PROCESSING', now(), now())
        RETURNING file_id
        """,
        (
            batch_id,
            source.source_type,
            source.bucket,
            source.key,
            downloaded.version_id,
            downloaded.etag,
            downloaded.content_sha256,
            downloaded.content_length,
        ),
    )
    return str(cursor.fetchone()[0]), "process"


def _replace_evidence_rows(
        cursor: Any,
        source_type: str,
        batch_id: str,
        file_id: str,
        rows: Sequence[EvidenceRow],
        page_size: int,
) -> int:
    source_config = SOURCE_CONFIG[source_type]
    table = source_config["table"]
    key_column = source_config["key_column"]

    cursor.execute(f"DELETE FROM {table} WHERE file_id = %s", (file_id,))
    if not rows:
        return 0

    sql = f"""
        INSERT INTO {table} (
            batch_id,
            file_id,
            source_row_number,
            {key_column},
            raw_payload,
            validation_errors
        ) VALUES %s
        ON CONFLICT (file_id, source_row_number) DO UPDATE SET
            batch_id = EXCLUDED.batch_id,
            {key_column} = EXCLUDED.{key_column},
            raw_payload = EXCLUDED.raw_payload,
            validation_errors = EXCLUDED.validation_errors,
            ingested_at = now()
    """
    values = [
        (
            batch_id,
            file_id,
            row.source_row_number,
            row.source_key,
            Json(row.raw_payload),
            Json(row.validation_errors) if row.validation_errors else None,
        )
        for row in rows
    ]
    for start in range(0, len(values), page_size):
        execute_values(
            cursor,
            sql,
            values[start: start + page_size],
            template="(%s,%s,%s,%s,%s,%s)",
            page_size=page_size,
        )
    return len(values)


def _complete_file(cursor: Any, file_id: str, parsed: ParsedSourceFile) -> None:
    cursor.execute(
        """
        UPDATE control.source_landing_file
        SET sheet_name         = %s,
            column_names       = %s,
            row_count          = %s,
            blank_key_rows     = %s,
            duplicate_key_rows = %s,
            status             = %s,
            error_message      = %s,
            updated_at         = now(),
            completed_at       = now()
        WHERE file_id = %s
        """,
        (
            parsed.sheet_name,
            Json(parsed.column_names),
            len(parsed.rows),
            parsed.blank_key_rows,
            parsed.duplicate_key_rows,
            parsed.file_status,
            parsed.file_error,
            file_id,
        ),
    )


def _fail_file(cursor: Any, file_id: str, error: str) -> None:
    cursor.execute(
        """
        UPDATE control.source_landing_file
        SET status        = 'INVALID',
            error_message = %s,
            updated_at    = now(),
            completed_at  = now()
        WHERE file_id = %s
        """,
        (error, file_id),
    )


def _refresh_batch_status(cursor: Any, batch_id: str) -> str:
    cursor.execute(
        """
        SELECT source_type, status, error_message
        FROM control.source_landing_file
        WHERE batch_id = %s
        """,
        (batch_id,),
    )
    files = {row[0]: (row[1], row[2]) for row in cursor.fetchall()}

    invalid = [
        (source_type, values[1])
        for source_type, values in files.items()
        if values[0] == "INVALID"
    ]
    if invalid:
        batch_status = "INVALID"
        last_error = ";".join(
            f"{source_type}:{error or 'invalid'}"
            for source_type, error in invalid
        )[:2000]
    elif set(SOURCE_CONFIG).issubset(files) and all(
            files[source_type][0] in SUCCESS_FILE_STATUSES
            for source_type in SOURCE_CONFIG
    ):
        batch_status = "SOURCES_INGESTED"
        last_error = None
    else:
        batch_status = "RECEIVING"
        last_error = None

    cursor.execute(
        """
        UPDATE control.source_landing_batch
        SET status        = %s,
            last_error    = %s,
            output_status = CASE
                                WHEN %s = 'INVALID' AND output_status <> 'PUBLISHED'
                                    THEN 'FAILED'
                                ELSE output_status
                END,
            output_error  = CASE
                                WHEN %s = 'INVALID' AND output_status <> 'PUBLISHED'
                                    THEN %s
                                ELSE output_error
                END,
            updated_at    = now()
        WHERE batch_id = %s
        """,
        (
            batch_status,
            last_error,
            batch_status,
            batch_status,
            last_error,
            batch_id,
        ),
    )
    return batch_status


def _payload_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        parsed = json.loads(value)
        if isinstance(parsed, dict):
            return parsed
    raise ValueError("Evidence raw_payload is not a JSON object")


def _payload_value(payload: Mapping[str, Any], name: str) -> Any:
    wanted = {
        _normalise_name(alias)
        for alias in COLUMN_ALIASES.get(name, (name,))
    }
    for key, value in payload.items():
        if _normalise_name(key) in wanted:
            return value
    return None


def _text_value(value: Any) -> str:
    if _is_missing(value):
        return ""
    return str(value).strip()


def _normalise_date(value: Any) -> str:
    if _is_missing(value):
        return ""
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()

    text = str(value).strip()
    if not text:
        return ""
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:[T ].*)?", text):
        return text[:10]

    try:
        number = float(text)
    except ValueError:
        number = None
    if number is not None and 1 <= number <= 100000:
        parsed = pd.to_datetime(
            number,
            unit="D",
            origin="1899-12-30",
            errors="coerce",
        )
    else:
        parsed = pd.to_datetime(text, errors="coerce")
    if pd.isna(parsed):
        return ""
    return parsed.date().isoformat()


def _normalise_timestamp(value: Any) -> str:
    if _is_missing(value):
        return ""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    text = str(value).strip()
    if not text:
        return ""
    parsed = pd.to_datetime(text, errors="coerce", utc=True)
    if pd.isna(parsed):
        return ""
    return parsed.isoformat()


def _json_object(value: Any, field_name: str) -> dict[str, Any]:
    if _is_missing(value):
        return {}
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{field_name} must be a JSON object") from exc
        if isinstance(parsed, dict):
            return parsed
    raise ValueError(f"{field_name} must be a JSON object")


def _external_ids(payload: Mapping[str, Any]) -> dict[str, Any]:
    result = _json_object(_payload_value(payload, "external_ids"), "external_ids")

    legacy_devices = _payload_value(payload, "device_id(s)")
    if not _is_missing(legacy_devices):
        values = [
            item.strip()
            for item in re.split(r"[;,|]", str(legacy_devices))
            if item.strip()
        ]
        if values:
            result["device_ids"] = list(dict.fromkeys(values))

    typed_identifiers = {
        "crm_record_id": "_id",
        "bzbs_user_id": "BzbsUserId",
        "line_user_id": "line_user_id",
        "national_id": "IDCard",
        "external_id": "external_id",
        "facebook_id": "facebook_id",
        "google_id": "google_id",
        "apple_id": "apple_id",
        "member_code": "member_code",
    }
    for target, source in typed_identifiers.items():
        value = _identifier_text(_payload_value(payload, source))
        if value and target not in result:
            result[target] = value
    return result


def _canonical_customer_row(
        source_customer_id: Any,
        payload: Mapping[str, Any],
) -> dict[str, Any]:
    first_name = _text_value(_payload_value(payload, "first_name"))
    middle_name = _text_value(_payload_value(payload, "middle_name"))
    last_name = _text_value(_payload_value(payload, "last_name"))
    full_name = _text_value(_payload_value(payload, "full_name"))
    if not full_name:
        full_name = " ".join(
            part for part in (first_name, middle_name, last_name) if part
        )

    return {
        "source_customer_id": _identifier_text(source_customer_id) or "",
        "external_id": _identifier_text(
            _payload_value(payload, "external_id")
        ) or "",
        "first_name": first_name,
        "middle_name": middle_name,
        "last_name": last_name,
        "full_name": full_name,
        "nickname": _text_value(_payload_value(payload, "nickname")),
        "email": _text_value(_payload_value(payload, "email")),
        "phone": _identifier_text(_payload_value(payload, "phone")) or "",
        "gender": _text_value(_payload_value(payload, "gender")),
        "date_of_birth": _normalise_date(
            _payload_value(payload, "date_of_birth")
            if not _is_missing(_payload_value(payload, "date_of_birth"))
            else _payload_value(payload, "birthday")
        ),
        "birthday": _normalise_date(_payload_value(payload, "birthday")),
        "age": _identifier_text(_payload_value(payload, "age")) or "",
        "line_user_id": _identifier_text(
            _payload_value(payload, "line_user_id")
        ) or "",
        "facebook_id": _identifier_text(
            _payload_value(payload, "facebook_id")
        ) or "",
        "google_id": _identifier_text(
            _payload_value(payload, "google_id")
        ) or "",
        "apple_id": _identifier_text(
            _payload_value(payload, "apple_id")
        ) or "",
        "member_code": _identifier_text(
            _payload_value(payload, "member_code")
        ) or "",
        "customer_type": _text_value(
            _payload_value(payload, "customer_type")
        ),
        "status": _text_value(_payload_value(payload, "status")),
        "language": _text_value(_payload_value(payload, "language")),
        "address_line_1": _text_value(
            _payload_value(payload, "address_line_1")
        ),
        "address_line_2": _text_value(
            _payload_value(payload, "address_line_2")
        ),
        "district": _text_value(_payload_value(payload, "district")),
        "province": _text_value(_payload_value(payload, "province")),
        "postal_code": _identifier_text(
            _payload_value(payload, "postal_code")
        ) or "",
        "country": _text_value(_payload_value(payload, "country")),
        "source": _text_value(_payload_value(payload, "source")),
        "source_system": _text_value(
            _payload_value(payload, "source_system")
        ),
        "external_ids": json.dumps(
            _external_ids(payload), ensure_ascii=False, sort_keys=True
        ),
        "attributes": json.dumps(
            _json_object(_payload_value(payload, "attributes"), "attributes"),
            ensure_ascii=False,
            sort_keys=True,
        ),
        "source_created_at": _normalise_timestamp(
            _payload_value(payload, "source_created_at")
        ),
        "source_updated_at": _normalise_timestamp(
            _payload_value(payload, "source_updated_at")
        ),
    }


def _build_customer_csv(
        connection: Any,
        batch_id: str,
        source_code: str,
) -> tuple[bytes, int]:
    with connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT DISTINCT ON (source_customer_id)
                       source_customer_id,
                       raw_payload
                FROM raw.customer_landing_source
                WHERE batch_id = %s
                  AND source_customer_id IS NOT NULL
                  AND btrim(source_customer_id) <> ''
                ORDER BY source_customer_id,
                         source_row_number DESC,
                         source_record_id DESC
                """,
                (batch_id,),
            )
            customers = cursor.fetchall()

    output = io.StringIO(newline="")
    writer = csv.DictWriter(
        output,
        fieldnames=CSV_COLUMNS,
        extrasaction="ignore",
        lineterminator="\n",
    )
    writer.writeheader()
    for source_customer_id, raw_payload in customers:
        payload = _payload_dict(raw_payload)
        writer.writerow(_canonical_customer_row(source_customer_id, payload))

    # utf-8-sig inserts a BOM, which the downstream Lambda explicitly accepts.
    return output.getvalue().encode("utf-8-sig"), len(customers)


def _mark_output_failed(connection: Any, batch_id: str, error: str) -> None:
    with connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE control.source_landing_batch
                SET output_status = 'FAILED',
                    output_error  = %s,
                    updated_at    = now()
                WHERE batch_id = %s
                  AND output_status <> 'PUBLISHED'
                """,
                (error[:2000], batch_id),
            )


def _head_matches_output(
        s3_client: Any,
        bucket: str,
        key: str,
        batch_id: str,
        source_code: str,
        sha256: str,
        row_count: int,
) -> tuple[bool, str | None]:
    response = s3_client.head_object(Bucket=bucket, Key=key)
    metadata = {
        str(name).casefold(): str(value)
        for name, value in (response.get("Metadata") or {}).items()
    }
    etag = response.get("ETag")
    if isinstance(etag, str):
        etag = etag.strip('"')
    matches = (
            metadata.get("landing-batch-id") == batch_id
            and metadata.get("source-code") == source_code
            and metadata.get("content-sha256") == sha256
            and metadata.get("row-count") == str(row_count)
    )
    return matches, etag


def _publish_conditionally(
        s3_client: Any,
        bucket: str,
        key: str,
        body: bytes,
        batch_id: str,
        source_code: str,
        sha256: str,
        row_count: int,
) -> tuple[str | None, bool]:
    metadata = {
        "landing-batch-id": batch_id,
        "source-code": source_code,
        "content-sha256": sha256,
        "row-count": str(row_count),
    }
    try:
        response = s3_client.put_object(
            Bucket=bucket,
            Key=key,
            Body=body,
            ContentType="text/csv; charset=utf-8",
            Metadata=metadata,
            IfNoneMatch="*",
        )
        etag = response.get("ETag")
        if isinstance(etag, str):
            etag = etag.strip('"')
        return etag, True
    except ClientError as exc:
        error = exc.response.get("Error") or {}
        code = str(error.get("Code") or "")
        status = (exc.response.get("ResponseMetadata") or {}).get(
            "HTTPStatusCode"
        )
        if code not in {"PreconditionFailed", "412"} and status != 412:
            raise

    matches, etag = _head_matches_output(
        s3_client,
        bucket,
        key,
        batch_id,
        source_code,
        sha256,
        row_count,
    )
    if not matches:
        raise OutputConflictError(
            f"Output s3://{bucket}/{key} already exists with different "
            "landing metadata or checksum; it was not overwritten"
        )
    return etag, False


def _acquire_output_lock(connection: Any, batch_id: str) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_advisory_lock(hashtextextended(%s, 0))",
            (f"landing-output:{batch_id}",),
        )
    connection.commit()


def _release_output_lock(connection: Any, batch_id: str) -> None:
    connection.rollback()
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_advisory_unlock(hashtextextended(%s, 0))",
            (f"landing-output:{batch_id}",),
        )
    connection.commit()


def _reconcile_output(
        connection: Any,
        s3_client: Any,
        config: Config,
        batch_id: str,
) -> dict[str, Any]:
    _acquire_output_lock(connection, batch_id)
    try:
        with connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT source_code,
                           batch_ref,
                           status,
                           output_status,
                           output_bucket,
                           output_key,
                           output_row_count
                    FROM control.source_landing_batch
                    WHERE batch_id = %s
                        FOR UPDATE
                    """,
                    (batch_id,),
                )
                batch = cursor.fetchone()
                if not batch:
                    raise RuntimeError(f"Landing batch not found: {batch_id}")

                (
                    source_code,
                    batch_ref,
                    batch_status,
                    output_status,
                    published_bucket,
                    published_key,
                    published_rows,
                ) = batch
                if output_status == "PUBLISHED":
                    return {
                        "status": "published",
                        "batch_id": batch_id,
                        "source_code": source_code,
                        "batch_ref": batch_ref,
                        "output_bucket": published_bucket,
                        "output_key": published_key,
                        "output_rows": published_rows,
                        "created": False,
                    }
                if batch_status == "INVALID":
                    return {
                        "status": "not_published_invalid_source",
                        "batch_id": batch_id,
                        "source_code": source_code,
                        "batch_ref": batch_ref,
                    }

                cursor.execute(
                    """
                    SELECT source_type, status, s3_bucket
                    FROM control.source_landing_file
                    WHERE batch_id = %s
                    """,
                    (batch_id,),
                )
                files = {
                    source_type: (status, bucket)
                    for source_type, status, bucket in cursor.fetchall()
                }
                ready = set(SOURCE_CONFIG).issubset(files) and all(
                    files[source_type][0] in SUCCESS_FILE_STATUSES
                    for source_type in SOURCE_CONFIG
                )
                if not ready:
                    return {
                        "status": "waiting_for_customer_file",
                        "batch_id": batch_id,
                        "source_code": source_code,
                        "batch_ref": batch_ref,
                        "available_sources": sorted(files),
                    }

                source_buckets = {
                    files[source_type][1] for source_type in SOURCE_CONFIG
                }
                if config.output_bucket:
                    output_bucket = config.output_bucket
                elif len(source_buckets) == 1:
                    output_bucket = next(iter(source_buckets))
                else:
                    raise ValueError("Unable to resolve the output S3 bucket")

                output_key = (
                    f"{config.output_root_prefix}{source_code}/"
                    f"{batch_id}/customer.csv"
                )
                cursor.execute(
                    """
                    UPDATE control.source_landing_batch
                    SET output_status     = 'BUILDING',
                        output_bucket     = %s,
                        output_key        = %s,
                        output_error      = NULL,
                        output_started_at = now(),
                        updated_at        = now()
                    WHERE batch_id = %s
                    """,
                    (output_bucket, output_key, batch_id),
                )

        LOGGER.info(
            "csv_build_started batch_id=%s source_code=%s",
            batch_id,
            source_code,
        )
        csv_bytes, row_count = _build_customer_csv(
            connection, batch_id, source_code
        )
        if row_count == 0:
            error = "valid_customer_rows_zero"
            _mark_output_failed(connection, batch_id, error)
            LOGGER.warning(
                "csv_not_published batch_id=%s reason=%s", batch_id, error
            )
            return {
                "status": "not_published_no_valid_customers",
                "batch_id": batch_id,
                "source_code": source_code,
                "batch_ref": batch_ref,
                "output_rows": 0,
            }

        sha256 = hashlib.sha256(csv_bytes).hexdigest()
        LOGGER.info(
            "s3_publish_started batch_id=%s bucket=%s key=%s rows=%d",
            batch_id,
            output_bucket,
            output_key,
            row_count,
        )
        etag, created = _publish_conditionally(
            s3_client,
            output_bucket,
            output_key,
            csv_bytes,
            batch_id,
            source_code,
            sha256,
            row_count,
        )

        with connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE control.source_landing_batch
                    SET status              = 'SOURCES_INGESTED',
                        output_status       = 'PUBLISHED',
                        output_bucket       = %s,
                        output_key          = %s,
                        output_row_count    = %s,
                        output_sha256       = %s,
                        output_etag         = %s,
                        output_error        = NULL,
                        output_published_at = now(),
                        updated_at          = now()
                    WHERE batch_id = %s
                    """,
                    (
                        output_bucket,
                        output_key,
                        row_count,
                        sha256,
                        etag,
                        batch_id,
                    ),
                )

        LOGGER.info(
            "s3_publish_complete batch_id=%s rows=%d created=%s",
            batch_id,
            row_count,
            created,
        )
        return {
            "status": "published",
            "batch_id": batch_id,
            "source_code": source_code,
            "batch_ref": batch_ref,
            "output_bucket": output_bucket,
            "output_key": output_key,
            "output_rows": row_count,
            "output_sha256": sha256,
            "created": created,
        }
    except Exception as exc:
        safe_error = f"output_failed:{type(exc).__name__}:{str(exc)[:1200]}"
        try:
            _mark_output_failed(connection, batch_id, safe_error)
        except Exception:
            LOGGER.exception(
                "output_failure_status_update_failed batch_id=%s", batch_id
            )
        LOGGER.exception("output_reconciliation_failed batch_id=%s", batch_id)
        raise
    finally:
        _release_output_lock(connection, batch_id)


def _process_source(
        source: SourceObject,
        downloaded: DownloadedObject,
        connection: Any,
        s3_client: Any,
        config: Config,
) -> dict[str, Any]:
    with connection:
        with connection.cursor() as cursor:
            batch_id = _resolve_batch(
                cursor, source.source_code, source.batch_ref
            )
            file_id, action = _register_file(
                cursor, batch_id, source, downloaded
            )

    LOGGER.info(
        "source_registered source_code=%s batch_ref=%s batch_id=%s "
        "source_type=%s file_format=%s action=%s",
        source.source_code,
        source.batch_ref,
        batch_id,
        source.source_type,
        source.file_format,
        action,
    )

    source_result: dict[str, Any]
    if action in {"processed", "in_progress"}:
        source_result = {
            "status": (
                "already_processed" if action == "processed" else "in_progress"
            ),
            "source_code": source.source_code,
            "batch_ref": source.batch_ref,
            "batch_id": batch_id,
            "file_id": file_id,
            "source_type": source.source_type,
            "file_format": source.file_format,
        }
    else:
        required_key = SOURCE_CONFIG[source.source_type]["required_key"]
        LOGGER.info(
            "source_parse_started batch_id=%s source_type=%s "
            "file_format=%s required_key=%s",
            batch_id,
            source.source_type,
            source.file_format,
            required_key,
        )
        try:
            parsed = parse_source_evidence(
                downloaded.content,
                source.file_format,
                required_key,
            )
        except Exception as exc:
            safe_error = (
                f"source_parse_failed:{source.file_format}:"
                f"{type(exc).__name__}:{str(exc)[:800]}"
            )
            with connection:
                with connection.cursor() as cursor:
                    _fail_file(cursor, file_id, safe_error)
                    batch_status = _refresh_batch_status(cursor, batch_id)
            LOGGER.exception(
                "source_parse_failed batch_id=%s source_type=%s file_format=%s",
                batch_id,
                source.source_type,
                source.file_format,
            )
            source_result = {
                "status": "invalid",
                "source_code": source.source_code,
                "batch_ref": source.batch_ref,
                "batch_id": batch_id,
                "file_id": file_id,
                "source_type": source.source_type,
                "file_format": source.file_format,
                "batch_status": batch_status,
                "error": safe_error,
            }
        else:
            LOGGER.info(
                "source_parse_complete batch_id=%s source_type=%s "
                "file_format=%s sheet=%s "
                "rows=%d columns=%d file_status=%s",
                batch_id,
                source.source_type,
                source.file_format,
                parsed.sheet_name,
                len(parsed.rows),
                len(parsed.column_names),
                parsed.file_status,
            )
            try:
                with connection:
                    with connection.cursor() as cursor:
                        affected = _replace_evidence_rows(
                            cursor,
                            source.source_type,
                            batch_id,
                            file_id,
                            parsed.rows,
                            config.upsert_page_size,
                        )
                        _complete_file(cursor, file_id, parsed)
                        batch_status = _refresh_batch_status(cursor, batch_id)
            except Exception as exc:
                safe_error = (
                    f"database_ingest_failed:{type(exc).__name__}:"
                    f"{str(exc)[:800]}"
                )
                try:
                    with connection:
                        with connection.cursor() as cursor:
                            _fail_file(cursor, file_id, safe_error)
                            _refresh_batch_status(cursor, batch_id)
                except Exception:
                    LOGGER.exception(
                        "file_failure_status_update_failed batch_id=%s file_id=%s",
                        batch_id,
                        file_id,
                    )
                raise

            source_result = {
                "status": (
                    "ingested"
                    if parsed.file_status in SUCCESS_FILE_STATUSES
                    else "invalid"
                ),
                "source_code": source.source_code,
                "batch_ref": source.batch_ref,
                "batch_id": batch_id,
                "file_id": file_id,
                "source_type": source.source_type,
                "file_format": source.file_format,
                "sheet_name": parsed.sheet_name,
                "input_rows": len(parsed.rows),
                "database_rows": affected,
                "blank_key_rows": parsed.blank_key_rows,
                "duplicate_key_rows": parsed.duplicate_key_rows,
                "file_status": parsed.file_status,
                "batch_status": batch_status,
            }
            LOGGER.info(
                "source_ingested batch_id=%s source_type=%s file_format=%s rows=%d "
                "blank_keys=%d duplicate_keys=%d file_status=%s",
                batch_id,
                source.source_type,
                source.file_format,
                len(parsed.rows),
                parsed.blank_key_rows,
                parsed.duplicate_key_rows,
                parsed.file_status,
            )

    output_result = _reconcile_output(
        connection, s3_client, config, batch_id
    )
    source_result["output"] = output_result
    return source_result


def lambda_handler(event: Mapping[str, Any], context: Any) -> dict[str, Any]:
    request_id = getattr(context, "aws_request_id", "unknown")
    event_records = event.get("Records", [])
    LOGGER.info(
        "handler_started request_id=%s event_records=%d",
        request_id,
        len(event_records),
    )

    config = _load_config()
    LOGGER.info(
        "config_loaded input_prefix=%s output_root_prefix=%s "
        "output_bucket_configured=%s page_size=%d",
        config.input_prefix,
        config.output_root_prefix,
        bool(config.output_bucket),
        config.upsert_page_size,
    )

    sources: list[SourceObject] = []
    rejected: list[dict[str, Any]] = []
    for record in event_records:
        try:
            source = _source_from_record(record, config)
            if source is not None:
                sources.append(source)
        except ValueError as exc:
            LOGGER.warning("source_event_rejected reason=%s", str(exc))
            rejected.append({"status": "rejected", "reason": str(exc)})

    if not sources:
        LOGGER.info(
            "handler_finished_no_sources rejected_records=%d", len(rejected)
        )
        return {
            "status": "ignored" if not rejected else "rejected",
            "processed_records": 0,
            "results": rejected,
        }

    LOGGER.info(
        "source_events_accepted accepted=%d rejected=%d",
        len(sources),
        len(rejected),
    )
    LOGGER.info("secret_load_started")
    secret = _get_secret(config.db_secret_id)
    LOGGER.info("secret_load_complete")
    LOGGER.info("database_connect_started")
    connection = _database_connection(secret)
    LOGGER.info("database_connect_complete")
    s3_client = boto3.client("s3")

    results = list(rejected)
    try:
        for source in sources:
            try:
                LOGGER.info(
                    "s3_download_started source_code=%s batch_ref=%s "
                    "source_type=%s file_format=%s bucket=%s key=%s",
                    source.source_code,
                    source.batch_ref,
                    source.source_type,
                    source.file_format,
                    source.bucket,
                    source.key,
                )
                downloaded = _download_object(s3_client, source)
                LOGGER.info(
                    "s3_download_complete source_code=%s batch_ref=%s "
                    "source_type=%s file_format=%s bytes=%d",
                    source.source_code,
                    source.batch_ref,
                    source.source_type,
                    source.file_format,
                    downloaded.content_length,
                )
                results.append(
                    _process_source(
                        source,
                        downloaded,
                        connection,
                        s3_client,
                        config,
                    )
                )
            except SourceConflictError as exc:
                LOGGER.warning(
                    "source_conflict source_code=%s batch_ref=%s "
                    "source_type=%s reason=%s",
                    source.source_code,
                    source.batch_ref,
                    source.source_type,
                    str(exc),
                )
                results.append(
                    {
                        "status": "rejected_conflict",
                        "source_code": source.source_code,
                        "batch_ref": source.batch_ref,
                        "source_type": source.source_type,
                        "file_format": source.file_format,
                        "reason": str(exc),
                    }
                )
    finally:
        connection.close()

    processed = sum(
        result.get("status")
        in {"ingested", "already_processed", "in_progress", "invalid"}
        for result in results
    )
    published = sum(
        (result.get("output") or {}).get("status") == "published"
        for result in results
    )
    LOGGER.info(
        "handler_finished request_id=%s processed=%d published=%d "
        "total_results=%d",
        request_id,
        processed,
        published,
        len(results),
    )
    return {
        "status": "completed" if processed else "rejected",
        "processed_records": processed,
        "published_outputs": published,
        "results": results,
    }
