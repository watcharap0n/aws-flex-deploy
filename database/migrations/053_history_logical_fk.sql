-- Coordinated, one-shot schema-only cutover; pause customer writers while applying.
-- Observation ownership is exact immutable (batch,raw,source,user), not the
-- mutable raw.customer_current row, whose UUID may represent another source.
BEGIN;
SET LOCAL lock_timeout='10s';
LOCK TABLE raw.customer,raw.customer_batch_member,raw.customer_current,
    staging.customer_profile_history IN ACCESS EXCLUSIVE MODE;
DO $preflight$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_constraint WHERE conrelid='raw.customer'::regclass
          AND conname='customer_history_current_fkey' AND contype='f'
          AND confrelid='raw.customer_current'::regclass AND convalidated)
       OR to_regclass('raw.customer_batch_member') IS NULL
       OR NOT EXISTS (SELECT FROM pg_constraint WHERE conrelid='raw.customer_batch_member'::regclass
          AND conname='customer_batch_member_pkey' AND contype='p' AND convalidated)
       OR NOT EXISTS (SELECT FROM pg_constraint WHERE conrelid='raw.customer_batch_member'::regclass
          AND conname='customer_batch_member_raw_customer_fkey' AND contype='f'
          AND confrelid='raw.customer_current'::regclass AND convalidated)
       OR NOT EXISTS (SELECT FROM pg_trigger WHERE tgrelid='raw.customer_batch_member'::regclass
          AND tgname='customer_batch_member_immutable' AND tgenabled='O')
       OR NOT EXISTS (SELECT FROM pg_trigger WHERE tgrelid='raw.customer'::regclass
          AND tgname='customer_history_immutable' AND tgenabled='O')
       OR NOT EXISTS (SELECT FROM pg_trigger WHERE tgrelid='staging.customer_profile_history'::regclass
          AND tgname='customer_profile_history_immutable' AND tgenabled='O')
       OR NOT EXISTS (SELECT FROM pg_constraint WHERE conrelid='staging.customer_profile_history'::regclass
          AND contype='f' AND confrelid='raw.customer'::regclass AND convalidated) THEN
        RAISE EXCEPTION '053 incompatible 019/014 immutable history catalog or already installed';
    END IF;
    IF EXISTS (SELECT FROM raw.customer h
        LEFT JOIN control.ingestion_batch b ON b.batch_id=h.batch_id
        LEFT JOIN raw.customer_batch_member m ON
          (m.ingestion_batch_id,m.raw_customer_id,m.source_system_id,m.source_customer_id)=
          (h.batch_id,h.raw_customer_id,h.source_system_id,h.source_customer_id)
        WHERE m.raw_customer_id IS NULL OR b.batch_id IS NULL
           OR h.app_id IS DISTINCT FROM b.app_id
           OR h.source_system_id IS DISTINCT FROM b.source_system_id)
       OR EXISTS (SELECT FROM raw.customer_batch_member m
          LEFT JOIN control.ingestion_batch b ON b.batch_id=m.ingestion_batch_id
          WHERE b.batch_id IS NULL OR m.source_system_id IS DISTINCT FROM b.source_system_id)
       OR EXISTS (SELECT FROM staging.customer_profile_history p
          JOIN raw.customer h ON (p.batch_id,p.raw_customer_id)=(h.batch_id,h.raw_customer_id)
          WHERE (p.app_id,p.source_system_id,p.source_customer_id) IS DISTINCT FROM
                (h.app_id,h.source_system_id,h.source_customer_id)) THEN
        RAISE EXCEPTION '053 missing or inconsistent exact immutable batch/source lineage';
    END IF;
END $preflight$;
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
