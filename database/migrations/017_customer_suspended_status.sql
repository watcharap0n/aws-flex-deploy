-- Preserve the SUSPENDED lifecycle state supported by the customer validation
-- contract (customer-data-quality-v1). BLOCKED remains valid for existing data;
-- it is not an alias for SUSPENDED. Unknown states remain rejected.
BEGIN;
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '2min';
ALTER TABLE raw.customer DROP CONSTRAINT customer_status_check;
ALTER TABLE raw.customer ADD CONSTRAINT customer_status_check
    CHECK (status IN ('ACTIVE', 'INACTIVE', 'BLOCKED', 'DELETED', 'SUSPENDED'));
COMMIT;
