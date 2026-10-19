"""Frozen canonical CSV reconstruction, not proof of original transformation."""
from __future__ import annotations
import csv
import io
import json
from datetime import date, datetime, timezone
from typing import Any, Dict, List, Tuple

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
