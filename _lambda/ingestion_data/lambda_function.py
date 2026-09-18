"""S3 CSV -> PostgreSQL customer ingestion -> ECS Fargate stitching task."""

from __future__ import annotations

import csv
import io
import hashlib
import json
import logging
import os
import time
import uuid
from datetime import date, datetime, timezone
from pathlib import PurePosixPath
from typing import Any, Dict, List, Mapping, Tuple
from urllib.parse import unquote_plus

import boto3

LOGGER = logging.getLogger()
LOGGER.setLevel(logging.INFO)

VALIDATION_RULESET_VERSION = "customer-data-quality-v1"

REQUIRED_COLUMNS = ["source_customer_id"]

OPTIONAL_COLUMNS = [
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

CANONICAL_COLUMNS = REQUIRED_COLUMNS + OPTIONAL_COLUMNS

LEGACY_COLUMN_ALIASES = {
    "customer_id": "source_customer_id",
    "phone_number": "phone",
    "dob": "date_of_birth",
    "signup_date": "source_created_at",
    "address": "address_line_1",
    "city": "district",
    "state": "province",
}


def _log_stage(stage: str, started_at: float, **metrics: Any) -> None:
    LOGGER.info(
        json.dumps(
            {
                "event": "ingestion_stage_complete",
                "stage": stage,
                "duration_ms": round(
                    (time.perf_counter() - started_at) * 1000
                ),
                **metrics,
            },
            sort_keys=True,
        )
    )


def derive_source_code(key: str) -> str:
    """raw/pos/file.csv -> pos."""
    parts = PurePosixPath(key).parts
    if len(parts) < 3 or parts[0] != "raw":
        raise ValueError(f"Expected S3 key raw/<source_code>/<file>.csv, got {key}")
    return parts[1].strip().lower()


def _optional(value: Any) -> Any:
    value = str(value).strip()
    return value or None


def _json_object(value: Any, field_name: str) -> Dict[str, Any]:
    text = str(value).strip()
    if not text:
        return {}
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{field_name} must be a JSON object") from exc
    if not isinstance(parsed, dict):
        raise ValueError(f"{field_name} must be a JSON object")
    return parsed


def _date_value(value: Any) -> Any:
    text = str(value).strip()
    if not text:
        return None
    return date.fromisoformat(text)


def _timestamp_value(value: Any) -> Any:
    text = str(value).strip()
    if not text:
        return None
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _integer_value(value: Any, field_name: str) -> int | None:
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an integer") from exc


def prepare_customers(
        csv_bytes: bytes,
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """Read, validate, and deduplicate the canonical customer CSV."""
    text_stream = io.TextIOWrapper(
        io.BytesIO(csv_bytes),
        encoding="utf-8-sig",
        newline="",
    )
    reader = csv.DictReader(text_stream)
    fieldnames = reader.fieldnames or []
    available_columns = set(fieldnames)
    source_columns = {
        canonical: canonical
        for canonical in CANONICAL_COLUMNS
        if canonical in available_columns
    }
    for legacy, canonical in LEGACY_COLUMN_ALIASES.items():
        if canonical not in source_columns and legacy in available_columns:
            source_columns[canonical] = legacy

    missing = [
        column for column in REQUIRED_COLUMNS if column not in source_columns
    ]
    if missing:
        raise ValueError(f"Missing CSV columns: {', '.join(missing)}")

    received = 0
    rejected = 0
    valid_count = 0
    customers_by_source_id: Dict[str, Dict[str, Any]] = {}
    for input_row in reader:
        received += 1
        raw_payload = {
            column: (
                input_row.get(source_columns[column]) or ""
                if column in source_columns
                else ""
            )
            for column in CANONICAL_COLUMNS
        }
        row = {
            column: str(raw_payload[column]).strip()
            for column in CANONICAL_COLUMNS
        }
        source_customer_id = _optional(row["source_customer_id"])
        if source_customer_id is None:
            rejected += 1
            continue
        if len(source_customer_id) > 255:
            rejected += 1
            continue
        try:
            external_ids = _json_object(row["external_ids"], "external_ids")
            attributes = _json_object(row["attributes"], "attributes")
            date_of_birth = _date_value(row["date_of_birth"])
            birthday = _date_value(row["birthday"])
            age = _integer_value(row["age"], "age")
            source_created_at = _timestamp_value(row["source_created_at"])
            source_updated_at = _timestamp_value(row["source_updated_at"])
        except (TypeError, ValueError):
            rejected += 1
            continue

        devices = [
            item.strip()
            for item in str(input_row.get("device_id(s)") or "").split(";")
            if item.strip()
        ]
        if devices and not external_ids.get("device_ids"):
            external_ids["device_ids"] = list(dict.fromkeys(devices))
        acquisition_source = _optional(row["source"])
        if acquisition_source and "acquisition_source" not in attributes:
            attributes["acquisition_source"] = acquisition_source

        direct_identifiers = {
            "external_id": _optional(row["external_id"]),
            "line_user_id": _optional(row["line_user_id"]),
            "facebook_id": _optional(row["facebook_id"]),
            "google_id": _optional(row["google_id"]),
            "apple_id": _optional(row["apple_id"]),
            "member_code": _optional(row["member_code"]),
        }
        for name, value in direct_identifiers.items():
            if value is not None and name not in external_ids:
                external_ids[name] = value

        first_name = _optional(row["first_name"])
        middle_name = _optional(row["middle_name"])
        last_name = _optional(row["last_name"])
        full_name = _optional(row["full_name"]) or " ".join(
            part for part in (first_name, middle_name, last_name) if part
        ) or None

        customer = {
            "source_customer_id": source_customer_id,
            "external_id": direct_identifiers["external_id"],
            "first_name": first_name,
            "middle_name": middle_name,
            "last_name": last_name,
            "full_name": full_name,
            "nickname": _optional(row["nickname"]),
            "email": _optional(row["email"].lower()),
            "phone": _optional(row["phone"]),
            "address_line_1": _optional(row["address_line_1"]),
            "address_line_2": _optional(row["address_line_2"]),
            "district": _optional(row["district"]),
            "province": _optional(row["province"]),
            "postal_code": _optional(row["postal_code"]),
            "country": _optional(row["country"]),
            "date_of_birth": date_of_birth,
            "birthday": birthday,
            "age": age,
            "gender": _optional(row["gender"]),
            "line_user_id": direct_identifiers["line_user_id"],
            "facebook_id": direct_identifiers["facebook_id"],
            "google_id": direct_identifiers["google_id"],
            "apple_id": direct_identifiers["apple_id"],
            "member_code": direct_identifiers["member_code"],
            "customer_type": _optional(row["customer_type"]),
            "status": {"ใช้งาน": "ACTIVE", "ปิดใช้งาน": "INACTIVE"}.get(
                row["status"], _optional(row["status"].upper()) or "ACTIVE"
            ),
            "language": _optional(row["language"]),
            "source": _optional(row["source"]),
            "source_system": _optional(row["source_system"]),
            "external_ids": external_ids,
            "attributes": attributes,
            "raw_payload": raw_payload,
            "source_created_at": source_created_at,
            "source_updated_at": source_updated_at,
        }
        valid_count += 1
        customers_by_source_id[customer["source_customer_id"]] = customer

    customers = list(customers_by_source_id.values())
    duplicates = valid_count - len(customers)

    return customers, {
        "records_received": received,
        "records_rejected": rejected,
        "duplicate_rows": duplicates,
    }


def _connect() -> Any:
    import psycopg2

    region = os.getenv("AWS_REGION", "ap-southeast-7")
    secret_id = os.getenv("DB_SECRET_ID", "marketing-automation-secret")
    client = boto3.client("secretsmanager", region_name=region)
    response = client.get_secret_value(SecretId=secret_id)
    db_creds = json.loads(response["SecretString"])
    LOGGER.info("database_secret_loaded")

    return psycopg2.connect(
        host=db_creds["host"],
        port=int(os.getenv("DB_PORT", "5432")),
        dbname=db_creds["dbname"],
        user=db_creds["username"],
        password=db_creds["password"],
        sslmode=os.getenv("DB_SSLMODE", "require"),
        connect_timeout=15,
    )


def _upsert_customers(
        cursor: Any,
        customers: List[Dict[str, Any]],
        batch_id: str,
        source_system_id: int,
) -> None:
    from psycopg2.extras import Json, execute_values

    if not customers:
        return

    page_size = int(os.getenv("UPSERT_PAGE_SIZE", "1000"))
    if page_size <= 0:
        raise ValueError("UPSERT_PAGE_SIZE must be greater than zero")

    upsert_sql = """
        INSERT INTO raw.customer (raw_customer_id, batch_id, source_system_id, source_customer_id,
                                  external_id, first_name, middle_name, last_name, full_name,
                                  nickname, gender, date_of_birth, birthday, age, email, phone,
                                  line_user_id, facebook_id, google_id, apple_id, member_code,
                                  customer_type, status, language, address_line_1, address_line_2,
                                  district, province, postal_code, country, source, source_system,
                                  external_ids, attributes, raw_payload, source_created_at,
                                  source_updated_at, ingested_at)
        VALUES %s
        ON CONFLICT (source_system_id, source_customer_id)
        DO UPDATE SET
        batch_id = EXCLUDED.batch_id,
        external_id = EXCLUDED.external_id,
        first_name = EXCLUDED.first_name,
        middle_name = EXCLUDED.middle_name,
        last_name = EXCLUDED.last_name,
        full_name = EXCLUDED.full_name,
        nickname = EXCLUDED.nickname,
        gender = EXCLUDED.gender,
        date_of_birth = EXCLUDED.date_of_birth,
        birthday = EXCLUDED.birthday,
        age = EXCLUDED.age,
        email = EXCLUDED.email,
        phone = EXCLUDED.phone,
        line_user_id = EXCLUDED.line_user_id,
        facebook_id = EXCLUDED.facebook_id,
        google_id = EXCLUDED.google_id,
        apple_id = EXCLUDED.apple_id,
        member_code = EXCLUDED.member_code,
        customer_type = EXCLUDED.customer_type,
        status = EXCLUDED.status,
        language = EXCLUDED.language,
        address_line_1 = EXCLUDED.address_line_1,
        address_line_2 = EXCLUDED.address_line_2,
        district = EXCLUDED.district,
        province = EXCLUDED.province,
        postal_code = EXCLUDED.postal_code,
        country = EXCLUDED.country,
        source = EXCLUDED.source,
        source_system = EXCLUDED.source_system,
        external_ids = EXCLUDED.external_ids,
        attributes = EXCLUDED.attributes,
        raw_payload = EXCLUDED.raw_payload,
        source_created_at = EXCLUDED.source_created_at,
        source_updated_at = EXCLUDED.source_updated_at,
        updated_at = now(),
        ingested_at = now()
        """
    template = "(" + ",".join(["%s"] * 37) + ",now())"

    total_pages = (len(customers) + page_size - 1) // page_size
    for page_number, offset in enumerate(
            range(0, len(customers), page_size),
            start=1,
    ):
        rows = []
        for customer in customers[offset:offset + page_size]:
            rows.append(
                (
                    str(uuid.uuid4()),
                    batch_id,
                    source_system_id,
                    customer["source_customer_id"],
                    customer["external_id"],
                    customer["first_name"],
                    customer["middle_name"],
                    customer["last_name"],
                    customer["full_name"],
                    customer["nickname"],
                    customer["gender"],
                    customer["date_of_birth"],
                    customer["birthday"],
                    customer["age"],
                    customer["email"],
                    customer["phone"],
                    customer["line_user_id"],
                    customer["facebook_id"],
                    customer["google_id"],
                    customer["apple_id"],
                    customer["member_code"],
                    customer["customer_type"],
                    customer["status"],
                    customer["language"],
                    customer["address_line_1"],
                    customer["address_line_2"],
                    customer["district"],
                    customer["province"],
                    customer["postal_code"],
                    customer["country"],
                    customer["source"],
                    customer["source_system"],
                    Json(customer["external_ids"]),
                    Json(customer["attributes"]),
                    Json(customer["raw_payload"]),
                    customer["source_created_at"],
                    customer["source_updated_at"],
                )
            )
        execute_values(
            cursor,
            upsert_sql,
            rows,
            template=template,
            page_size=page_size,
        )
        if (
                page_number == 1
                or page_number == total_pages
                or page_number % 10 == 0
        ):
            LOGGER.info(
                json.dumps(
                    {
                        "event": "ingestion_upsert_progress",
                        "page": page_number,
                        "total_pages": total_pages,
                        "records_processed": min(
                            offset + page_size, len(customers)
                        ),
                        "records_total": len(customers),
                    },
                    sort_keys=True,
                )
            )


def _landing_metadata(response: Mapping[str, Any]) -> Dict[str, str]:
    metadata = {
        str(name).casefold(): str(value).strip()
        for name, value in (response.get("Metadata") or {}).items()
    }
    required = (
        "landing-batch-id",
        "source-code",
        "content-sha256",
        "row-count",
    )
    missing = [name for name in required if not metadata.get(name)]
    if missing:
        raise ValueError(
            "Canonical customer object is missing required landing metadata: "
            + ", ".join(missing)
        )
    try:
        metadata["landing-batch-id"] = str(
            uuid.UUID(metadata["landing-batch-id"])
        )
    except ValueError as exc:
        raise ValueError("landing-batch-id metadata must be a UUID") from exc
    return metadata


def _resolve_landing_lineage(
        cursor: Any,
        *,
        landing_metadata: Mapping[str, str],
        source_code: str,
        object_key: str,
        checksum: str,
        loaded_rows: int,
) -> Tuple[str, str]:
    landing_batch_id = landing_metadata["landing-batch-id"]
    if landing_metadata["source-code"] != source_code:
        raise ValueError("Landing metadata source-code does not match object key")
    if landing_metadata["content-sha256"] != checksum:
        raise ValueError("Landing metadata checksum does not match object body")
    try:
        metadata_rows = int(landing_metadata["row-count"])
    except ValueError as exc:
        raise ValueError("Landing metadata row-count must be an integer") from exc
    if metadata_rows != loaded_rows:
        raise ValueError(
            "Landing metadata row-count does not match valid customer rows"
        )

    cursor.execute(
        """
        SELECT batch_ref,
               source_code,
               output_status,
               output_key,
               output_sha256,
               output_row_count
        FROM control.source_landing_batch
        WHERE batch_id = %s
        FOR SHARE
        """,
        (landing_batch_id,),
    )
    landing = cursor.fetchone()
    if landing is None:
        raise ValueError(f"Landing batch not found: {landing_batch_id}")
    (
        batch_ref,
        stored_source_code,
        output_status,
        output_key,
        output_sha256,
        output_row_count,
    ) = landing
    if output_status != "PUBLISHED":
        raise ValueError(f"Landing batch is not published: {landing_batch_id}")
    if stored_source_code != source_code or output_key != object_key:
        raise ValueError("Landing batch output does not match the S3 object")
    if str(output_sha256 or "").strip() != checksum:
        raise ValueError("Landing batch checksum does not match the S3 object")
    if int(output_row_count or 0) != loaded_rows:
        raise ValueError("Landing batch output row count does not match ingestion")
    return landing_batch_id, str(batch_ref)


def _record_batch_members(
        cursor: Any,
        batch_id: str,
        source_system_id: int,
        expected_records: int,
) -> None:
    cursor.execute(
        """
        INSERT INTO raw.customer_batch_member (
            ingestion_batch_id,
            raw_customer_id,
            source_system_id,
            source_customer_id
        )
        SELECT %s,
               raw_customer_id,
               source_system_id,
               source_customer_id
        FROM raw.customer
        WHERE batch_id = %s
          AND source_system_id = %s
        ON CONFLICT DO NOTHING
        """,
        (batch_id, batch_id, source_system_id),
    )
    cursor.execute(
        """
        SELECT count(*)
        FROM raw.customer_batch_member
        WHERE ingestion_batch_id = %s
          AND source_system_id = %s
        """,
        (batch_id, source_system_id),
    )
    recorded = int(cursor.fetchone()[0])
    if recorded != expected_records:
        raise RuntimeError(
            f"Recorded {recorded} of {expected_records} batch members"
        )


def _create_validation_run(
        cursor: Any,
        *,
        landing_batch_id: str,
        ingestion_batch_id: str,
        stitching_job_id: str,
        attempt: int,
) -> str:
    cursor.execute(
        """
        WITH grouped_keys AS (
            SELECT NULLIF(btrim(source_customer_id), '') AS source_key,
                   count(*)::bigint AS row_count
            FROM raw.customer_landing_source
            WHERE batch_id = %s
            GROUP BY NULLIF(btrim(source_customer_id), '')
        ), source_stats AS (
            SELECT COALESCE(sum(row_count), 0)::bigint
                       AS source_row_count,
                   COALESCE(
                       sum(row_count) FILTER (WHERE source_key IS NULL),
                       0
                   )::bigint AS blank_key_row_count,
                   count(*) FILTER (
                       WHERE source_key IS NOT NULL AND row_count > 1
                   )::bigint
                       AS duplicate_group_count,
                   COALESCE(
                       sum(row_count) FILTER (
                           WHERE source_key IS NOT NULL AND row_count > 1
                       ),
                       0
                   )::bigint
                       AS duplicate_affected_row_count,
                   COALESCE(
                       sum(row_count - 1) FILTER (
                           WHERE source_key IS NOT NULL AND row_count > 1
                       ),
                       0
                   )::bigint
                       AS duplicate_excess_row_count
            FROM grouped_keys
        )
        INSERT INTO quality.customer_validation_run (
            landing_batch_id,
            ingestion_batch_id,
            stitching_job_id,
            attempt,
            run_status,
            ruleset_version,
            source_row_count,
            canonical_row_count,
            blank_key_row_count,
            duplicate_group_count,
            duplicate_affected_row_count,
            duplicate_excess_row_count
        )
        SELECT %s,
               %s,
               %s,
               %s,
               'PENDING',
               %s,
               source_stats.source_row_count,
               COALESCE(slb.output_row_count, 0),
               source_stats.blank_key_row_count,
               source_stats.duplicate_group_count,
               source_stats.duplicate_affected_row_count,
               source_stats.duplicate_excess_row_count
        FROM source_stats
        JOIN control.source_landing_batch AS slb
          ON slb.batch_id = %s
        RETURNING validation_run_id::text
        """,
        (
            landing_batch_id,
            landing_batch_id,
            ingestion_batch_id,
            stitching_job_id,
            attempt,
            VALIDATION_RULESET_VERSION,
            landing_batch_id,
        ),
    )
    row = cursor.fetchone()
    if row is None:
        raise RuntimeError("Unable to create customer validation run")
    return str(row[0])


def _seed_validation_records(
        cursor: Any,
        *,
        validation_run_id: str,
        landing_batch_id: str,
        source_system_id: int,
) -> None:
    cursor.execute(
        """
        WITH ranked AS (
            SELECT source.source_record_id,
                   source.source_row_number,
                   source.source_customer_id,
                   count(*) FILTER (
                       WHERE source.source_customer_id IS NOT NULL
                         AND btrim(source.source_customer_id) <> ''
                   ) OVER (
                       PARTITION BY source.source_customer_id
                   ) AS source_key_count,
                   row_number() OVER (
                       PARTITION BY source.source_customer_id
                       ORDER BY source.source_row_number DESC,
                                source.source_record_id DESC
                   ) AS source_key_rank
            FROM raw.customer_landing_source AS source
            WHERE source.batch_id = %s
        ), classified AS (
            SELECT ranked.*,
                   ranked.source_customer_id IS NOT NULL
                     AND btrim(ranked.source_customer_id) <> ''
                     AND ranked.source_key_rank = 1
                       AS selected_for_canonical
            FROM ranked
        )
        INSERT INTO quality.customer_validation_record (
            validation_run_id,
            source_record_id,
            raw_customer_id,
            source_row_number,
            selected_for_canonical,
            disposition,
            existing_source_record,
            preexisting_entity_id,
            identity_classification,
            record_fingerprint,
            issue_codes
        )
        SELECT %s,
               classified.source_record_id,
               customer.raw_customer_id,
               classified.source_row_number,
               classified.selected_for_canonical,
               CASE
                   WHEN classified.source_customer_id IS NULL
                     OR btrim(classified.source_customer_id) = ''
                       THEN 'EXCLUDED'
                   WHEN classified.source_key_count > 1
                    AND classified.source_key_rank > 1
                       THEN 'EXCLUDED'
                   WHEN source.validation_errors IS NOT NULL
                       THEN 'ACCEPTED_WITH_ISSUES'
                   ELSE 'ACCEPTED'
               END,
               existing.raw_customer_id IS NOT NULL,
               existing.preexisting_entity_id,
               CASE
                   WHEN existing.raw_customer_id IS NOT NULL
                       THEN 'EXISTING_SOURCE_RECORD'
                   ELSE 'NOT_EVALUATED'
               END,
               encode(
                   digest(source.raw_payload::text, 'sha256'),
                   'hex'
               ),
               COALESCE(
                   ARRAY(
                       SELECT jsonb_array_elements_text(
                           source.validation_errors
                       )
                   ),
                   '{}'::text[]
               )
        FROM classified
        JOIN raw.customer_landing_source AS source
          ON source.batch_id = %s
         AND source.source_record_id = classified.source_record_id
        LEFT JOIN raw.customer AS customer
          ON classified.selected_for_canonical
         AND customer.source_system_id = %s
         AND customer.source_customer_id::text =
             classified.source_customer_id
        LEFT JOIN ingestion_existing_source_snapshot AS existing
          ON existing.source_record_id = classified.source_record_id
        """,
        (
            landing_batch_id,
            validation_run_id,
            landing_batch_id,
            source_system_id,
        ),
    )


def _create_existing_source_snapshot(
        cursor: Any,
        *,
        landing_batch_id: str,
        source_system_id: int,
) -> None:
    cursor.execute(
        """
        CREATE TEMP TABLE ingestion_existing_source_snapshot
        ON COMMIT DROP AS
        WITH selected_source AS (
            SELECT DISTINCT ON (source_customer_id)
                   source_record_id,
                   source_customer_id
            FROM raw.customer_landing_source
            WHERE batch_id = %s
              AND source_customer_id IS NOT NULL
              AND btrim(source_customer_id) <> ''
            ORDER BY source_customer_id,
                     source_row_number DESC,
                     source_record_id DESC
        )
        SELECT selected_source.source_record_id,
               customer.raw_customer_id,
               member.customer_entity_id AS preexisting_entity_id
        FROM selected_source
        JOIN raw.customer AS customer
          ON customer.source_system_id = %s
         AND customer.source_customer_id::text =
             selected_source.source_customer_id
        LEFT JOIN staging.customer_profile AS profile
          ON profile.raw_customer_id = customer.raw_customer_id
        LEFT JOIN identity.customer_entity_member AS member
          ON member.standardized_record_id = profile.standardized_record_id
        """,
        (landing_batch_id, source_system_id),
    )
    cursor.execute(
        """
        CREATE UNIQUE INDEX
            ON ingestion_existing_source_snapshot (source_record_id)
        """
    )
    cursor.execute("ANALYZE ingestion_existing_source_snapshot")


def _copy_validation_records(
        cursor: Any,
        *,
        validation_run_id: str,
        previous_validation_run_id: str,
) -> None:
    cursor.execute(
        """
        INSERT INTO quality.customer_validation_record (
            validation_run_id,
            source_record_id,
            raw_customer_id,
            source_row_number,
            selected_for_canonical,
            disposition,
            existing_source_record,
            preexisting_entity_id,
            identity_classification,
            entity_candidate_count,
            matched_identifier_types,
            record_fingerprint,
            issue_codes
        )
        SELECT %s,
               source_record_id,
               raw_customer_id,
               source_row_number,
               selected_for_canonical,
               disposition,
               existing_source_record,
               preexisting_entity_id,
               'NOT_EVALUATED',
               0,
               '{}'::text[],
               record_fingerprint,
               issue_codes
        FROM quality.customer_validation_record
        WHERE validation_run_id = %s
        ORDER BY source_row_number
        """,
        (validation_run_id, previous_validation_run_id),
    )


def _process_record(
        record: Mapping[str, Any],
        s3: Any,
        connection: Any,
) -> Dict[str, Any]:
    from psycopg2.extras import Json

    bucket = record["s3"]["bucket"]["name"]
    obj = record["s3"]["object"]
    key = unquote_plus(obj["key"])
    etag = obj["eTag"].strip('"')
    source_code = derive_source_code(key)

    stage_started = time.perf_counter()
    response = s3.get_object(Bucket=bucket, Key=key, IfMatch=etag)
    csv_bytes = response["Body"].read()
    _log_stage("s3_download", stage_started, bytes=len(csv_bytes))

    stage_started = time.perf_counter()
    customers, stats = prepare_customers(csv_bytes)
    _log_stage(
        "prepare_customers",
        stage_started,
        records_received=stats["records_received"],
        records_loaded=len(customers),
        records_rejected=stats["records_rejected"],
    )
    if not customers:
        raise ValueError("Customer file contains no valid customer rows")
    checksum = hashlib.sha256(csv_bytes).hexdigest()
    landing_metadata = _landing_metadata(response)
    del csv_bytes
    batch_id = str(uuid.uuid4())

    # The context commits before this function returns. ECS is started later.
    transaction_started = time.perf_counter()
    with connection:
        with connection.cursor() as cursor:
            stage_started = time.perf_counter()
            cursor.execute(
                "SELECT set_config('work_mem', %s, true)",
                (os.getenv("INGESTION_WORK_MEM", "32MB"),),
            )
            cursor.execute(
                "SELECT set_config('statement_timeout', %s, true)",
                (
                    os.getenv(
                        "INGESTION_STATEMENT_TIMEOUT",
                        "13min",
                    ),
                ),
            )
            cursor.execute(
                """
                INSERT INTO control.source_system (source_code, source_name, source_type, is_active)
                VALUES (%s, %s, 's3_csv', TRUE)
                ON CONFLICT (source_code) DO UPDATE SET is_active = TRUE
                RETURNING source_system_id
                """,
                (source_code, source_code.upper()),
            )
            source_system_id = cursor.fetchone()[0]

            landing_batch_id, batch_ref = _resolve_landing_lineage(
                cursor,
                landing_metadata=landing_metadata,
                source_code=source_code,
                object_key=key,
                checksum=checksum,
                loaded_rows=len(customers),
            )

            cursor.execute(
                """
                INSERT INTO control.ingestion_batch (
                    batch_id,
                    source_system_id,
                    object_name,
                    file_name,
                    status,
                    checksum,
                    metadata,
                    batch_ref,
                    landing_batch_id
                )
                VALUES (%s, %s, %s, %s, 'processing', %s, %s, %s, %s)
                ON CONFLICT (source_system_id, object_name, checksum)
                    DO UPDATE SET status = 'processing',
                                  completed_at = NULL,
                                  updated_at = now()
                    WHERE ingestion_batch.status = 'failed'
                      AND ingestion_batch.metadata ? 'ingestion_error_code'
                      AND ingestion_batch.landing_batch_id = EXCLUDED.landing_batch_id
                RETURNING batch_id::text
                """,
                (
                    batch_id,
                    source_system_id,
                    key,
                    PurePosixPath(key).name,
                    checksum,
                    Json(
                        {
                            "bucket": bucket,
                            "event_time": record.get("eventTime"),
                            "landing_batch_id": landing_batch_id,
                        }
                    ),
                    batch_ref,
                    landing_batch_id,
                ),
            )
            inserted = cursor.fetchone()
            if inserted is None:
                cursor.execute(
                    """
                    SELECT batch_id::text,
                           status,
                           metadata ->> 'stitching_task_arn',
                           landing_batch_id::text
                    FROM control.ingestion_batch
                    WHERE source_system_id = %s
                      AND object_name = %s
                      AND checksum = %s
                    """,
                    (source_system_id, key, checksum),
                )
                existing = cursor.fetchone()
                if existing is None:
                    raise RuntimeError("Existing ingestion batch could not be resolved")
                (
                    existing_batch_id,
                    existing_status,
                    task_arn,
                    existing_landing_batch_id,
                ) = existing
                if existing_landing_batch_id != landing_batch_id:
                    raise RuntimeError(
                        "Existing ingestion batch has conflicting landing lineage"
                    )
                if existing_status != "completed":
                    raise RuntimeError(
                        f"Existing ingestion batch {existing_batch_id} is not completed"
                    )
                return {
                    "status": "skipped",
                    "reason": "file already completed",
                    "key": key,
                    "batch_id": existing_batch_id,
                    "source_code": source_code,
                    "stitching_task_arn": task_arn,
                }

            batch_id = inserted[0]
            failure = None
            cursor.execute("SAVEPOINT ingestion_payload")
            try:
                cursor.execute(
                    """
                    INSERT INTO control.stitching_job (
                        ingestion_batch_id, attempt, status
                    )
                    VALUES (%s, 1, 'QUEUED')
                    RETURNING job_id::text
                    """,
                    (batch_id,),
                )
                stitching_job_id = str(cursor.fetchone()[0])
                validation_run_id = _create_validation_run(
                    cursor,
                    landing_batch_id=landing_batch_id,
                    ingestion_batch_id=batch_id,
                    stitching_job_id=stitching_job_id,
                    attempt=1,
                )
                _log_stage("database_setup", stage_started)

                stage_started = time.perf_counter()
                _create_existing_source_snapshot(
                    cursor,
                    landing_batch_id=landing_batch_id,
                    source_system_id=source_system_id,
                )
                _log_stage("snapshot_existing_records", stage_started)

                stage_started = time.perf_counter()
                _upsert_customers(cursor, customers, batch_id, source_system_id)
                _log_stage(
                    "upsert_customers",
                    stage_started,
                    records=len(customers),
                )

                stage_started = time.perf_counter()
                _seed_validation_records(
                    cursor,
                    validation_run_id=validation_run_id,
                    landing_batch_id=landing_batch_id,
                    source_system_id=source_system_id,
                )
                _log_stage("seed_validation_records", stage_started)

                stage_started = time.perf_counter()
                _record_batch_members(
                    cursor,
                    batch_id,
                    source_system_id,
                    len(customers),
                )
                _log_stage(
                    "record_batch_members",
                    stage_started,
                    records=len(customers),
                )
                cursor.execute(
                    """
                    UPDATE quality.customer_validation_run
                    SET selected_record_count = %s,
                        existing_source_record_count = (
                            SELECT count(*)
                            FROM quality.customer_validation_record
                            WHERE validation_run_id = %s
                              AND existing_source_record
                              AND selected_for_canonical
                        ),
                        updated_at = now()
                    WHERE validation_run_id = %s
                      AND run_status = 'PENDING'
                    """,
                    (
                        len(customers),
                        validation_run_id,
                        validation_run_id,
                    ),
                )
                if cursor.rowcount != 1:
                    raise RuntimeError(
                        f"Unable to initialize validation run {validation_run_id}"
                    )
                metadata = {
                    "bucket": bucket,
                    "event_time": record.get("eventTime"),
                    "duplicate_rows": stats["duplicate_rows"],
                    "landing_batch_id": landing_batch_id,
                    "stitching_job_id": stitching_job_id,
                    "validation_run_id": validation_run_id,
                }
                cursor.execute(
                    """
                    UPDATE control.ingestion_batch
                    SET status           = 'completed',
                        completed_at     = now(),
                        records_received = %s,
                        records_loaded   = %s,
                        records_rejected = %s,
                        metadata         = %s
                    WHERE batch_id = %s
                    """,
                    (
                        stats["records_received"],
                        len(customers),
                        stats["records_rejected"],
                        Json(metadata),
                        batch_id,
                    ),
                )
            except Exception as exc:
                # Roll back customer/snapshot/job writes, not the batch identity.
                # Re-raise AFTER committing failure so S3/Lambda still retries.
                cursor.execute("ROLLBACK TO SAVEPOINT ingestion_payload")
                cursor.execute(
                    """
                    UPDATE control.ingestion_batch
                    SET status = 'failed',
                        completed_at = now(),
                        updated_at = now(),
                        metadata = COALESCE(metadata, '{}'::jsonb) || %s
                    WHERE batch_id = %s
                    """,
                    (Json({"ingestion_error_code": type(exc).__name__}), batch_id),
                )
                failure = exc
    if failure is not None:
        # Database exception details can contain an entire customer row.
        raise RuntimeError(
            f"Customer ingestion failed for batch {batch_id}: "
            f"{type(failure).__name__}"
        ) from None
    _log_stage(
        "database_transaction",
        transaction_started,
        records=len(customers),
    )

    return {
        "status": "completed",
        "batch_id": batch_id,
        "landing_batch_id": landing_batch_id,
        "source_code": source_code,
        "records_received": stats["records_received"],
        "records_loaded": len(customers),
        "records_rejected": stats["records_rejected"],
        "duplicate_rows": stats["duplicate_rows"],
        "stitching_job_id": stitching_job_id,
        "validation_run_id": validation_run_id,
        "stitching_task_arn": None,
    }


def _required_environment(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ValueError(f"Missing required environment variable: {name}")
    return value


def _environment_list(name: str) -> List[str]:
    values = [
        value.strip()
        for value in _required_environment(name).split(",")
        if value.strip()
    ]
    if not values:
        raise ValueError(f"Environment variable {name} contains no values")
    return values


def _resolve_ecs_container_name(ecs: Any, task_definition: str) -> str:
    configured = os.getenv("ECS_CONTAINER_NAME", "").strip()
    if configured:
        return configured

    response = ecs.describe_task_definition(taskDefinition=task_definition)
    containers = (response.get("taskDefinition") or {}).get(
        "containerDefinitions"
    ) or []
    essential = [container for container in containers if container.get("essential", True)]
    if len(essential) != 1 or not essential[0].get("name"):
        raise RuntimeError(
            "ECS_CONTAINER_NAME is required unless the task definition has "
            "exactly one essential container"
        )
    return str(essential[0]["name"])


def _start_stitching_task(batch_id: str, job_id: str) -> str:
    assign_public_ip = os.getenv("ECS_ASSIGN_PUBLIC_IP", "DISABLED").upper()
    if assign_public_ip not in {"ENABLED", "DISABLED"}:
        raise ValueError("ECS_ASSIGN_PUBLIC_IP must be ENABLED or DISABLED")

    ecs = boto3.client("ecs")
    task_definition = _required_environment("ECS_TASK_DEFINITION")
    container_name = _resolve_ecs_container_name(ecs, task_definition)
    response = ecs.run_task(
        cluster=_required_environment("ECS_CLUSTER"),
        taskDefinition=task_definition,
        launchType="FARGATE",
        platformVersion=os.getenv("ECS_PLATFORM_VERSION", "LATEST"),
        count=1,
        clientToken=f"stitch-{job_id}",
        startedBy=job_id,
        overrides={
            "containerOverrides": [
                {
                    "name": container_name,
                    "command": [
                        "run",
                        "--batch-id",
                        batch_id,
                        "--job-id",
                        job_id,
                    ],
                    "environment": [
                        {
                            "name": "STITCHING_LOCK_WAIT_SECONDS",
                            "value": os.getenv(
                                "STITCHING_LOCK_WAIT_SECONDS", "900"
                            ),
                        },
                        {
                            "name": "STITCHING_LOCK_POLL_SECONDS",
                            "value": os.getenv(
                                "STITCHING_LOCK_POLL_SECONDS", "5"
                            ),
                        },
                    ],
                }
            ]
        },
        networkConfiguration={
            "awsvpcConfiguration": {
                "subnets": _environment_list("ECS_SUBNET_IDS"),
                "securityGroups": _environment_list(
                    "ECS_SECURITY_GROUP_IDS"
                ),
                "assignPublicIp": assign_public_ip,
            }
        },
        enableECSManagedTags=True,
    )
    failures = response.get("failures") or []
    if failures:
        raise RuntimeError(f"ECS RunTask failed: {failures}")
    tasks = response.get("tasks") or []
    if not tasks or not tasks[0].get("taskArn"):
        raise RuntimeError("ECS RunTask returned no task ARN")
    return str(tasks[0]["taskArn"])


def _ensure_stitching_job(
        connection: Any,
        batch_id: str,
) -> Dict[str, Any]:
    with connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
                (f"stitching-job:{batch_id}",),
            )
            cursor.execute(
                """
                SELECT sj.job_id::text,
                       sj.attempt,
                       sj.status,
                       sj.ecs_task_arn,
                       vr.validation_run_id::text,
                       ib.landing_batch_id::text
                FROM control.stitching_job AS sj
                JOIN control.ingestion_batch AS ib
                  ON ib.batch_id = sj.ingestion_batch_id
                LEFT JOIN quality.customer_validation_run AS vr
                  ON vr.stitching_job_id = sj.job_id
                WHERE sj.ingestion_batch_id = %s
                ORDER BY sj.attempt DESC
                LIMIT 1
                """,
                (batch_id,),
            )
            existing = cursor.fetchone()
            if existing and existing[2] not in {"FAILED"}:
                if existing[2] not in {"SUCCEEDED", "LEGACY_UNKNOWN"} and not existing[4]:
                    raise RuntimeError(
                        f"Validation run missing for stitching job {existing[0]}"
                    )
                return {
                    "job_id": existing[0],
                    "attempt": int(existing[1]),
                    "status": existing[2],
                    "task_arn": existing[3],
                    "validation_run_id": existing[4],
                }

            if existing is None:
                raise RuntimeError(
                    "Cannot create a validation snapshot for a historical "
                    f"ingestion batch without arrival-time facts: {batch_id}"
                )

            previous_validation_run_id = existing[4]
            landing_batch_id = existing[5]
            if not previous_validation_run_id or not landing_batch_id:
                raise RuntimeError(
                    "Cannot retry stitching without the previous validation "
                    f"snapshot and landing lineage: {batch_id}"
                )

            attempt = int(existing[1]) + 1
            cursor.execute(
                """
                INSERT INTO control.stitching_job (
                    ingestion_batch_id, attempt, status
                )
                VALUES (%s, %s, 'QUEUED')
                RETURNING job_id::text
                """,
                (batch_id, attempt),
            )
            job_id = str(cursor.fetchone()[0])
            validation_run_id = _create_validation_run(
                cursor,
                landing_batch_id=landing_batch_id,
                ingestion_batch_id=batch_id,
                stitching_job_id=job_id,
                attempt=attempt,
            )
            _copy_validation_records(
                cursor,
                validation_run_id=validation_run_id,
                previous_validation_run_id=previous_validation_run_id,
            )
            cursor.execute(
                """
                UPDATE quality.customer_validation_run
                SET selected_record_count = (
                        SELECT count(*)
                        FROM quality.customer_validation_record
                        WHERE validation_run_id = %s
                          AND selected_for_canonical
                    ),
                    existing_source_record_count = (
                        SELECT count(*)
                        FROM quality.customer_validation_record
                        WHERE validation_run_id = %s
                          AND existing_source_record
                          AND selected_for_canonical
                    ),
                    updated_at = now()
                WHERE validation_run_id = %s
                """,
                (
                    validation_run_id,
                    validation_run_id,
                    validation_run_id,
                ),
            )
            return {
                "job_id": job_id,
                "attempt": attempt,
                "status": "QUEUED",
                "task_arn": None,
                "validation_run_id": validation_run_id,
            }


def _record_stitching_dispatch(
        connection: Any,
        batch_id: str,
        job_id: str,
        task_arn: str,
) -> None:
    from psycopg2.extras import Json

    with connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE control.stitching_job
                SET status = CASE
                                 WHEN status = 'QUEUED' THEN 'SUBMITTED'
                                 ELSE status
                             END,
                    ecs_task_arn = COALESCE(ecs_task_arn, %s),
                    submitted_at = COALESCE(submitted_at, now()),
                    updated_at = now()
                WHERE job_id = %s
                  AND ingestion_batch_id = %s
                  AND (ecs_task_arn IS NULL OR ecs_task_arn = %s)
                  AND status IN (
                      'QUEUED', 'SUBMITTED', 'WAITING_FOR_LOCK',
                      'VALIDATING', 'RUNNING', 'SUCCEEDED', 'FAILED'
                  )
                """,
                (task_arn, job_id, batch_id, task_arn),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(
                    f"Unable to record stitching job dispatch for {job_id}"
                )
            cursor.execute(
                """
                UPDATE control.ingestion_batch
                SET metadata   = COALESCE(metadata, '{}'::jsonb) || %s,
                    updated_at = now()
                WHERE batch_id = %s
                  AND status = 'completed'
                """,
                (
                    Json(
                        {
                            "stitching_job_id": job_id,
                            "stitching_task_arn": task_arn,
                            "stitching_dispatch_status": "submitted",
                        }
                    ),
                    batch_id,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError(
                    f"Unable to record stitching task for batch {batch_id}"
                )


def _mark_stitching_dispatch_failed(
        connection: Any,
        batch_id: str,
        job_id: str,
        error: Exception,
) -> None:
    from psycopg2.extras import Json

    message = f"{type(error).__name__}: {str(error)}"[:2000]
    with connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE control.stitching_job
                SET status        = 'FAILED',
                    completed_at  = now(),
                    error_code    = %s,
                    error_message = %s,
                    updated_at    = now()
                WHERE job_id = %s
                  AND status NOT IN ('SUCCEEDED', 'FAILED')
                """,
                (type(error).__name__, message, job_id),
            )
            cursor.execute(
                """
                UPDATE quality.customer_validation_run
                SET run_status    = 'FAILED',
                    error_stage   = 'DISPATCH',
                    error_code    = %s,
                    error_message = %s,
                    completed_at  = now(),
                    updated_at    = now()
                WHERE stitching_job_id = %s
                  AND run_status IN ('PENDING', 'RUNNING')
                """,
                (type(error).__name__, message, job_id),
            )
            cursor.execute(
                """
                UPDATE control.ingestion_batch
                SET metadata = COALESCE(metadata, '{}'::jsonb) || %s,
                    updated_at = now()
                WHERE batch_id = %s
                """,
                (
                    Json(
                        {
                            "stitching_job_id": job_id,
                            "stitching_dispatch_status": "failed",
                            "stitching_dispatch_error": message,
                        }
                    ),
                    batch_id,
                ),
            )


def _dispatch_stitching(
        result: Dict[str, Any],
        connection: Any,
) -> Dict[str, Any]:
    batch_id = result.get("batch_id")
    if not batch_id:
        raise RuntimeError("Completed ingestion result contains no batch_id")

    job = _ensure_stitching_job(connection, str(batch_id))
    if job["status"] in {
        "SUBMITTED",
        "WAITING_FOR_LOCK",
        "VALIDATING",
        "RUNNING",
        "SUCCEEDED",
        "LEGACY_UNKNOWN",
    }:
        return {
            "status": "already_submitted",
            "batch_id": batch_id,
            "job_id": job["job_id"],
            "task_arn": job["task_arn"],
        }

    job_id = str(job["job_id"])
    LOGGER.info(
        "ecs_run_task_started batch_id=%s job_id=%s", batch_id, job_id
    )
    try:
        task_arn = _start_stitching_task(str(batch_id), job_id)
    except Exception as exc:
        _mark_stitching_dispatch_failed(
            connection, str(batch_id), job_id, exc
        )
        raise
    # The task can move QUEUED -> WAITING/RUNNING before RunTask returns. The
    # recorder preserves that later state. If this database write fails, leave
    # the job retryable: the same ECS client token resolves to the same task.
    _record_stitching_dispatch(
        connection, str(batch_id), job_id, task_arn
    )
    LOGGER.info(
        "ecs_run_task_submitted batch_id=%s job_id=%s task_arn=%s",
        batch_id,
        job_id,
        task_arn,
    )
    result["stitching_task_arn"] = task_arn
    result["stitching_job_id"] = job_id
    return {
        "status": "submitted",
        "batch_id": batch_id,
        "job_id": job_id,
        "task_arn": task_arn,
    }


def _handle_ecs_task_state_change(
        event: Mapping[str, Any],
        connection: Any,
) -> Dict[str, Any]:
    from psycopg2.extras import Json

    detail = event.get("detail") or {}
    if detail.get("lastStatus") != "STOPPED":
        return {"status": "ignored", "reason": "task is not stopped"}

    task_arn = str(detail.get("taskArn") or "").strip()
    started_by = str(detail.get("startedBy") or "").strip()
    containers = detail.get("containers") or []
    exit_codes = [
        int(container["exitCode"])
        for container in containers
        if container.get("exitCode") is not None
    ]
    exit_code = next(
        (code for code in exit_codes if code != 0),
        exit_codes[0] if exit_codes else None,
    )
    failed_container = next(
        (
            container
            for container in containers
            if container.get("exitCode") is not None
            and int(container["exitCode"]) != 0
        ),
        containers[0] if containers else {},
    )
    container_reason = str(failed_container.get("reason") or "").strip() or None
    stop_code = str(detail.get("stopCode") or "").strip() or None
    stopped_reason = str(detail.get("stoppedReason") or "").strip() or None

    with connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT job_id::text, ingestion_batch_id::text, status
                FROM control.stitching_job
                WHERE (%s <> '' AND ecs_task_arn = %s)
                   OR (%s <> '' AND job_id::text = %s)
                ORDER BY CASE WHEN ecs_task_arn = %s THEN 0 ELSE 1 END
                LIMIT 1
                FOR UPDATE
                """,
                (task_arn, task_arn, started_by, started_by, task_arn),
            )
            job = cursor.fetchone()
            if job is None:
                LOGGER.warning(
                    "ecs_stopped_job_not_found task_arn=%s started_by=%s",
                    task_arn,
                    started_by,
                )
                return {"status": "ignored", "reason": "job not found"}

            job_id, batch_id, status = job
            if status in {"SUCCEEDED", "FAILED", "LEGACY_UNKNOWN"}:
                return {
                    "status": "unchanged",
                    "job_id": job_id,
                    "job_status": status,
                }

            message = container_reason or stopped_reason or "ECS task stopped"
            if exit_code == 0:
                message = (
                    "ECS task exited successfully without finalizing the "
                    "stitching job"
                )
            error_code = (
                "OutOfMemoryError"
                if container_reason and "OutOfMemoryError" in container_reason
                else stop_code or "ECS_TASK_STOPPED"
            )
            cursor.execute(
                """
                UPDATE control.stitching_job
                SET status         = 'FAILED',
                    ecs_task_arn   = COALESCE(ecs_task_arn, NULLIF(%s, '')),
                    completed_at   = COALESCE(completed_at, now()),
                    exit_code      = %s,
                    stop_code      = %s,
                    stopped_reason = %s,
                    error_code     = COALESCE(error_code, %s),
                    error_message  = COALESCE(error_message, %s),
                    updated_at     = now()
                WHERE job_id = %s
                  AND status NOT IN ('SUCCEEDED', 'LEGACY_UNKNOWN')
                """,
                (
                    task_arn,
                    exit_code,
                    stop_code,
                    stopped_reason,
                    error_code,
                    message[:2000],
                    job_id,
                ),
            )
            cursor.execute(
                """
                UPDATE identity.resolution_run
                SET status        = 'FAILED',
                    completed_at  = COALESCE(completed_at, now()),
                    error_message = COALESCE(error_message, %s)
                WHERE stitching_job_id = %s
                  AND status = 'RUNNING'
                """,
                (message[:8000], job_id),
            )
            cursor.execute(
                """
                UPDATE control.ingestion_batch
                SET metadata = COALESCE(metadata, '{}'::jsonb) || %s,
                    updated_at = now()
                WHERE batch_id = %s
                """,
                (
                    Json(
                        {
                            "stitching_job_id": job_id,
                            "stitching_task_arn": task_arn or None,
                            "stitching_dispatch_status": "failed",
                            "stitching_exit_code": exit_code,
                            "stitching_stop_code": stop_code,
                        }
                    ),
                    batch_id,
                ),
            )
            cursor.execute(
                """
                UPDATE quality.customer_validation_run
                SET run_status    = 'FAILED',
                    error_stage   = CASE
                                        WHEN %s = 'VALIDATING'
                                            THEN 'VALIDATION'
                                        ELSE 'FARGATE'
                                    END,
                    error_code    = COALESCE(error_code, %s),
                    error_message = COALESCE(error_message, %s),
                    completed_at  = COALESCE(completed_at, now()),
                    updated_at    = now()
                WHERE stitching_job_id = %s
                  AND run_status IN ('PENDING', 'RUNNING')
                """,
                (
                    status,
                    error_code,
                    message[:2000],
                    job_id,
                ),
            )
    return {"status": "failed", "job_id": job_id, "exit_code": exit_code}


def lambda_handler(event: Mapping[str, Any], context: Any) -> Dict[str, Any]:
    print(json.dumps(event))
    if event.get("source") == "aws.ecs" and event.get(
        "detail-type"
    ) == "ECS Task State Change":
        connection = _connect()
        try:
            return _handle_ecs_task_state_change(event, connection)
        finally:
            connection.close()

    records = event.get("Records", [])
    if not records:
        raise ValueError("No S3 Records in event")

    s3 = boto3.client("s3")
    connection = _connect()
    try:
        results = [_process_record(record, s3, connection) for record in records]
        dispatches: List[Dict[str, Any]] = []
        dispatched_by_batch: Dict[str, Dict[str, Any]] = {}
        for result in results:
            if result.get("status") not in {"completed", "skipped"}:
                continue
            batch_id = str(result.get("batch_id"))
            if batch_id in dispatched_by_batch:
                dispatch = dispatched_by_batch[batch_id]
                result["stitching_task_arn"] = dispatch.get("task_arn")
            else:
                dispatch = _dispatch_stitching(result, connection)
                dispatched_by_batch[batch_id] = dispatch
                dispatches.append(dispatch)
        return {"results": results, "stitching_tasks": dispatches}
    finally:
        connection.close()
