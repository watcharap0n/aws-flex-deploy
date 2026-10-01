-- ClickHouse READ ONLY. Run only on the intended database after installation.
-- Counts are at source-user grain. No customer IDs or PII payloads are emitted.
-- The canonical SHA-256 value is independently computed/verified by the worker;
-- a matching stored marker alone is NOT a new content verification.
SELECT p.transfer_id,p.resolution_run_id,p.workspace_id,p.app_id,p.publication_seq,
       p.row_count AS expected_rows,p.source_sha256,
       v.physical_rows,v.logical_users,v.first_row,v.last_row,
       v.physical_rows=p.row_count AND v.logical_users=p.row_count
         AND (p.row_count=0 OR (v.first_row=1 AND v.last_row=p.row_count)) AS count_and_grain_match
FROM cdp_customer.identity_publications p
LEFT JOIN (
    SELECT resolution_run_id,count() AS physical_rows,
           uniqExact(tuple(app_id,user_id)) AS logical_users,
           min(row_number) AS first_row,max(row_number) AS last_row
    FROM cdp_customer.identity_profile_versions GROUP BY resolution_run_id
) v USING(resolution_run_id)
ORDER BY p.publication_seq;

-- No rows expected: markers are physically unique, not eventually deduplicated.
SELECT resolution_run_id,count() AS marker_rows
FROM cdp_customer.identity_publications GROUP BY resolution_run_id HAVING count()>1;

-- Both returned counts must be zero. Run with publication paused for a stable
-- current-state reconciliation; paired live reads should pin a committed run.
-- Legacy profile-only users have NULL resolution lineage and are excluded.
SELECT countIf(m.app_id!=p.app_id OR m.user_id!=p.customer_id
            OR m.resolution_run_id!=p.resolution_run_id
            OR m.customer_entity_id!=p.customer_entity_id) AS mismatched_current_rows,
       countIf(m.app_id IS NULL OR p.app_id IS NULL) AS missing_current_rows
FROM cdp_customer.identity_mapping m
FULL OUTER JOIN (
    SELECT app_id,customer_id,customer_entity_id,resolution_run_id
    FROM cdp_customer.customer_profile WHERE resolution_run_id IS NOT NULL
) p ON m.app_id=p.app_id AND m.user_id=p.customer_id
SETTINGS join_use_nulls=1;

-- Unpublished partitions are safe to retain, not evidence of completion.
SELECT v.resolution_run_id,count() AS unpublished_rows
FROM cdp_customer.identity_profile_versions v
LEFT ANTI JOIN cdp_customer.identity_publications p USING(resolution_run_id)
GROUP BY v.resolution_run_id;
