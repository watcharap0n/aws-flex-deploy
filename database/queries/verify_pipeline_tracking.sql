-- Read-only checks to run after applying 014_pipeline_tracking.sql.

BEGIN TRANSACTION READ ONLY;

SELECT landing_batch_id,
       source_code,
       batch_ref,
       overall_status,
       ingestion_batch_id,
       stitching_job_id,
       resolution_run_id,
       output_row_count,
       records_loaded,
       batch_member_count,
       standardized_record_count,
       member_record_count,
       entity_count,
       error_code,
       error_message,
       updated_at
FROM control.customer_pipeline_status
ORDER BY updated_at DESC NULLS LAST
LIMIT 20;

SELECT count(*) AS ingestion_batches,
       count(landing_batch_id) AS linked_to_landing,
       count(*) FILTER (
           WHERE object_name LIKE 'raw/%/customer.csv'
             AND landing_batch_id IS NULL
       ) AS canonical_outputs_without_lineage
FROM control.ingestion_batch;

SELECT count(*) AS current_raw_rows,
       count(DISTINCT raw_customer_id) AS current_raw_customers,
       (SELECT count(*) FROM raw.customer_batch_member) AS historical_memberships
FROM raw.customer;

SELECT status,
       count(*) AS jobs,
       min(created_at) AS first_created_at,
       max(updated_at) AS last_updated_at
FROM control.stitching_job
GROUP BY status
ORDER BY status;

SELECT sj.job_id,
       sj.ingestion_batch_id,
       sj.status,
       sj.ecs_task_arn,
       rr.run_id,
       rr.status AS resolution_status,
       sj.selected_record_count,
       sj.standardized_record_count,
       sj.member_record_count,
       sj.entity_count,
       sj.completed_at,
       sj.error_code,
       sj.error_message
FROM control.stitching_job AS sj
LEFT JOIN identity.resolution_run AS rr
  ON rr.stitching_job_id = sj.job_id
ORDER BY sj.created_at DESC
LIMIT 20;

ROLLBACK;
