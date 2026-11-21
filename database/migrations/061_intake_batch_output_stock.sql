-- 061: the Stock dataset type (STOCK_BALANCE, 2026-11-21) reports its intake status like the other fact types.
-- Additive: widens the target_entity check of control.intake_batch_output.
-- Rollback (only while no STOCK_BALANCE row exists): re-run 060.
BEGIN;
ALTER TABLE control.intake_batch_output DROP CONSTRAINT IF EXISTS intake_batch_output_target_entity_check;
ALTER TABLE control.intake_batch_output ADD CONSTRAINT intake_batch_output_target_entity_check
    CHECK (target_entity IN ('CUSTOMER', 'TRANSACTION', 'EVENT', 'ASSET', 'RELATIONSHIP',
                             'TRANSACTION_ITEM', 'PRODUCT', 'STORE', 'CAMPAIGN', 'CONSENT',
                             'POINT_LEDGER', 'POINT_BALANCE', 'COUPON', 'COUPON_WALLET', 'STOCK_BALANCE'));
COMMIT;
