-- Disposable-PG candidate only. Not a deployable migration or activation.
-- Preserve immutable observations. A batch member proves the exact observation
-- tuple even when its provenance UUID belongs to a different current source.
BEGIN;
SET LOCAL lock_timeout = '3s';
LOCK TABLE raw.customer, raw.customer_batch_member, raw.customer_current
    IN ACCESS EXCLUSIVE MODE;
DO $candidate$
BEGIN
    IF EXISTS (
        SELECT FROM raw.customer h
        LEFT JOIN raw.customer_batch_member m
          ON (m.ingestion_batch_id,m.raw_customer_id,m.source_system_id,m.source_customer_id)
           = (h.batch_id,h.raw_customer_id,h.source_system_id,h.source_customer_id)
        WHERE m.raw_customer_id IS NULL
    ) THEN
        RAISE EXCEPTION 'Missing exact immutable batch/source membership';
    END IF;
END $candidate$;
ALTER TABLE raw.customer_batch_member
    ADD CONSTRAINT customer_batch_member_observation_tuple_key
    UNIQUE (ingestion_batch_id,raw_customer_id,source_system_id,source_customer_id);
ALTER TABLE raw.customer DROP CONSTRAINT customer_history_current_fkey;
ALTER TABLE raw.customer
    ADD CONSTRAINT customer_history_member_tuple_fkey
    FOREIGN KEY (batch_id,raw_customer_id,source_system_id,source_customer_id)
    REFERENCES raw.customer_batch_member
        (ingestion_batch_id,raw_customer_id,source_system_id,source_customer_id)
    DEFERRABLE INITIALLY DEFERRED;
COMMIT;
