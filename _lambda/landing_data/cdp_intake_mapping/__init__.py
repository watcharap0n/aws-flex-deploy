"""VENDORED from buzz-cdp-workspace/packages/cdp-intake-mapping (edit there, then copy).

Shared intake mapping engine (mapping v2 projections) for upload landing and connector."""

from .contracts import ENTITY_FIELDS, ENTITY_KEY, ENTITY_REQUIRED
from .engine import (
    MappingError,
    ProjectionResult,
    Projector,
    normalize_headers,
    referenced_paths,
    validate_mapping,
)
from .transforms import TransformError, Transforms

__all__ = [
    "ENTITY_FIELDS", "ENTITY_KEY", "ENTITY_REQUIRED", "MappingError", "ProjectionResult",
    "Projector", "TransformError", "Transforms", "normalize_headers", "referenced_paths",
    "validate_mapping",
]
