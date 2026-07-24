"""S3 CSV -> PostgreSQL customer ingestion -> ECS Fargate stitching task."""

from __future__ import annotations

import io
import json
import logging
import os
import uuid
from datetime import timezone
from pathlib import PurePosixPath
from typing import Any, Dict, List, Mapping, Tuple
from urllib.parse import unquote_plus

import boto3
import pandas as pd

LOGGER = logging.getLogger()
LOGGER.setLevel(logging.INFO)

REQUIRED_COLUMNS = [
    "customer_id",
    "first_name",
    "last_name",
    "email",
    "phone_number",
    "gender",
    "dob",
    "signup_date",
    "address",
    "city",
    "state",
    "country",
    "device_id(s)",
    "source",
]


def derive_source_code(key: str) -> str:
    """raw/pos/file.csv -> pos."""
    parts = PurePosixPath(key).parts
    if len(parts) < 3 or parts[0] != "raw":
        raise ValueError(f"Expected S3 key raw/<source_code>/<file>.csv, got {key}")
    return parts[1].strip().lower()


def _optional(value: Any) -> Any:
    value = str(value).strip()
    return value or None


def prepare_customers(
        csv_bytes: bytes,
) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """Read, validate, and deduplicate the CSV with pandas."""
    df = pd.read_csv(
        io.BytesIO(csv_bytes),
        dtype=str,
        keep_default_na=False,
        encoding="utf-8-sig",
    )
    missing = [column for column in REQUIRED_COLUMNS if column not in df.columns]
    if missing:
        raise ValueError(f"Missing CSV columns: {', '.join(missing)}")

    df = df[REQUIRED_COLUMNS].copy()
    raw_payloads = df.to_dict("records")
    for column in REQUIRED_COLUMNS:
        df[column] = df[column].str.strip()

    dob = pd.to_datetime(df["dob"], format="%Y-%m-%d", errors="coerce")
    signup = pd.to_datetime(
        df["signup_date"], format="%Y-%m-%d", errors="coerce"
    )
    valid = (
            df["customer_id"].ne("")
            & (df["dob"].eq("") | dob.notna())
            & (df["signup_date"].eq("") | signup.notna())
    )
    received = len(df)
    rejected = int((~valid).sum())
    df = df.loc[valid].copy()
    df["_dob"] = dob.loc[valid]
    df["_signup"] = signup.loc[valid]
    df["_raw_payload"] = [raw_payloads[index] for index in df.index]

    valid_before_dedup = len(df)
    df = df.drop_duplicates(subset=["customer_id"], keep="last")
    duplicates = valid_before_dedup - len(df)

    customers: List[Dict[str, Any]] = []
    for row in df.to_dict("records"):
        first_name = _optional(row["first_name"])
        last_name = _optional(row["last_name"])
        full_name = " ".join(
            part for part in (first_name, last_name) if part
        ) or None
        device_ids = list(
            dict.fromkeys(
                item.strip()
                for item in row["device_id(s)"].split(";")
                if item.strip()
            )
        )
        source_created_at = None
        if pd.notna(row["_signup"]):
            source_created_at = row["_signup"].to_pydatetime().replace(
                tzinfo=timezone.utc
            )

        customers.append(
            {
                "source_customer_id": row["customer_id"],
                "first_name": first_name,
                "last_name": last_name,
                "full_name": full_name,
                "email": _optional(row["email"].lower()),
                "phone": _optional(row["phone_number"]),
                "address_line_1": _optional(row["address"]),
                "district": _optional(row["city"]),
                "province": _optional(row["state"]),
                "country": _optional(row["country"]),
                "date_of_birth": (
                    row["_dob"].date() if pd.notna(row["_dob"]) else None
                ),
                "gender": _optional(row["gender"]),
                "external_ids": {"device_ids": device_ids},
                "attributes": {
                    "acquisition_source": _optional(row["source"])
                },
                "raw_payload": row["_raw_payload"],
                "source_created_at": source_created_at,
            }
        )

    return customers, {
        "records_received": received,
        "records_rejected": rejected,
        "duplicate_rows": duplicates,
    }


def _connect() -> Any:
    import psycopg2

    client = boto3.client("secretsmanager", region_name="ap-southeast-7")
    response = client.get_secret_value(SecretId="marketing-automation-secret")
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

    rows = [
        (
            str(uuid.uuid4()),
            batch_id,
            source_system_id,
            customer["source_customer_id"],
            customer["first_name"],
            customer["last_name"],
            customer["full_name"],
            customer["email"],
            customer["phone"],
            customer["address_line_1"],
            customer["district"],
            customer["province"],
            customer["country"],
            customer["date_of_birth"],
            customer["gender"],
            Json(customer["external_ids"]),
            Json(customer["attributes"]),
            Json(customer["raw_payload"]),
            customer["source_created_at"],
        )
        for customer in customers
    ]
    if not rows:
        return

    execute_values(
        cursor,
        """
        INSERT INTO raw.customer (raw_customer_id, batch_id, source_system_id, source_customer_id,
                                  first_name, last_name, full_name, email, phone, address_line_1,
                                  district, province, country, date_of_birth, gender, external_ids,
                                  attributes, raw_payload, source_created_at, ingested_at)
        VALUES %s
        ON CONFLICT (source_system_id, source_customer_id)
        DO UPDATE SET
        batch_id = EXCLUDED.batch_id,
        first_name = EXCLUDED.first_name,
        last_name = EXCLUDED.last_name,
        full_name = EXCLUDED.full_name,
        email = EXCLUDED.email,
        phone = EXCLUDED.phone,
        address_line_1 = EXCLUDED.address_line_1,
        district = EXCLUDED.district,
        province = EXCLUDED.province,
        country = EXCLUDED.country,
        date_of_birth = EXCLUDED.date_of_birth,
        gender = EXCLUDED.gender,
        external_ids = EXCLUDED.external_ids,
        attributes = EXCLUDED.attributes,
        raw_payload = EXCLUDED.raw_payload,
        source_created_at = EXCLUDED.source_created_at,
        ingested_at = now()
        """,
        rows,
        template="(" + ",".join(["%s"] * 19) + ",now())",
        page_size=int(os.getenv("UPSERT_PAGE_SIZE", "1000")),
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

    response = s3.get_object(Bucket=bucket, Key=key, IfMatch=etag)
    customers, stats = prepare_customers(response["Body"].read())
    batch_id = str(uuid.uuid4())

    # The context commits before this function returns. ECS is started later.
    with connection:
        with connection.cursor() as cursor:
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

            cursor.execute(
                """
                INSERT INTO control.ingestion_batch (batch_id, source_system_id, object_name, file_name, status,
                                                     checksum, metadata)
                VALUES (%s, %s, %s, %s, 'processing', %s, %s)
                ON CONFLICT (source_system_id, object_name, checksum)
                    DO NOTHING
                RETURNING batch_id::text
                """,
                (
                    batch_id,
                    source_system_id,
                    key,
                    PurePosixPath(key).name,
                    etag,
                    Json({"bucket": bucket, "event_time": record.get("eventTime")}),
                ),
            )
            inserted = cursor.fetchone()
            if inserted is None:
                cursor.execute(
                    """
                    SELECT batch_id::text,
                           status,
                           metadata ->> 'stitching_task_arn'
                    FROM control.ingestion_batch
                    WHERE source_system_id = %s
                      AND object_name = %s
                      AND checksum = %s
                    """,
                    (source_system_id, key, etag),
                )
                existing = cursor.fetchone()
                if existing is None:
                    raise RuntimeError("Existing ingestion batch could not be resolved")
                existing_batch_id, existing_status, task_arn = existing
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
            _upsert_customers(cursor, customers, batch_id, source_system_id)
            metadata = {
                "bucket": bucket,
                "event_time": record.get("eventTime"),
                "duplicate_rows": stats["duplicate_rows"],
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

    return {
        "status": "completed",
        "batch_id": batch_id,
        "source_code": source_code,
        "records_received": stats["records_received"],
        "records_loaded": len(customers),
        "records_rejected": stats["records_rejected"],
        "duplicate_rows": stats["duplicate_rows"],
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


def _start_stitching_task(batch_id: str) -> str:
    assign_public_ip = os.getenv("ECS_ASSIGN_PUBLIC_IP", "DISABLED").upper()
    if assign_public_ip not in {"ENABLED", "DISABLED"}:
        raise ValueError("ECS_ASSIGN_PUBLIC_IP must be ENABLED or DISABLED")

    ecs = boto3.client("ecs")
    response = ecs.run_task(
        cluster=_required_environment("ECS_CLUSTER"),
        taskDefinition=_required_environment("ECS_TASK_DEFINITION"),
        launchType="FARGATE",
        platformVersion=os.getenv("ECS_PLATFORM_VERSION", "LATEST"),
        count=1,
        clientToken=f"stitch-{batch_id}",
        startedBy=batch_id,
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


def _record_stitching_dispatch(
        connection: Any,
        batch_id: str,
        task_arn: str,
) -> None:
    from psycopg2.extras import Json

    with connection:
        with connection.cursor() as cursor:
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


def _dispatch_stitching(
        result: Dict[str, Any],
        connection: Any,
) -> Dict[str, Any]:
    batch_id = result.get("batch_id")
    if not batch_id:
        raise RuntimeError("Completed ingestion result contains no batch_id")

    existing_task_arn = result.get("stitching_task_arn")
    if existing_task_arn:
        return {
            "status": "already_submitted",
            "batch_id": batch_id,
            "task_arn": existing_task_arn,
        }

    LOGGER.info("ecs_run_task_started batch_id=%s", batch_id)
    task_arn = _start_stitching_task(batch_id)
    _record_stitching_dispatch(connection, batch_id, task_arn)
    LOGGER.info(
        "ecs_run_task_submitted batch_id=%s task_arn=%s",
        batch_id,
        task_arn,
    )
    result["stitching_task_arn"] = task_arn
    return {
        "status": "submitted",
        "batch_id": batch_id,
        "task_arn": task_arn,
    }


def lambda_handler(event: Mapping[str, Any], context: Any) -> Dict[str, Any]:
    print(json.dumps(event))
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
