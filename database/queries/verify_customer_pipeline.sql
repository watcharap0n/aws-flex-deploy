-- Read-only checks to run after a single-file customer ingestion.

BEGIN TRANSACTION READ ONLY;

SELECT
    ss.source_code,
    count(*) AS raw_customers,
    count(DISTINCT c.source_customer_id) AS distinct_source_customer_ids,
    count(DISTINCT c.batch_id) AS batches,
    min(c.ingested_at) AS first_ingested_at,
    max(c.ingested_at) AS last_ingested_at
FROM raw.customer AS c
JOIN control.source_system AS ss USING (source_system_id)
GROUP BY ss.source_code
ORDER BY ss.source_code;

SELECT
    ib.batch_id,
    ss.source_code,
    ib.object_name,
    ib.status,
    ib.records_received,
    ib.records_loaded,
    ib.records_rejected,
    ib.checksum,
    ib.created_at,
    ib.completed_at
FROM control.ingestion_batch AS ib
JOIN control.source_system AS ss USING (source_system_id)
ORDER BY ib.created_at DESC
LIMIT 20;

SELECT
    count(*) AS staging_rows,
    count(*) FILTER (WHERE r.raw_customer_id IS NULL) AS orphaned_from_raw
FROM staging.customer_profile AS s
LEFT JOIN raw.customer AS r USING (raw_customer_id);

SELECT
    count(*) AS entity_members,
    count(*) FILTER (WHERE s.standardized_record_id IS NULL)
        AS orphaned_from_staging,
    count(*) FILTER (WHERE e.customer_entity_id IS NULL)
        AS orphaned_from_entity
FROM identity.customer_entity_member AS m
LEFT JOIN staging.customer_profile AS s USING (standardized_record_id)
LEFT JOIN identity.customer_entity AS e USING (customer_entity_id);

ROLLBACK;
