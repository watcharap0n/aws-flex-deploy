-- Additive recovery outcomes only: never rewrites ingestion/execution or verified ledgers.
-- Operator applies only after review. No runtime write grants.
BEGIN;
SET LOCAL lock_timeout='5s';
SET LOCAL statement_timeout='120s';
CREATE TABLE control.customer_history_recovery_outcome (
 outcome_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
 -- Caller persists/reuses this handle across uncertain commits. No random default.
 attempt_id uuid NOT NULL UNIQUE,
 ingestion_batch_id uuid NOT NULL REFERENCES control.ingestion_batch(batch_id),
 status text NOT NULL CHECK (status IN ('FAILED','DEFERRED')),
 reason_code text NOT NULL CHECK (reason_code ~ '^[A-Z][A-Z0-9_]{2,127}$'),
 evidence jsonb NOT NULL CHECK (jsonb_typeof(evidence)='object'),
 recorded_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
CREATE INDEX customer_history_recovery_outcome_batch_idx ON control.customer_history_recovery_outcome(ingestion_batch_id,outcome_id DESC);
CREATE TRIGGER customer_history_recovery_outcome_immutable
 BEFORE UPDATE OR DELETE OR TRUNCATE ON control.customer_history_recovery_outcome
 FOR EACH STATEMENT EXECUTE FUNCTION raw.reject_customer_history_mutation();
CREATE FUNCTION control.check_customer_history_recovery_outcome() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE prior control.customer_history_recovery_outcome%ROWTYPE; k text; v jsonb;
BEGIN
 -- Serialize duplicate delivery. Mismatched reuse is an error, never last-write-wins.
 PERFORM pg_advisory_xact_lock(hashtextextended(NEW.attempt_id::text, 21021));
 SELECT * INTO prior FROM control.customer_history_recovery_outcome WHERE attempt_id=NEW.attempt_id;
 IF FOUND THEN
  IF (prior.ingestion_batch_id,prior.status,prior.reason_code,prior.evidence)
      IS DISTINCT FROM (NEW.ingestion_batch_id,NEW.status,NEW.reason_code,NEW.evidence) THEN
   RAISE EXCEPTION 'RECOVERY_ATTEMPT_ID_MISMATCH';
  END IF;
  RETURN NULL;
 END IF;
 -- Reject customer payloads/paths/free text rather than relying on an operator comment.
 IF jsonb_typeof(NEW.evidence) IS DISTINCT FROM 'object' OR octet_length(NEW.evidence::text)>4096 THEN
  RAISE EXCEPTION 'UNSAFE_RECOVERY_EVIDENCE';
 END IF;
 FOR k,v IN SELECT * FROM jsonb_each(NEW.evidence) LOOP
  IF k IN ('records_loaded','membership_rows','history_rows','output_row_count') THEN
   IF v <> 'null'::jsonb AND (jsonb_typeof(v)<>'number' OR v::text !~ '^[0-9]{1,18}$') THEN RAISE EXCEPTION 'UNSAFE_RECOVERY_EVIDENCE'; END IF;
  ELSIF k IN ('artifact_sha256','evidence_sha256','object_name_sha256','output_key_sha256') THEN
   IF v <> 'null'::jsonb AND (jsonb_typeof(v)<>'string' OR v#>>'{}' !~ '^[0-9a-f]{64}$') THEN RAISE EXCEPTION 'UNSAFE_RECOVERY_EVIDENCE'; END IF;
  ELSIF k='landing_batch_id' THEN
   IF v <> 'null'::jsonb AND (jsonb_typeof(v)<>'string' OR v#>>'{}' !~ '^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$') THEN RAISE EXCEPTION 'UNSAFE_RECOVERY_EVIDENCE'; END IF;
  ELSIF k IN ('lineage_matches','artifact_lineage_present','membership_complete') THEN
   IF jsonb_typeof(v)<>'boolean' THEN RAISE EXCEPTION 'UNSAFE_RECOVERY_EVIDENCE'; END IF;
  ELSE RAISE EXCEPTION 'UNSAFE_RECOVERY_EVIDENCE';
  END IF;
 END LOOP;
 -- Serialize against verified ledger INSERT even when the batch has no ledger yet.
 LOCK TABLE control.customer_history_recovery IN SHARE MODE;
 IF EXISTS(SELECT 1 FROM control.customer_history_recovery WHERE ingestion_batch_id=NEW.ingestion_batch_id) THEN
  RAISE EXCEPTION 'VERIFIED_HISTORY_CANNOT_BE_FAILED';
 END IF;
 -- Complete native upload evidence is not failed merely because it lacks a recovery ledger.
 IF EXISTS(SELECT 1 FROM control.ingestion_batch b WHERE b.batch_id=NEW.ingestion_batch_id
   AND b.status='completed' AND b.records_loaded>0
   AND b.records_loaded=(SELECT count(*) FROM raw.customer WHERE batch_id=b.batch_id)
   AND b.records_loaded=(SELECT count(*) FROM raw.customer_batch_member WHERE ingestion_batch_id=b.batch_id)
   AND NOT EXISTS(SELECT 1 FROM raw.customer_batch_member m WHERE m.ingestion_batch_id=b.batch_id
     AND NOT EXISTS(SELECT 1 FROM raw.customer h WHERE h.batch_id=b.batch_id AND h.raw_customer_id=m.raw_customer_id
       AND h.source_system_id=m.source_system_id AND h.source_customer_id=m.source_customer_id))) THEN
  RAISE EXCEPTION 'COMPLETE_HISTORY_CANNOT_BE_FAILED';
 END IF;
 RETURN NEW;
END $$;
CREATE TRIGGER customer_history_recovery_outcome_check BEFORE INSERT ON control.customer_history_recovery_outcome
 FOR EACH ROW EXECUTE FUNCTION control.check_customer_history_recovery_outcome();
CREATE VIEW control.customer_history_recovery_status AS
 SELECT ingestion_batch_id,'VERIFIED'::text AS status,NULL::text AS reason_code,
        verified_at AS recorded_at,provenance AS evidence
 FROM control.customer_history_recovery
 UNION ALL
 SELECT o.ingestion_batch_id,o.status,o.reason_code,o.recorded_at,o.evidence
 FROM (SELECT DISTINCT ON (ingestion_batch_id) *
       FROM control.customer_history_recovery_outcome
       ORDER BY ingestion_batch_id,outcome_id DESC) o
 WHERE NOT EXISTS(SELECT 1 FROM control.customer_history_recovery r WHERE r.ingestion_batch_id=o.ingestion_batch_id);
-- Append fields to the exact installed definition; preserve OID, ACL, old columns and semantics.
-- Absent pipeline view is allowed for minimal installations; never fabricate a replacement.
DO $$ DECLARE definition text; BEGIN
 IF to_regclass('control.customer_pipeline_status') IS NOT NULL THEN
  IF EXISTS(SELECT 1 FROM pg_attribute WHERE attrelid='control.customer_pipeline_status'::regclass
    AND attname IN ('history_recovery_status','history_recovery_reason_code','history_recovery_recorded_at') AND NOT attisdropped) THEN
   RAISE EXCEPTION 'PIPELINE_HISTORY_FIELDS_ALREADY_EXIST';
  END IF;
  definition := rtrim(pg_get_viewdef('control.customer_pipeline_status'::regclass,true), E';\n ');
  EXECUTE 'CREATE OR REPLACE VIEW control.customer_pipeline_status AS SELECT p.*, h.status AS history_recovery_status, h.reason_code AS history_recovery_reason_code, h.recorded_at AS history_recovery_recorded_at FROM ('
    || definition || ') p LEFT JOIN control.customer_history_recovery_status h ON h.ingestion_batch_id=p.ingestion_batch_id';
 END IF;
END $$;
-- Match explicitly granted history readers, with no runtime write permission.
DO $$ DECLARE reader record; BEGIN
 FOR reader IN SELECT DISTINCT grantee FROM information_schema.role_table_grants
  WHERE table_schema='raw' AND table_name='customer' AND privilege_type='SELECT' AND grantee<>current_user
 LOOP
  EXECUTE format('GRANT SELECT ON control.customer_history_recovery_outcome, control.customer_history_recovery_status TO %I',reader.grantee);
 END LOOP;
END $$;
COMMENT ON TABLE control.customer_history_recovery_outcome IS
 'Append-only historical recovery failures/deferrals, NOT ingestion or stitching execution state. No customer payloads in evidence. Later verified recovery supersedes display only.';
COMMENT ON VIEW control.customer_history_recovery_status IS
 'History recovery status for existing readers; VERIFIED evidence always wins. Missing row means not assessed, never failed by age.';
COMMIT;
