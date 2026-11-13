"""Generic intake contracts (v2).

CUSTOMER / TRANSACTION / EVENT mirror journey-builder-api
`app/modules/connectors/entities.py` (keep both in sync). ASSET and RELATIONSHIP add
the people-to-things and people-to-people layer (households, shared vehicles, ...).
Retail types (2026-11-13): bill lines, product, store, campaign, consent, point movements and
balances, coupon definitions and entitlements. One table each in cdp_intake; every contract keeps
`attributes` so no column is dropped.
Design: buzz-cdp-workspace/ops/design/intake-v2-pipeline-2026-10-21.md,
        buzz-cdp-workspace/ops/design/dataset-types-2026-11-13.md
"""

from __future__ import annotations

CUSTOMER_FIELDS = frozenset({
    "source_customer_id", "external_id", "first_name", "middle_name", "last_name", "full_name",
    "nickname", "gender", "date_of_birth", "birthday", "age", "email", "phone", "line_user_id",
    "facebook_id", "google_id", "apple_id", "member_code", "customer_type", "status", "language",
    "address_line_1", "address_line_2", "district", "province", "postal_code", "country", "source",
    "source_system", "external_ids", "attributes", "source_created_at", "source_updated_at",
})
TRANSACTION_FIELDS = frozenset({
    "source_transaction_id", "source_customer_id", "transaction_type", "status", "occurred_at",
    "amount", "currency", "discount_amount", "quantity", "channel", "location_id", "items",
    "attributes", "source_updated_at", "original_transaction_id", "line_count", "amount_from",
})
EVENT_FIELDS = frozenset({
    "source_event_id", "source_customer_id", "anonymous_id", "event_name", "event_category",
    "occurred_at", "channel", "object_type", "object_id", "value", "attributes",
})
ASSET_FIELDS = frozenset({
    "source_asset_id", "asset_type", "name", "identifiers", "status", "attributes",
    "source_updated_at",
})
RELATIONSHIP_FIELDS = frozenset({
    "source_relationship_id", "from_type", "from_id", "relation_type", "to_type", "to_id", "role",
    "valid_from", "valid_to", "confidence", "attributes",
})

TRANSACTION_ITEM_FIELDS = frozenset({
    "source_transaction_id", "line_no", "source_customer_id", "occurred_at", "location_id", "sku",
    "product_name", "category", "category_path", "brand", "quantity", "unit_price",
    "discount_amount", "net_amount", "currency", "attributes",
})
PRODUCT_FIELDS = frozenset({
    "sku", "name", "category", "category_path", "brand", "unit_price", "status", "origin",
    "attributes",
})
STORE_FIELDS = frozenset({
    "store_code", "name", "company", "format", "province", "district", "latitude", "longitude",
    "status", "opened_at", "origin", "attributes",
})
CAMPAIGN_FIELDS = frozenset({
    "source_campaign_id", "name", "objective", "channel", "company", "status", "start_at",
    "end_at", "attributes",
})
CONSENT_FIELDS = frozenset({
    "source_customer_id", "company", "purpose", "channel", "status", "captured_at", "source",
    "policy_version", "expires_at", "attributes",
})
POINT_LEDGER_FIELDS = frozenset({
    "source_movement_id", "source_customer_id", "program", "movement_type", "points",
    "occurred_at", "source_transaction_id", "expires_at", "location_id", "attributes",
})
POINT_BALANCE_FIELDS = frozenset({
    "source_customer_id", "program", "as_of", "balance", "baht_value", "expiring_points",
    "expiry_date", "tier", "attributes",
})
COUPON_FIELDS = frozenset({
    "source_coupon_id", "name", "benefit_type", "benefit_value", "min_spend", "valid_from",
    "valid_to", "quota", "source_campaign_id", "conditions", "status", "attributes",
})
COUPON_WALLET_FIELDS = frozenset({
    "source_wallet_id", "source_customer_id", "source_coupon_id", "coupon_code", "status",
    "issued_at", "status_at", "redeemed_at", "expires_at", "source_transaction_id", "location_id",
    "attributes",
})

ENTITY_FIELDS = {
    "CUSTOMER": CUSTOMER_FIELDS,
    "TRANSACTION": TRANSACTION_FIELDS,
    "EVENT": EVENT_FIELDS,
    "ASSET": ASSET_FIELDS,
    "RELATIONSHIP": RELATIONSHIP_FIELDS,
    "TRANSACTION_ITEM": TRANSACTION_ITEM_FIELDS,
    "PRODUCT": PRODUCT_FIELDS,
    "STORE": STORE_FIELDS,
    "CAMPAIGN": CAMPAIGN_FIELDS,
    "CONSENT": CONSENT_FIELDS,
    "POINT_LEDGER": POINT_LEDGER_FIELDS,
    "POINT_BALANCE": POINT_BALANCE_FIELDS,
    "COUPON": COUPON_FIELDS,
    "COUPON_WALLET": COUPON_WALLET_FIELDS,
}
# Each inner tuple means "at least one of".
ENTITY_REQUIRED = {
    "CUSTOMER": (("source_customer_id",),),
    "TRANSACTION": (("source_transaction_id",), ("transaction_type",), ("occurred_at",),
                    ("amount",), ("currency",)),
    "EVENT": (("source_event_id",), ("event_name",), ("occurred_at",),
              ("source_customer_id", "anonymous_id")),
    "ASSET": (("source_asset_id",), ("asset_type",)),
    "RELATIONSHIP": (("from_type",), ("from_id",), ("relation_type",), ("to_type",), ("to_id",)),
    "TRANSACTION_ITEM": (("source_transaction_id",), ("sku", "product_name")),
    "PRODUCT": (("sku",),),
    "STORE": (("store_code",),),
    "CAMPAIGN": (("source_campaign_id",), ("name",)),
    "CONSENT": (("source_customer_id",), ("purpose",), ("status",), ("captured_at",)),
    "POINT_LEDGER": (("source_customer_id",), ("movement_type",), ("points",), ("occurred_at",)),
    "POINT_BALANCE": (("source_customer_id",), ("balance",), ("as_of",)),
    "COUPON": (("source_coupon_id",),),
    "COUPON_WALLET": (("source_customer_id",), ("source_coupon_id",), ("status",)),
}
ENTITY_KEY = {
    "CUSTOMER": "source_customer_id",
    "TRANSACTION": "source_transaction_id",
    "EVENT": "source_event_id",
    "ASSET": "source_asset_id",
    "RELATIONSHIP": "source_relationship_id",  # derived from the edge when not mapped
    "TRANSACTION_ITEM": "source_transaction_id",  # + line_no (derived per bill when not mapped)
    "PRODUCT": "sku",
    "STORE": "store_code",
    "CAMPAIGN": "source_campaign_id",
    "CONSENT": "source_customer_id",  # + company, purpose, channel, captured_at
    "POINT_LEDGER": "source_customer_id",  # + source_movement_id (derived when not mapped)
    "POINT_BALANCE": "source_customer_id",  # + program, as_of
    "COUPON": "source_coupon_id",
    "COUPON_WALLET": "source_customer_id",  # + source_wallet_id (derived when not mapped)
}
# Facts that attach to a person by source_customer_id and never create one.
PERSON_FACTS = frozenset({"TRANSACTION", "EVENT", "CONSENT", "POINT_LEDGER", "POINT_BALANCE",
                          "COUPON_WALLET"})
# Masters: a row from the brand's own list (origin 'master') always beats one seen on bills
# ('observed'); the landing ranks _version by origin.
MASTER_ENTITIES = frozenset({"PRODUCT", "STORE", "CAMPAIGN", "COUPON"})
# Canonical values for fields the reads depend on. Unknown text keeps its raw value in attributes.
CANONICAL_VALUES = {
    ("POINT_LEDGER", "movement_type"): {
        "earn": ("earn", "earned", "accrue", "accrual", "collect", "get", "add", "plus", "สะสม",
                 "ได้รับ", "ได้แต้ม", "รับแต้ม", "เพิ่ม"),
        "burn": ("burn", "burned", "redeem", "redeemed", "redemption", "use", "used", "spend",
                 "แลก", "ใช้", "ใช้แต้ม", "แลกแต้ม", "แลกของรางวัล"),
        "expire": ("expire", "expired", "expiry", "expiration", "หมดอายุ", "แต้มหมดอายุ"),
        "adjust": ("adjust", "adjustment", "manual", "correction", "reverse", "reversal", "refund",
                   "ปรับ", "ปรับปรุง", "คืน", "ยกเลิก"),
    },
    ("COUPON_WALLET", "status"): {
        "issued": ("issued", "issue", "new", "available", "active", "collected", "granted", "ได้รับ",
                   "ใช้ได้", "เก็บแล้ว", "พร้อมใช้"),
        "reserved": ("reserved", "reserve", "hold", "pending", "จอง", "รอใช้"),
        "redeemed": ("redeemed", "redeem", "used", "use", "consumed", "ใช้แล้ว", "แลกแล้ว"),
        "expired": ("expired", "expire", "หมดอายุ"),
        "reversed": ("reversed", "reverse", "refunded", "returned", "คืน", "ยกเลิกการใช้"),
        "released": ("released", "release", "cancelled", "canceled", "void", "timeout", "ยกเลิก",
                     "ปล่อยคืน"),
    },
    ("CONSENT", "status"): {
        "granted": ("granted", "grant", "yes", "y", "true", "1", "agree", "agreed", "accept",
                    "accepted", "opt-in", "opt in", "optin", "consent", "allow", "allowed", "ยินยอม",
                    "ให้ความยินยอม", "อนุญาต", "ตกลง", "ใช่"),
        "withdrawn": ("withdrawn", "withdraw", "no", "n", "false", "0", "revoked", "revoke",
                      "declined", "decline", "reject", "rejected", "opt-out", "opt out", "optout",
                      "deny", "denied", "ไม่ยินยอม", "ถอนความยินยอม", "ยกเลิก", "ไม่อนุญาต", "ไม่"),
    },
}
NODE_TYPES = frozenset({"customer", "asset", "organization"})
JSON_FIELDS = frozenset({"attributes", "external_ids", "items", "identifiers", "conditions"})
