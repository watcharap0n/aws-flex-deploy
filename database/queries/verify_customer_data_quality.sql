-- Read-only checks to run after applying 015_customer_data_quality.sql.

BEGIN TRANSACTION READ ONLY;

SELECT landing_batch_id,
       overall_status,
       validation_available,
       validation_run_id,
       validation_run_status,
       validation_source_row_count,
       validation_canonical_row_count,
       validation_standardized_record_count,
       validation_issue_record_count,
       validation_existing_source_record_count,
       validation_existing_entity_record_count,
       validation_new_to_cdp_record_count,
       validation_ambiguous_entity_record_count,
       error_stage,
       error_code,
       error_message,
       updated_at,
       expected_ingestion.batch_id AS expected_latest_ingestion_batch_id,
       expected_job.job_id AS expected_latest_stitching_job_id,
       ingestion_batch_id IS NOT DISTINCT FROM expected_ingestion.batch_id
           AS latest_ingestion_selected,
       stitching_job_id IS NOT DISTINCT FROM expected_job.job_id
           AS latest_job_attempt_selected
FROM control.customer_pipeline_status AS pipeline
LEFT JOIN LATERAL (
    SELECT candidate.batch_id
    FROM control.ingestion_batch AS candidate
    WHERE candidate.landing_batch_id = pipeline.landing_batch_id
    ORDER BY candidate.created_at DESC, candidate.batch_id DESC
    LIMIT 1
) AS expected_ingestion ON TRUE
LEFT JOIN LATERAL (
    SELECT candidate.job_id
    FROM control.stitching_job AS candidate
    WHERE candidate.ingestion_batch_id = expected_ingestion.batch_id
    ORDER BY candidate.attempt DESC, candidate.created_at DESC
    LIMIT 1
) AS expected_job ON TRUE
ORDER BY pipeline.updated_at DESC NULLS LAST
LIMIT 20;

SELECT vr.validation_run_id,
       vr.landing_batch_id,
       vr.ingestion_batch_id,
       vr.stitching_job_id,
       vr.attempt,
       vr.run_status,
       vr.source_row_count,
       count(DISTINCT record.validation_record_id) AS stored_record_count,
       count(DISTINCT metric.metric_id) AS stored_metric_count,
       vr.source_row_count = count(DISTINCT record.validation_record_id)
           AS source_row_grain_reconciles
FROM quality.customer_validation_run AS vr
LEFT JOIN quality.customer_validation_record AS record
  ON record.validation_run_id = vr.validation_run_id
LEFT JOIN quality.customer_validation_metric AS metric
  ON metric.validation_run_id = vr.validation_run_id
GROUP BY vr.validation_run_id
ORDER BY vr.created_at DESC
LIMIT 20;

SELECT validation_run_id,
       source_row_count,
       blank_key_row_count,
       duplicate_excess_row_count,
       canonical_row_count,
       source_row_count
         - blank_key_row_count
         - duplicate_excess_row_count
         - canonical_row_count AS reconciliation_delta
FROM quality.customer_validation_run
WHERE run_status = 'SUCCEEDED'
ORDER BY completed_at DESC
LIMIT 20;

SELECT landing_batch_id,
       dimension,
       rule_code,
       field_name,
       evaluation_status,
       severity,
       failure_count,
       denominator,
       metric_rate,
       baseline_sample_count
FROM control.customer_data_quality_metric
ORDER BY created_at DESC
LIMIT 100;

SELECT landing_batch_id,
       validation_run_id,
       source_row_number,
       selected_for_canonical,
       disposition,
       existing_source_record,
       identity_classification,
       entity_candidate_count,
       matched_identifier_types,
       issue_codes
FROM control.customer_data_quality_record
ORDER BY validation_run_id DESC, source_row_number
LIMIT 100;

ROLLBACK;
