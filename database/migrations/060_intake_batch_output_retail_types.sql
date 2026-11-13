-- 060: the retail dataset types (2026-11-13) report their intake status like the other fact types.
-- Additive: widens the target_entity check of control.intake_batch_output.
-- Rollback (only while no row of a new type exists):
--   ALTER TABLE control.intake_batch_output DROP CONSTRAINT intake_batch_output_target_entity_check;
--   ALTER TABLE control.intake_batch_output ADD CONSTRAINT intake_batch_output_target_entity_check
--     CHECK (target_entity IN ('CUSTOMER', 'TRANSACTION', 'EVENT', 'ASSET', 'RELATIONSHIP'));
BEGIN;
ALTER TABLE control.intake_batch_output DROP CONSTRAINT IF EXISTS intake_batch_output_target_entity_check;
ALTER TABLE control.intake_batch_output ADD CONSTRAINT intake_batch_output_target_entity_check
    CHECK (target_entity IN ('CUSTOMER', 'TRANSACTION', 'EVENT', 'ASSET', 'RELATIONSHIP',
                             'TRANSACTION_ITEM', 'PRODUCT', 'STORE', 'CAMPAIGN', 'CONSENT',
                             'POINT_LEDGER', 'POINT_BALANCE', 'COUPON', 'COUPON_WALLET'));
COMMIT;
