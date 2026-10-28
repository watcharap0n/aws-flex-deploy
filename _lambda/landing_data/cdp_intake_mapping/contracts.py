"""Generic intake contracts (v2): 3 core types + 2 graph types.

CUSTOMER / TRANSACTION / EVENT mirror journey-builder-api
`app/modules/connectors/entities.py` (keep both in sync). ASSET and RELATIONSHIP add
the people-to-things and people-to-people layer (households, shared vehicles, ...).
Design: buzz-cdp-workspace/ops/design/intake-v2-pipeline-2026-10-21.md
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
    "attributes", "source_updated_at",
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

ENTITY_FIELDS = {
    "CUSTOMER": CUSTOMER_FIELDS,
    "TRANSACTION": TRANSACTION_FIELDS,
    "EVENT": EVENT_FIELDS,
    "ASSET": ASSET_FIELDS,
    "RELATIONSHIP": RELATIONSHIP_FIELDS,
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
}
ENTITY_KEY = {
    "CUSTOMER": "source_customer_id",
    "TRANSACTION": "source_transaction_id",
    "EVENT": "source_event_id",
    "ASSET": "source_asset_id",
    "RELATIONSHIP": "source_relationship_id",  # derived from the edge when not mapped
}
NODE_TYPES = frozenset({"customer", "asset", "organization"})
JSON_FIELDS = frozenset({"attributes", "external_ids", "items", "identifiers"})
