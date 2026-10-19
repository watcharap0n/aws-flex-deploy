from __future__ import annotations

from decimal import Decimal
from typing import Any, Sequence

from psycopg2.extras import Json, RealDictCursor, execute_values

from .config import AppConfig
from .normalization import (
    clean_text,
    national_id_hash,
    normalize_country,
    normalize_date,
    normalize_email,
    normalize_gender,
    normalize_phone,
    normalize_text,
    normalized_devices,
    stable_fingerprint,
)


RAW_COLUMNS = (
    "raw_customer_id", "batch_id", "source_system_id", "source_customer_id",
    "first_name", "middle_name", "last_name", "full_name", "email", "phone",
    "address_line_1", "address_line_2", "district", "province", "postal_code", "country",
    "date_of_birth", "gender", "external_ids", "attributes", "source_created_at",
    "source_updated_at", "ingested_at",
)

STAGING_COLUMNS = (
    "raw_customer_id", "batch_id", "source_system_id", "source_customer_id",
    "first_name_raw", "first_name_normalized", "middle_name_raw", "middle_name_normalized",
    "last_name_raw", "last_name_normalized", "full_name_raw", "full_name_normalized",
    "email_raw", "email_normalized", "email_domain", "email_is_valid",
    "phone_raw", "phone_normalized", "phone_country_code", "phone_is_valid",
    "address_raw", "address_normalized", "district_normalized", "province_normalized",
    "postal_code_normalized", "country_code", "date_of_birth", "gender_normalized",
    "national_id_hash", "device_ids_normalized", "record_quality_score",
    "standardization_status", "attributes", "source_created_at", "source_updated_at",
    "record_fingerprint", "normalization_version",
)


def _quality_score(values: dict[str, Any]) -> Decimal:
    checks = (
        (values["full_name_normalized"], 0.20),
        (values["email_normalized"], 0.15),
        (values["email_is_valid"], 0.10),
        (values["phone_normalized"], 0.15),
        (values["phone_is_valid"], 0.10),
        (values["date_of_birth"], 0.10),
        (values["address_normalized"], 0.05),
        (values["country_code"], 0.05),
        (values["national_id_hash"] or values["device_ids_normalized"], 0.10),
    )
    return Decimal(str(round(sum(weight for present, weight in checks if present), 4)))


def standardize_record(row: dict[str, Any], config: AppConfig) -> dict[str, Any]:
    first_raw = clean_text(row.get("first_name"))
    middle_raw = clean_text(row.get("middle_name"))
    last_raw = clean_text(row.get("last_name"))
    full_raw = clean_text(row.get("full_name"))
    if full_raw is None:
        full_raw = " ".join(part for part in (first_raw, middle_raw, last_raw) if part) or None

    email_raw = clean_text(row.get("email"))
    email, email_domain, email_valid = normalize_email(email_raw)
    phone_raw = clean_text(row.get("phone"))
    country_code = normalize_country(row.get("country"))
    phone, phone_country_code, phone_valid = normalize_phone(
        phone_raw,
        country_code or config.default_phone_region,
    )
    address_raw = " ".join(
        part for part in (clean_text(row.get("address_line_1")), clean_text(row.get("address_line_2"))) if part
    ) or None
    devices = normalized_devices(row.get("external_ids"))

    values: dict[str, Any] = {
        "raw_customer_id": row["raw_customer_id"],
        "batch_id": row["batch_id"],
        "source_system_id": row["source_system_id"],
        "source_customer_id": row["source_customer_id"],
        "first_name_raw": first_raw,
        "first_name_normalized": normalize_text(first_raw),
        "middle_name_raw": middle_raw,
        "middle_name_normalized": normalize_text(middle_raw),
        "last_name_raw": last_raw,
        "last_name_normalized": normalize_text(last_raw),
        "full_name_raw": full_raw,
        "full_name_normalized": normalize_text(full_raw),
        "email_raw": email_raw,
        "email_normalized": email,
        "email_domain": email_domain,
        "email_is_valid": email_valid,
        "phone_raw": phone_raw,
        "phone_normalized": phone,
        "phone_country_code": phone_country_code,
        "phone_is_valid": phone_valid,
        "address_raw": address_raw,
        "address_normalized": normalize_text(address_raw),
        "district_normalized": normalize_text(row.get("district")),
        "province_normalized": normalize_text(row.get("province")),
        "postal_code_normalized": normalize_text(row.get("postal_code")),
        "country_code": country_code,
        "date_of_birth": normalize_date(row.get("date_of_birth")),
        "gender_normalized": normalize_gender(row.get("gender")),
        "national_id_hash": national_id_hash(row.get("external_ids"), config.pii_hash_key),
        "device_ids_normalized": devices,
        "attributes": row.get("attributes") or {},
        "source_created_at": row.get("source_created_at"),
        "source_updated_at": row.get("source_updated_at"),
        "normalization_version": config.normalization_version,
    }
    identifiers = (
        values["full_name_normalized"],
        values["email_normalized"] if values["email_is_valid"] else None,
        values["phone_normalized"] if values["phone_is_valid"] else None,
        values["national_id_hash"], values["device_ids_normalized"],
    )
    values["standardization_status"] = "READY" if any(identifiers) else "INSUFFICIENT_IDENTIFIERS"
    values["record_quality_score"] = _quality_score(values)
    values["record_fingerprint"] = stable_fingerprint(values.get(column) for column in STAGING_COLUMNS[:-3])
    return values


def standardize_customers(conn, config: AppConfig, batch_ids: Sequence[str] | None = None, chunk_size: int = 2_000) -> int:
    # The 019/022 raw UUID projection cannot represent two independent app
    # origins. Reject proven unrepresented app identities before writing; the
    # 050-cutover schema uses the literal app/source tuple instead.
    from .linkage import _require_complete_logical_sources
    with conn.cursor() as cursor:
        cursor.execute("SELECT to_regclass('identity.customer_entity_member') IS NOT NULL AND EXISTS (SELECT FROM pg_attribute WHERE attrelid='identity.customer_entity_member'::regclass AND attname='app_id' AND NOT attisdropped)")
        app_scoped = bool(cursor.fetchone()[0])
    # A pending migration cannot safely resolve an independently normalized
    # alias through the one-row raw UUID projection.
    if not app_scoped:
        _require_complete_logical_sources(conn)
    # A legacy selected batch that is not the current app must still validate
    # its immutable history without replacing another app's current profile.
    if not app_scoped and batch_ids:
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT EXISTS (
                    SELECT FROM raw.customer_current c
                    JOIN raw.customer_batch_member m
                      ON m.raw_customer_id=c.raw_customer_id
                     AND m.source_system_id=c.source_system_id
                     AND m.source_customer_id=c.source_customer_id
                    JOIN control.ingestion_batch selected ON selected.batch_id=m.ingestion_batch_id
                    JOIN control.ingestion_batch current ON current.batch_id=c.batch_id
                    WHERE m.ingestion_batch_id=ANY(%s::uuid[])
                      AND selected.app_id=current.app_id
                      AND NOT EXISTS (
                          SELECT FROM raw.customer_batch_member other
                          JOIN control.ingestion_batch origin
                            ON origin.batch_id=other.ingestion_batch_id
                          WHERE other.raw_customer_id=c.raw_customer_id
                            AND origin.app_id IS DISTINCT FROM current.app_id
                      )
                )
            """, (list(batch_ids),))
            eligible_current = cursor.fetchone()[0]
        if not eligible_current:
            with conn.cursor() as cursor:
                cursor.execute("""
                    SELECT EXISTS (
                        SELECT FROM raw.customer_batch_member m
                        JOIN raw.customer_batch_member other
                          ON other.raw_customer_id=m.raw_customer_id
                        JOIN control.ingestion_batch selected
                          ON selected.batch_id=m.ingestion_batch_id
                        JOIN control.ingestion_batch origin
                          ON origin.batch_id=other.ingestion_batch_id
                        JOIN staging.customer_profile p
                          ON p.raw_customer_id=m.raw_customer_id
                        WHERE m.ingestion_batch_id=ANY(%s::uuid[])
                          AND selected.app_id IS DISTINCT FROM origin.app_id
                    )
                """, (list(batch_ids),))
                represented_alias = cursor.fetchone()[0]
            if represented_alias:
                _require_complete_logical_sources(conn)
            try:
                for batch_id in batch_ids:
                    _standardize_customers(conn, config, [batch_id], chunk_size, history=True)
                conn.commit()
                return 0
            except Exception:
                conn.rollback()
                raise
    if app_scoped and not batch_ids:
        with conn.cursor() as cursor:
            cursor.execute("SELECT DISTINCT batch_id::text FROM raw.customer ORDER BY batch_id::text")
            batch_ids = [row[0] for row in cursor.fetchall()]
    if app_scoped and batch_ids:
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT EXISTS (
                    SELECT FROM raw.customer_current c
                    JOIN control.ingestion_batch b ON b.batch_id=c.batch_id
                    JOIN raw.customer_batch_member m ON
                      (m.ingestion_batch_id,m.raw_customer_id,m.source_system_id,m.source_customer_id)=
                      (c.batch_id,c.raw_customer_id,c.source_system_id,c.source_customer_id)
                    LEFT JOIN raw.customer h ON
                      (h.batch_id,h.raw_customer_id,h.source_system_id,h.source_customer_id)=
                      (c.batch_id,c.raw_customer_id,c.source_system_id,c.source_customer_id)
                    WHERE m.ingestion_batch_id=ANY(%s::uuid[])
                      AND (h.batch_id IS NULL OR b.app_id IS NULL)
                )
            """, (list(batch_ids),))
            if cursor.fetchone()[0]:
                raise RuntimeError('LOGICAL_SOURCE_PROJECTION_REQUIRED: current winner lacks exact raw observation')
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT EXISTS (
                    SELECT FROM raw.customer h
                    JOIN control.ingestion_batch b ON b.batch_id=h.batch_id
                    LEFT JOIN raw.customer_batch_member m
                      ON m.ingestion_batch_id=h.batch_id
                     AND m.raw_customer_id=h.raw_customer_id
                     AND m.source_system_id=h.source_system_id
                     AND m.source_customer_id=h.source_customer_id
                    WHERE h.batch_id=ANY(%s::uuid[])
                      AND (m.ingestion_batch_id IS NULL
                           OR b.app_id IS DISTINCT FROM h.app_id
                           OR b.source_system_id IS DISTINCT FROM h.source_system_id)
                ) OR EXISTS (
                    SELECT FROM raw.customer_batch_member m
                    JOIN control.ingestion_batch b ON b.batch_id=m.ingestion_batch_id
                    LEFT JOIN raw.customer h
                      ON h.batch_id=m.ingestion_batch_id
                     AND h.raw_customer_id=m.raw_customer_id
                     AND h.source_system_id=m.source_system_id
                     AND h.source_customer_id=m.source_customer_id
                    WHERE m.ingestion_batch_id=ANY(%s::uuid[])
                      AND (h.batch_id IS NULL OR b.app_id IS NULL)
                )
            """, (list(batch_ids), list(batch_ids)))
            if cursor.fetchone()[0]:
                raise RuntimeError('LOGICAL_SOURCE_PROJECTION_REQUIRED: selected raw history lacks exact batch membership')
    # Every selected source must already have a profile, or be a genuinely
    # new raw current winner. A shared raw UUID cannot invent another owner.
    if app_scoped and batch_ids:
        with conn.cursor() as cursor:
            cursor.execute("""
                SELECT EXISTS (
                    SELECT FROM raw.customer_batch_member m
                    JOIN control.ingestion_batch b ON b.batch_id=m.ingestion_batch_id
                    LEFT JOIN staging.customer_profile p
                      ON (p.app_id,p.source_system_id,p.source_customer_id)=
                         (b.app_id,m.source_system_id,m.source_customer_id)
                    LEFT JOIN raw.customer_current c
                      ON (c.raw_customer_id,c.batch_id,c.source_system_id,c.source_customer_id)=
                         (m.raw_customer_id,m.ingestion_batch_id,m.source_system_id,m.source_customer_id)
                    WHERE m.ingestion_batch_id=ANY(%s::uuid[])
                      AND p.standardized_record_id IS NULL
                      AND (c.raw_customer_id IS NULL OR EXISTS (
                          SELECT FROM staging.customer_profile incumbent
                          WHERE incumbent.raw_customer_id=m.raw_customer_id
                            AND (incumbent.app_id,incumbent.source_system_id,incumbent.source_customer_id)
                                IS DISTINCT FROM (b.app_id,m.source_system_id,m.source_customer_id)
                      ))
                )
            """, (list(batch_ids),))
            if cursor.fetchone()[0]:
                raise RuntimeError('LOGICAL_SOURCE_PROJECTION_REQUIRED: selected logical source lacks current winner')
    # Identity is always current. Historical evidence is normalized separately.
    try:
        # A different completed ingestion can commit before this job obtains
        # the global matcher lock. A selected-only normalization would leave
        # that newer active origin unrepresented in the global read set.
        # Index-driven batch lookup keeps work bounded to distinct current
        # batches missing either an active profile or exact typed winner.
        if app_scoped and batch_ids:
            with conn.cursor() as cursor:
                cursor.execute("""
                    SELECT DISTINCT c.batch_id::text
                    FROM control.ingestion_batch b
                    JOIN raw.customer_current c ON c.batch_id=b.batch_id
                    WHERE b.app_id IS NOT NULL AND b.status='completed'
                      AND NOT EXISTS (SELECT FROM staging.customer_profile p
                          WHERE (p.app_id,p.source_system_id,p.source_customer_id)=
                                (b.app_id,c.source_system_id,c.source_customer_id)
                            AND (p.batch_id,p.raw_customer_id)=
                                (c.batch_id,c.raw_customer_id)
                            AND p.standardization_status IN ('READY','INSUFFICIENT_IDENTIFIERS')
                            AND EXISTS (SELECT FROM staging.customer_profile_history h
                                WHERE (h.batch_id,h.raw_customer_id,h.app_id,h.source_system_id,h.source_customer_id)=
                                      (c.batch_id,c.raw_customer_id,b.app_id,c.source_system_id,c.source_customer_id)
                                  AND h.standardization_status IN ('READY','INSUFFICIENT_IDENTIFIERS')))
                    ORDER BY c.batch_id::text
                    LIMIT 101
                """)
                missing_batches = [row[0] for row in cursor.fetchall()]
            if len(missing_batches) > 100:
                raise RuntimeError('LOGICAL_SOURCE_PROJECTION_REQUIRED: current inventory repair exceeds batch budget')
            for repair_batch in missing_batches:
                if repair_batch not in batch_ids:
                    _standardize_customers(conn, config, [repair_batch], chunk_size,
                                           history=True)
                    _standardize_customers(conn, config, [repair_batch], chunk_size,
                                           app_scoped=True)
        # Validate immutable evidence before touching the mutable current profile.
        for batch_id in batch_ids or []:
            _standardize_customers(conn, config, [batch_id], chunk_size, history=True)
        total = _standardize_customers(conn, config, batch_ids, chunk_size, app_scoped=app_scoped)
        conn.commit()
        return total
    except Exception:
        conn.rollback()
        raise


def _standardize_customers(conn, config, batch_ids, chunk_size, *, history=False, app_scoped=False):
    source = "raw.customer AS customer" if history else "raw.customer_current AS customer"
    selected_columns = ", ".join(
        f"customer.{column}" for column in RAW_COLUMNS
    )
    where = ""
    params: list[Any] = []
    if history:
        where = "WHERE customer.batch_id = ANY(%s::uuid[])"
        params.append(list(batch_ids))
    elif batch_ids:
        # A legacy raw UUID can span apps. Select only the exact member source
        # identity with the same immutable ingestion-batch app provenance.
        where = """WHERE EXISTS (
            SELECT 1 FROM raw.customer_batch_member AS member
            JOIN control.ingestion_batch AS selected_batch
              ON selected_batch.batch_id = member.ingestion_batch_id
            JOIN control.ingestion_batch AS current_batch
              ON current_batch.batch_id = customer.batch_id
            WHERE member.raw_customer_id = customer.raw_customer_id
              AND member.source_system_id = customer.source_system_id
              AND member.source_customer_id = customer.source_customer_id
              AND (to_jsonb(selected_batch)->>'app_id')
                  IS NOT DISTINCT FROM (to_jsonb(current_batch)->>'app_id')
              AND member.ingestion_batch_id = ANY(%s::uuid[])
        )"""
        params.append(list(batch_ids))

    total = 0
    last_id = None
    while True:
        page_where = where
        page_params = list(params)
        if last_id is not None:
            page_where += (
                (" AND " if page_where else "WHERE ")
                + "customer.raw_customer_id > %s"
            )
            page_params.append(last_id)
        page_params.append(chunk_size)
        with conn.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(
                f"""
                SELECT {selected_columns}
                FROM {source}
                {page_where}
                ORDER BY customer.raw_customer_id
                LIMIT %s
                """,
                page_params,
            )
            rows = cursor.fetchall()
        if not rows:
            break

        standardized = [standardize_record(dict(row), config) for row in rows]
        columns_sql = ", ".join(STAGING_COLUMNS)
        update_sql = ", ".join(
            f"{column} = EXCLUDED.{column}" for column in STAGING_COLUMNS if column != "raw_customer_id"
        )
        records = [
            tuple(Json(item[column]) if column == "attributes" else item[column] for column in STAGING_COLUMNS)
            for item in standardized
        ]
        target = "staging.customer_profile_history" if history else "staging.customer_profile"
        if app_scoped and not history:
            # 050 keeps incumbent standardized_record_id as the active row PK.
            # Match its literal batch app and source tuple, never the raw UUID.
            with conn.cursor() as cursor:
                for item in standardized:
                    cursor.execute("""
                        SELECT b.app_id FROM control.ingestion_batch b
                        WHERE b.batch_id=%s AND b.source_system_id=%s
                    """, (item['batch_id'], item['source_system_id']))
                    result = cursor.fetchone()
                    if result is None or result[0] is None:
                        raise RuntimeError('LOGICAL_SOURCE_PROJECTION_REQUIRED: missing current batch app')
                    app = result[0]
                    cursor.execute("""
                        SELECT standardized_record_id,raw_customer_id
                        FROM staging.customer_profile
                        WHERE app_id=%s AND source_system_id=%s AND source_customer_id=%s
                        FOR UPDATE
                    """, (app, item['source_system_id'], item['source_customer_id']))
                    existing = cursor.fetchone()
                    if existing:
                        changed_raw = str(existing[1]) != str(item['raw_customer_id'])
                        if changed_raw:
                            # The 019 history FK pins every observation to the
                            # mutable current UUID/source. Only the 053 exact
                            # immutable member FK permits this provenance move.
                            cursor.execute("""
                                SELECT EXISTS (SELECT FROM pg_constraint
                                    WHERE conrelid='raw.customer'::regclass
                                      AND conname='customer_history_member_tuple_fkey'
                                      AND contype='f' AND convalidated)
                                   AND NOT EXISTS (SELECT FROM pg_constraint
                                    WHERE conrelid='raw.customer'::regclass
                                      AND conname='customer_history_current_fkey')
                            """)
                            if not cursor.fetchone()[0]:
                                raise RuntimeError('LOGICAL_SOURCE_PROJECTION_REQUIRED: raw provenance changed without exact immutable history FK')
                            cursor.execute("""
                                SELECT EXISTS (SELECT FROM staging.customer_profile p
                                    WHERE p.raw_customer_id=%s
                                      AND p.standardized_record_id<>%s)
                                    OR EXISTS (SELECT FROM raw.customer h
                                      WHERE h.raw_customer_id=%s
                                        AND (h.app_id,h.source_system_id,h.source_customer_id)
                                          IS DISTINCT FROM (%s,%s,%s))
                                    OR NOT EXISTS (SELECT FROM raw.customer h
                                      JOIN raw.customer_batch_member m ON
                                        (m.ingestion_batch_id,m.raw_customer_id,m.source_system_id,m.source_customer_id)=
                                        (h.batch_id,h.raw_customer_id,h.source_system_id,h.source_customer_id)
                                      WHERE (h.batch_id,h.raw_customer_id,h.source_system_id,h.source_customer_id,h.app_id)=
                                        (%s,%s,%s,%s,%s))
                            """, (item['raw_customer_id'], existing[0], item['raw_customer_id'],
                                  app, item['source_system_id'], item['source_customer_id'],
                                  item['batch_id'], item['raw_customer_id'],
                                  item['source_system_id'], item['source_customer_id'], app))
                            if cursor.fetchone()[0]:
                                raise RuntimeError('LOGICAL_SOURCE_PROJECTION_REQUIRED: raw UUID collision or invalid exact observation lineage')
                        cursor.execute(
                            f"UPDATE {target} SET " + ", ".join(
                                f"{column}=%s" for column in STAGING_COLUMNS
                                if column not in ('source_system_id', 'source_customer_id')
                                and (changed_raw or column != 'raw_customer_id')
                            ) + ", standardized_at=now() WHERE standardized_record_id=%s",
                            [Json(item[column]) if column == 'attributes' else item[column]
                             for column in STAGING_COLUMNS
                             if column not in ('source_system_id', 'source_customer_id')
                             and (changed_raw or column != 'raw_customer_id')]
                            + [existing[0]],
                        )
                    else:
                        cursor.execute(
                            f"INSERT INTO {target} ({columns_sql},app_id) VALUES (" +
                            ','.join(['%s'] * (len(STAGING_COLUMNS) + 1)) + ')',
                            tuple(Json(item[column]) if column == 'attributes' else item[column]
                                  for column in STAGING_COLUMNS) + (app,),
                        )
            total += len(rows)
            last_id = rows[-1]['raw_customer_id']
            continue
        conflict = "(batch_id, raw_customer_id) DO NOTHING" if history else f"(raw_customer_id) DO UPDATE SET {update_sql}, standardized_at = now()"
        with conn.cursor() as cursor:
            execute_values(
                cursor,
                f"""
                INSERT INTO {target} ({columns_sql}) VALUES %s
                ON CONFLICT {conflict}
                """,
                records,
                page_size=chunk_size,
            )
        if history:
            # Read after ON CONFLICT: at READ COMMITTED this also sees a competing
            # insert that committed while the conflict check waited. Compare every
            # typed field, not only a fingerprint or a declared version.
            with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute(
                    f"SELECT {columns_sql} FROM {target} "
                    "WHERE batch_id = %s AND raw_customer_id = ANY(%s::uuid[])",
                    (batch_ids[0], [item['raw_customer_id'] for item in standardized]),
                )
                persisted = {str(row['raw_customer_id']): dict(row) for row in cursor.fetchall()}
            for item in standardized:
                if persisted.get(str(item['raw_customer_id'])) != item:
                    raise ValueError('Historical normalization retry mismatch: typed values or version changed')
        total += len(rows)
        last_id = rows[-1]["raw_customer_id"]
    return total
