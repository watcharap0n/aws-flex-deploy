"""Standardize staging customers and publish raw and profile read models."""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime
from urllib import request

import boto3
import psycopg2
from psycopg2.extras import RealDictCursor, execute_values

from identity_stitcher.config import AppConfig, DatabaseConfig
from identity_stitcher.standardize import _standardize_customers


logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
LOG = logging.getLogger("raw-clickhouse-mirror")


def load_secrets() -> tuple[dict, dict]:
    client = boto3.client("secretsmanager", region_name=os.environ["AWS_REGION"])
    db = json.loads(client.get_secret_value(SecretId=os.environ["DB_SECRET_ID"])["SecretString"])
    ch = json.loads(client.get_secret_value(SecretId=os.environ["CLICKHOUSE_SECRET_ID"])["SecretString"])
    if db.get("dbname") != "staging" or ch.get("database") != "staging":
        raise RuntimeError("staging database secrets required")
    return db, ch


def clickhouse(sql: str, ch: dict, *, body: bytes | None = None) -> str:
    headers = {
        "X-ClickHouse-User": ch["username"],
        "X-ClickHouse-Key": ch["password"],
        "Content-Type": "text/plain; charset=utf-8",
    }
    if body is None:
        payload = sql.encode()
        url = "http://127.0.0.1:8123/"
    else:
        from urllib.parse import quote
        payload = body
        url = "http://127.0.0.1:8123/?query=" + quote(sql)
    req = request.Request(url, data=payload, headers=headers, method="POST")
    with request.urlopen(req, timeout=30) as response:
        return response.read().decode().strip()


def timestamp(value: datetime) -> str:
    return value.strftime("%Y-%m-%d %H:%M:%S.%f")


def standardize_profiles_once(db: dict, batch_size: int) -> int:
    """Run the existing normalizer for completed batches needing typed profiles."""
    conn = psycopg2.connect(
        host="127.0.0.1", port=5432, dbname=db["dbname"],
        user=db["username"], password=db["password"], sslmode="disable",
        connect_timeout=10, application_name="staging-customer-standardizer",
    )
    config = AppConfig(database=DatabaseConfig.from_mapping(db, sslmode="disable"))
    try:
        with conn.cursor() as cursor:
            # A current winner must have exact immutable raw and membership
            # evidence before it is normalized into an app-scoped profile.
            cursor.execute("""
                SELECT EXISTS (
                    SELECT 1 FROM raw.customer_current AS c
                    JOIN control.ingestion_batch AS b ON b.batch_id = c.batch_id
                    WHERE b.status = 'completed' AND (
                        b.app_id IS NULL OR b.source_system_id IS DISTINCT FROM c.source_system_id
                        OR NOT EXISTS (
                            SELECT 1 FROM raw.customer AS h
                            WHERE (h.batch_id,h.raw_customer_id,h.source_system_id,h.source_customer_id,h.app_id)
                                = (c.batch_id,c.raw_customer_id,c.source_system_id,c.source_customer_id,b.app_id)
                        ) OR NOT EXISTS (
                            SELECT 1 FROM raw.customer_batch_member AS m
                            WHERE (m.ingestion_batch_id,m.raw_customer_id,m.source_system_id,m.source_customer_id)
                                = (c.batch_id,c.raw_customer_id,c.source_system_id,c.source_customer_id)
                        )
                    )
                )
            """)
            if cursor.fetchone()[0]:
                raise RuntimeError("staging current winner lacks exact app-scoped raw evidence")
            cursor.execute("""
                SELECT EXISTS (
                    SELECT 1 FROM raw.customer AS h
                    JOIN control.ingestion_batch AS b ON b.batch_id = h.batch_id
                    WHERE b.status = 'completed' AND (
                        b.app_id IS NULL
                        OR (b.app_id,b.source_system_id)
                           IS DISTINCT FROM (h.app_id,h.source_system_id)
                        OR NOT EXISTS (
                            SELECT 1 FROM raw.customer_batch_member AS m
                            WHERE (m.ingestion_batch_id,m.raw_customer_id,m.source_system_id,m.source_customer_id)
                                = (h.batch_id,h.raw_customer_id,h.source_system_id,h.source_customer_id)
                        )
                    )
                )
            """)
            if cursor.fetchone()[0]:
                raise RuntimeError("staging raw observation lacks exact batch lineage")
            cursor.execute("""
                SELECT batch_id::text FROM (
                    SELECT h.batch_id FROM raw.customer AS h
                    JOIN control.ingestion_batch AS b ON b.batch_id = h.batch_id
                    WHERE b.status = 'completed'
                      AND NOT EXISTS (
                          SELECT 1 FROM staging.customer_profile_history AS p
                          WHERE (p.batch_id,p.raw_customer_id,p.app_id,p.source_system_id,p.source_customer_id)
                              = (h.batch_id,h.raw_customer_id,h.app_id,h.source_system_id,h.source_customer_id)
                      )
                    UNION
                    SELECT c.batch_id FROM raw.customer_current AS c
                    JOIN control.ingestion_batch AS b ON b.batch_id = c.batch_id
                    WHERE b.status = 'completed'
                      AND NOT EXISTS (
                          SELECT 1 FROM staging.customer_profile AS p
                          WHERE (p.batch_id,p.raw_customer_id,p.app_id,p.source_system_id,p.source_customer_id)
                              = (c.batch_id,c.raw_customer_id,b.app_id,c.source_system_id,c.source_customer_id)
                      )
                ) AS pending
                ORDER BY batch_id LIMIT %s
            """, (min(batch_size, 20),))
            batch_ids = [row[0] for row in cursor.fetchall()]
        count = 0
        for batch_id in batch_ids:
            _standardize_customers(conn, config, [batch_id], batch_size, history=True)
            count += _standardize_customers(
                conn, config, [batch_id], batch_size, app_scoped=True
            )
        conn.commit()
        return count
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def sync_once(db: dict, ch: dict, batch_size: int) -> int:
    conn = psycopg2.connect(
        host="127.0.0.1", port=5432, dbname=db["dbname"],
        user=db["username"], password=db["password"], sslmode="disable",
        connect_timeout=10, application_name="staging-raw-clickhouse-mirror",
    )
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute("SELECT to_regclass('control.clickhouse_raw_mirror')")
            if cursor.fetchone()["to_regclass"] is None:
                raise RuntimeError("staging delivery ledger DDL is missing")
            cursor.execute(
                """SELECT r.snapshot_id::text, r.batch_id::text,
                          r.raw_customer_id::text, r.source_system_id,
                          r.source_customer_id, r.app_id, r.status,
                          coalesce(r.raw_payload::text, '{}') AS raw_payload,
                          r.ingested_at AT TIME ZONE 'UTC' AS ingested_at,
                          r.snapshot_recorded_at AT TIME ZONE 'UTC' AS snapshot_recorded_at
                   FROM raw.customer AS r
                   LEFT JOIN control.clickhouse_raw_mirror AS delivered
                     ON delivered.snapshot_id = r.snapshot_id
                   WHERE delivered.snapshot_id IS NULL
                   ORDER BY r.ingested_at, r.snapshot_id
                   LIMIT %s""",
                (batch_size,),
            )
            rows = [dict(row) for row in cursor.fetchall()]
            if not rows:
                conn.rollback()
                return 0
            payload = b"".join(
                (json.dumps({**row,
                    "ingested_at": timestamp(row["ingested_at"]),
                    "snapshot_recorded_at": timestamp(row["snapshot_recorded_at"]),
                }, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
                for row in rows
            )
            clickhouse("INSERT INTO staging.raw_customer FORMAT JSONEachRow", ch, body=payload)
            ids = ",".join("'" + row["snapshot_id"] + "'" for row in rows)
            visible = int(clickhouse(
                "SELECT countDistinct(snapshot_id) FROM staging.raw_customer "
                f"WHERE snapshot_id IN ({ids}) FORMAT TabSeparated", ch
            ))
            if visible != len(rows):
                raise RuntimeError("ClickHouse readback did not match inserted snapshot IDs")
            execute_values(cursor,
                "INSERT INTO control.clickhouse_raw_mirror(snapshot_id) VALUES %s "
                "ON CONFLICT (snapshot_id) DO NOTHING",
                [(row["snapshot_id"],) for row in rows],
            )
        conn.commit()
        return len(rows)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def profile_payload(row: dict) -> dict:
    app_id = row["app_id"]
    tenant_key = row["tenant_key"]
    customer_id = row["customer_id"]
    if not app_id or not tenant_key or not customer_id:
        raise RuntimeError("standardized profile has incomplete source scope")
    standardized_at = row["standardized_at"]
    fingerprint = row["record_fingerprint"]
    if not fingerprint or len(fingerprint) != 64:
        raise RuntimeError("standardized profile has invalid fingerprint")
    dob = row["date_of_birth"]
    dob_text = dob.isoformat() if dob else None
    typed_dob = dob_text if dob and 1900 <= dob.year <= 2299 else None
    status = row["standardization_status"]
    return {
        "standardized_record_id": row["standardized_record_id"],
        "app_id": app_id,
        "source_system_id": row["source_system_id"],
        "tenant_key": tenant_key,
        "customer_id": customer_id,
        "customer_key_id": None,
        "profile_source_record_id": None,
        "external_id": row["external_id"],
        "first_name": row["first_name"],
        "middle_name": row["middle_name"],
        "last_name": row["last_name"],
        "full_name": row["full_name"],
        "nickname": row["nickname"],
        "email": row["email"],
        "phone": row["phone"],
        "gender": row["gender"],
        "date_of_birth_raw": dob_text,
        "date_of_birth": typed_dob,
        "member_code": row["member_code"],
        "customer_type": row["customer_type"],
        "status": row["status"],
        "language": row["language"],
        "address_line_1": row["address_line_1"],
        "address_line_2": row["address_line_2"],
        "district": row["district"],
        "province": row["province"],
        "postal_code": row["postal_code"],
        "country_code": row["country_code"].strip() if row["country_code"] else None,
        "external_ids": row["external_ids"],
        "attributes": row["attributes"],
        "source_created_at": timestamp(row["source_created_at"]) if row["source_created_at"] else None,
        "source_updated_at": timestamp(row["source_updated_at"]) if row["source_updated_at"] else None,
        "profile_updated_at": timestamp(row["source_updated_at"] or standardized_at),
        "record_fingerprint": fingerprint,
        "identity_eligible": bool(row["email_is_valid"] or row["phone_is_valid"]),
        "quality_flags": [] if status == "READY" else [status or "UNKNOWN"],
        "standardized_at": timestamp(standardized_at),
        "first_seen_at": timestamp(row["source_created_at"] or standardized_at),
        "last_seen_at": timestamp(row["source_updated_at"] or standardized_at),
        "customer_entity_id": None,
        "resolution_run_id": None,
        "workspace_id": None,
        "transfer_id": None,
        "publication_seq": None,
    }


def sync_profiles_once(db: dict, ch: dict, batch_size: int) -> int:
    conn = psycopg2.connect(
        host="127.0.0.1", port=5432, dbname=db["dbname"],
        user=db["username"], password=db["password"], sslmode="disable",
        connect_timeout=10, application_name="staging-customer-profile-mirror",
    )
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute("SELECT to_regclass('control.clickhouse_customer_profile_mirror')")
            if cursor.fetchone()["to_regclass"] is None:
                raise RuntimeError("staging profile delivery ledger DDL is missing")
            cursor.execute(
                """SELECT p.standardized_record_id::text, p.app_id,
                          p.source_system_id, p.source_customer_id AS customer_id,
                          p.record_fingerprint, p.standardization_status,
                          p.standardized_at AT TIME ZONE 'UTC' AS standardized_at,
                          p.source_created_at AT TIME ZONE 'UTC' AS source_created_at,
                          p.source_updated_at AT TIME ZONE 'UTC' AS source_updated_at,
                          p.date_of_birth, p.email_is_valid, p.phone_is_valid,
                          p.first_name_normalized AS first_name,
                          p.middle_name_normalized AS middle_name,
                          p.last_name_normalized AS last_name,
                          p.full_name_normalized AS full_name,
                          p.email_normalized AS email,
                          p.phone_normalized AS phone,
                          p.gender_normalized AS gender,
                          p.address_normalized AS address_line_1,
                          p.district_normalized AS district,
                          p.province_normalized AS province,
                          p.postal_code_normalized AS postal_code,
                          p.country_code,
                          coalesce(p.attributes::text, '{}') AS attributes,
                          s.source_code AS tenant_key,
                          r.external_id, r.nickname, r.member_code,
                          r.customer_type, r.status, r.language, r.address_line_2,
                          coalesce(r.external_ids::text, '{}') AS external_ids
                   FROM staging.customer_profile AS p
                   JOIN raw.customer_current AS r
                     ON r.raw_customer_id = p.raw_customer_id
                    AND r.source_system_id = p.source_system_id
                    AND r.source_customer_id = p.source_customer_id
                   JOIN control.source_system AS s
                     ON s.source_system_id = p.source_system_id
                   LEFT JOIN control.clickhouse_customer_profile_mirror AS delivered
                     ON delivered.standardized_record_id = p.standardized_record_id
                   WHERE delivered.standardized_record_id IS NULL
                      OR delivered.record_fingerprint IS DISTINCT FROM p.record_fingerprint
                   ORDER BY p.standardized_at, p.standardized_record_id
                   LIMIT %s""",
                (batch_size,),
            )
            rows = [dict(row) for row in cursor.fetchall()]
            if not rows:
                conn.rollback()
                return 0
            profiles = [profile_payload(row) for row in rows]
            payload = b"".join(
                (json.dumps(profile, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
                for profile in profiles
            )
            clickhouse("INSERT INTO cdp_customer.customer_profile_stage FORMAT JSONEachRow", ch, body=payload)
            ids = ",".join("'" + row["standardized_record_id"] + "'" for row in rows)
            readback = clickhouse(
                "SELECT toString(standardized_record_id) AS id, "
                "toString(record_fingerprint) AS fingerprint "
                "FROM cdp_customer.customer_profile_stage FINAL "
                f"WHERE standardized_record_id IN ({ids}) FORMAT JSONEachRow", ch
            )
            delivered = {
                item["id"]: item["fingerprint"]
                for item in (json.loads(line) for line in readback.splitlines())
            }
            if delivered != {
                row["standardized_record_id"]: profile["record_fingerprint"]
                for row, profile in zip(rows, profiles)
            }:
                raise RuntimeError("ClickHouse profile readback fingerprint mismatch")
            execute_values(cursor,
                "INSERT INTO control.clickhouse_customer_profile_mirror"
                "(standardized_record_id,record_fingerprint) VALUES %s "
                "ON CONFLICT (standardized_record_id) DO UPDATE SET "
                "record_fingerprint=EXCLUDED.record_fingerprint,published_at=now()",
                [(row["standardized_record_id"], profile["record_fingerprint"])
                 for row, profile in zip(rows, profiles)],
            )
        conn.commit()
        return len(rows)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def main() -> None:
    interval = int(os.getenv("SYNC_INTERVAL_SECONDS", "60"))
    batch_size = int(os.getenv("SYNC_BATCH_SIZE", "500"))
    if interval < 5 or not 1 <= batch_size <= 1000:
        raise ValueError("invalid staging mirror settings")
    while True:
        try:
            db, ch = load_secrets()
            count = sync_once(db, ch, batch_size)
            if count:
                LOG.info("published_snapshot_count=%d", count)
            standardized = standardize_profiles_once(db, batch_size)
            if standardized:
                LOG.info("standardized_profile_count=%d", standardized)
            profiles = sync_profiles_once(db, ch, batch_size)
            if profiles:
                LOG.info("published_profile_count=%d", profiles)
        except Exception as exc:
            LOG.error("staging raw mirror failed: %s", type(exc).__name__)
        time.sleep(interval)


if __name__ == "__main__":
    main()
