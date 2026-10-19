-- Generated from read-only live-schema-contract.json; no production rows.
-- Pinned customer/current/membership columns, all indexes and constraints.
-- Control/profile tables remain bounded test scaffolding; not full-pipeline fixture.
DROP SCHEMA IF EXISTS quality CASCADE; DROP SCHEMA IF EXISTS raw CASCADE; DROP SCHEMA IF EXISTS staging CASCADE; DROP SCHEMA IF EXISTS control CASCADE;
CREATE SCHEMA raw; CREATE SCHEMA control; CREATE SCHEMA staging;
CREATE TABLE control.source_system(source_system_id bigint PRIMARY KEY, source_code varchar(50) UNIQUE NOT NULL, source_name varchar(255) NOT NULL); INSERT INTO control.source_system VALUES (1,'synthetic-1','Synthetic 1'),(2,'synthetic-2','Synthetic 2');
CREATE TABLE control.source_landing_batch(batch_id uuid PRIMARY KEY);
CREATE TABLE control.ingestion_batch(batch_id uuid PRIMARY KEY,source_system_id bigint REFERENCES control.source_system,created_at timestamptz NOT NULL DEFAULT now());
CREATE TABLE raw.customer(
"raw_customer_id" uuid NOT NULL DEFAULT gen_random_uuid(),
"batch_id" uuid NOT NULL,
"source_system_id" bigint NOT NULL,
"customer_id" text,
"source_customer_id" character varying(255) NOT NULL,
"external_id" text,
"first_name" text,
"middle_name" text,
"last_name" text,
"full_name" text,
"nickname" text,
"gender" text,
"date_of_birth" date,
"birthday" date,
"age" integer,
"email" text,
"phone" text,
"line_user_id" text,
"facebook_id" text,
"google_id" text,
"apple_id" text,
"member_code" text,
"customer_type" text,
"status" text NOT NULL DEFAULT 'ACTIVE'::text,
"language" text,
"address_line_1" text,
"address_line_2" text,
"district" text,
"province" text,
"postal_code" text,
"country" text,
"source" text,
"source_system" text,
"external_ids" jsonb,
"attributes" jsonb,
"raw_payload" jsonb,
"source_created_at" timestamp with time zone,
"source_updated_at" timestamp with time zone,
"created_at" timestamp with time zone NOT NULL DEFAULT now(),
"updated_at" timestamp with time zone NOT NULL DEFAULT now(),
"ingested_at" timestamp with time zone NOT NULL DEFAULT now(),
CONSTRAINT "customer_age_check" CHECK (((age IS NULL) OR (age >= 0))),
CONSTRAINT "customer_batch_id_fkey" FOREIGN KEY (batch_id) REFERENCES control.ingestion_batch(batch_id),
CONSTRAINT "customer_pkey" PRIMARY KEY (raw_customer_id),
CONSTRAINT "customer_source_system_id_fkey" FOREIGN KEY (source_system_id) REFERENCES control.source_system(source_system_id),
CONSTRAINT "customer_status_check" CHECK ((status = ANY (ARRAY['ACTIVE'::text, 'INACTIVE'::text, 'BLOCKED'::text, 'DELETED'::text, 'SUSPENDED'::text])))
);
CREATE UNIQUE INDEX uq_customer_source_customer_id ON raw.customer USING btree (source_system_id, source_customer_id);
CREATE INDEX idx_raw_customer_batch_id ON raw.customer USING btree (batch_id);
CREATE INDEX idx_raw_customer_source_system_id ON raw.customer USING btree (source_system_id);
CREATE INDEX idx_raw_customer_email ON raw.customer USING btree (email) WHERE (email IS NOT NULL);
CREATE INDEX idx_raw_customer_phone ON raw.customer USING btree (phone) WHERE (phone IS NOT NULL);
CREATE INDEX idx_raw_customer_line_user_id ON raw.customer USING btree (line_user_id) WHERE (line_user_id IS NOT NULL);
CREATE INDEX idx_raw_customer_member_code ON raw.customer USING btree (member_code) WHERE (member_code IS NOT NULL);
CREATE INDEX idx_raw_customer_status ON raw.customer USING btree (status);
CREATE INDEX idx_raw_customer_ingested_at ON raw.customer USING btree (ingested_at);
CREATE INDEX idx_raw_customer_external_ids_gin ON raw.customer USING gin (external_ids);
CREATE INDEX idx_raw_customer_attributes_gin ON raw.customer USING gin (attributes);
CREATE UNIQUE INDEX uq_customer_batch_source_customer ON raw.customer USING btree (batch_id, source_system_id, source_customer_id);
CREATE INDEX idx_raw_customer_source_external_id ON raw.customer USING btree (source_system_id, external_id) WHERE (external_id IS NOT NULL);
CREATE TABLE raw.customer_batch_member(
"ingestion_batch_id" uuid NOT NULL,
"raw_customer_id" uuid NOT NULL,
"source_system_id" bigint NOT NULL,
"source_customer_id" character varying(255) NOT NULL,
"recorded_at" timestamp with time zone NOT NULL DEFAULT now(),
CONSTRAINT "customer_batch_member_batch_fkey" FOREIGN KEY (ingestion_batch_id) REFERENCES control.ingestion_batch(batch_id),
CONSTRAINT "customer_batch_member_pkey" PRIMARY KEY (ingestion_batch_id, raw_customer_id),
CONSTRAINT "customer_batch_member_raw_customer_fkey" FOREIGN KEY (raw_customer_id) REFERENCES raw.customer(raw_customer_id),
CONSTRAINT "customer_batch_member_source_system_fkey" FOREIGN KEY (source_system_id) REFERENCES control.source_system(source_system_id),
CONSTRAINT "uq_customer_batch_member_source_customer" UNIQUE (ingestion_batch_id, source_system_id, source_customer_id)
);
CREATE INDEX ix_customer_batch_member_raw_customer ON raw.customer_batch_member USING btree (raw_customer_id);
CREATE FUNCTION raw.set_customer_updated_at() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN NEW.updated_at=now(); RETURN NEW; END $$;
CREATE TRIGGER trg_raw_customer_updated_at BEFORE UPDATE ON raw.customer FOR EACH ROW EXECUTE FUNCTION raw.set_customer_updated_at();
CREATE FUNCTION raw.reject_customer_batch_member_mutation() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RAISE EXCEPTION 'Customer batch membership is immutable'; END $$;
CREATE TRIGGER customer_batch_member_immutable BEFORE DELETE OR UPDATE OR TRUNCATE ON raw.customer_batch_member FOR EACH STATEMENT EXECUTE FUNCTION raw.reject_customer_batch_member_mutation();
CREATE TABLE staging.customer_profile(standardized_record_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),raw_customer_id uuid UNIQUE REFERENCES raw.customer,batch_id uuid,source_system_id bigint);
