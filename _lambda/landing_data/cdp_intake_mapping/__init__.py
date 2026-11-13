"""VENDORED from buzz-cdp-workspace/packages/cdp-intake-mapping (edit there, then copy).

Shared intake mapping engine (mapping v2 projections) for upload landing and connector."""

from .contracts import (
    CANONICAL_VALUES,
    ENTITY_FIELDS,
    ENTITY_KEY,
    ENTITY_REQUIRED,
    MASTER_ENTITIES,
    PERSON_FACTS,
)
from .engine import (
    MappingError,
    ProjectionResult,
    Projector,
    finish_record,
    normalize_headers,
    referenced_paths,
    required_groups,
    validate_mapping,
)
from .transforms import TransformError, Transforms

__all__ = [
    "CANONICAL_VALUES", "ENTITY_FIELDS", "ENTITY_KEY", "ENTITY_REQUIRED", "MASTER_ENTITIES",
    "MappingError", "PERSON_FACTS", "ProjectionResult", "Projector", "TransformError",
    "Transforms", "finish_record", "normalize_headers", "referenced_paths", "required_groups",
    "validate_mapping",
]
