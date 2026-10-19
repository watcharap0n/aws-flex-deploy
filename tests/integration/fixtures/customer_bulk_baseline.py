"""Pinned pre-bulk Lambda baseline for comparative benchmarks only.
Source function SHA256: bfed69d8cdf159b1d913a5b46653ab021f9d9b2c1aab3a9d382ae070ec37b49f
"""
from typing import Any, List, Dict
import os, uuid, json, logging
LOGGER=logging.getLogger(__name__)

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
        INSERT INTO raw.customer_current (raw_customer_id, batch_id, source_system_id, source_customer_id,
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
