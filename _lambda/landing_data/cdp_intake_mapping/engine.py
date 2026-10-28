"""Mapping v2: one source row -> canonical records of several types (projections).

mapping = {
  "version": 2,
  "derived_keys": {name: {"first_of": [{"id_type", "source_path", "transforms"}...]}},
  "projections": [{"name", "target_entity", "when_present": [...], "fields": [...]}],
}
Each field has `target_field` and exactly one value source:
  source_path | constant | derived_key | template ("vehicle:{VIN}") | pack | coalesce
- pack: list of items -> JSON object; an item is a path,
  {"path", "as", "transforms"} or {"as", "constant"}
- coalesce: list of value specs; the first non-empty one wins
plus optional `transforms` (applied to source_path / template parts).
Projection conditions: `when_present` (all refs present) and `when_any_present` (at least one);
a ref is a source path or a derived key name.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from typing import Any

from .contracts import ENTITY_FIELDS, ENTITY_KEY, ENTITY_REQUIRED, JSON_FIELDS, NODE_TYPES
from .transforms import TransformError, Transforms, clean

VALUE_SOURCES = ("source_path", "constant", "derived_key", "template", "pack", "coalesce")
EDGE_FIELDS = ("from_type", "from_id", "relation_type", "to_type", "to_id")
_PLACEHOLDER = re.compile(r"\{([^{}]+)\}")


def normalize_header(name: Any) -> str:
    return re.sub(r"\s+", " ", str(name if name is not None else "")).strip()


def normalize_headers(headers: list[Any]) -> list[str]:
    """Collapse whitespace; repeated names get ' #2', ' #3' ... so each column stays addressable."""
    seen: dict[str, int] = {}
    out = []
    for raw in headers:
        name = normalize_header(raw)
        seen[name] = seen.get(name, 0) + 1
        out.append(name if seen[name] == 1 else f"{name} #{seen[name]}")
    return out


class MappingError(ValueError):
    pass


def _validate_pack(where: str, items: Any, names: frozenset[str]) -> list[str]:
    errors = []
    for item in items:
        if isinstance(item, dict):
            one_source = (item.get("path") is None) != (item.get("constant") is None)
            if not item.get("as") or not one_source:
                errors.append(f"{where}: pack item needs 'as' and one of path/constant")
            bad = set(item.get("transforms", [])) - names
            if bad:
                errors.append(f"{where}: unknown transforms {sorted(bad)}")
        elif not isinstance(item, str):
            errors.append(f"{where}: pack item must be a path or an object")
    return errors


def _validate_field(where: str, spec: dict, entity: str, keys: dict,
                    names: frozenset[str]) -> list[str]:
    errors = []
    target = spec.get("target_field")
    if len([s for s in VALUE_SOURCES if spec.get(s) is not None]) != 1:
        errors.append(f"{where}: needs exactly one of {', '.join(VALUE_SOURCES)}")
    if spec.get("derived_key") is not None and spec["derived_key"] not in keys:
        errors.append(f"{where}: unknown derived_key {spec['derived_key']}")
    if target == ENTITY_KEY[entity] and spec.get("constant") is not None:
        errors.append(f"{where}: the key cannot be a constant")
    if spec.get("pack") is not None:
        if target not in JSON_FIELDS:
            errors.append(f"{where}: pack only fills JSON fields")
        errors += _validate_pack(where, spec["pack"], names)
    for sub in spec.get("coalesce") or []:
        subs = [s for s in VALUE_SOURCES if sub.get(s) is not None]
        if len(subs) != 1 or "coalesce" in subs:
            errors.append(f"{where}: each coalesce option needs one value source")
        if sub.get("derived_key") is not None and sub["derived_key"] not in keys:
            errors.append(f"{where}: unknown derived_key {sub['derived_key']}")
        bad_sub = set(sub.get("transforms", [])) - names
        if bad_sub:
            errors.append(f"{where}: unknown transforms {sorted(bad_sub)}")
    bad = set(spec.get("transforms", [])) - names
    if bad:
        errors.append(f"{where}: unknown transforms {sorted(bad)}")
    return errors


def validate_mapping(mapping: dict, transforms: Transforms | None = None) -> list[str]:
    """Static checks; the API runs the same rules before a mapping can be published."""
    errors: list[str] = []
    names = (transforms or Transforms()).names
    if mapping.get("version") != 2:
        errors.append("mapping version must be 2")
    keys = mapping.get("derived_keys") or {}
    for key_name, spec in keys.items():
        candidates = spec.get("first_of") or []
        if not candidates:
            errors.append(f"derived key {key_name} has no candidates")
        for cand in candidates:
            if not cand.get("id_type") or not cand.get("source_path"):
                errors.append(f"derived key {key_name} candidate needs id_type and source_path")
            bad = set(cand.get("transforms", [])) - names
            if bad:
                errors.append(f"derived key {key_name}: unknown transforms {sorted(bad)}")
    projections = mapping.get("projections") or []
    if not projections:
        errors.append("mapping needs at least one projection")
    seen_names: set[str] = set()
    for proj in projections:
        pname, entity = proj.get("name") or "?", proj.get("target_entity")
        if pname in seen_names:
            errors.append(f"projection name {pname} is repeated")
        seen_names.add(pname)
        if entity not in ENTITY_FIELDS:
            errors.append(f"{pname}: unsupported target_entity {entity}")
            continue
        targets = []
        for spec in proj.get("fields") or []:
            targets.append(spec.get("target_field"))
            errors += _validate_field(f"{pname}.{spec.get('target_field')}", spec, entity, keys,
                                      names)
        if len(set(targets)) != len(targets):
            errors.append(f"{pname}: target_field values must be unique")
        unsupported = sorted(set(targets) - ENTITY_FIELDS[entity])
        if unsupported:
            errors.append(f"{pname}: unsupported {entity.lower()} fields {unsupported}")
        for group in ENTITY_REQUIRED[entity]:
            if not set(targets) & set(group):
                errors.append(f"{pname}: {entity.lower()} requires {' or '.join(group)}")
    return errors


def _spec_paths(spec: dict) -> set[str]:
    paths: set[str] = set()
    if spec.get("source_path") is not None:
        paths.add(spec["source_path"])
    if spec.get("template") is not None:
        paths.update(_PLACEHOLDER.findall(spec["template"]))
    for item in spec.get("pack") or []:
        if isinstance(item, str):
            paths.add(item)
        elif isinstance(item, dict) and item.get("path") is not None:
            paths.add(item["path"])
    for sub in spec.get("coalesce") or []:
        paths |= _spec_paths(sub)
    return paths


def referenced_paths(mapping: dict) -> set[str]:
    """Every source column a mapping reads (to check against a schema snapshot)."""
    keys = mapping.get("derived_keys") or {}
    paths = {c["source_path"] for spec in keys.values() for c in spec.get("first_of") or []
             if c.get("source_path")}
    for proj in mapping.get("projections") or []:
        for ref in (proj.get("when_present") or []) + (proj.get("when_any_present") or []):
            if ref not in keys:
                paths.add(ref)
        for spec in proj.get("fields") or []:
            paths |= _spec_paths(spec)
    return paths


@dataclass
class ProjectionResult:
    records: dict[str, list[dict]] = field(default_factory=dict)
    rejects: list[dict] = field(default_factory=list)
    skipped: dict[str, int] = field(default_factory=dict)
    # Optional values that could not be read: dropped (the record is kept), listed here.
    warnings: list[dict] = field(default_factory=list)

    def merge(self, other: ProjectionResult) -> None:
        for entity, recs in other.records.items():
            self.records.setdefault(entity, []).extend(recs)
        self.rejects.extend(other.rejects)
        self.warnings.extend(other.warnings)
        for name, count in other.skipped.items():
            self.skipped[name] = self.skipped.get(name, 0) + count


REQUIRED_FIELDS = {entity: frozenset(f for group in groups for f in group) | {"source_customer_id"}
                   for entity, groups in ENTITY_REQUIRED.items()}


class Projector:
    def __init__(self, mapping: dict, transforms: Transforms | None = None):
        self.transforms = transforms or Transforms()
        errors = validate_mapping(mapping, self.transforms)
        if errors:
            raise MappingError("; ".join(errors))
        self.mapping = mapping
        self.keys = mapping.get("derived_keys") or {}

    # -- value resolution ------------------------------------------------------------------
    def _source(self, row: dict, path: str, transforms: list[str]) -> Any:
        return self.transforms.apply(clean(row.get(path)), transforms)

    def _derived(self, row: dict, key_name: str, cache: dict) -> str | None:
        if key_name not in cache:
            cache[key_name] = None
            for cand in self.keys[key_name]["first_of"]:
                try:
                    value = self._source(row, cand["source_path"], cand.get("transforms", []))
                except TransformError:
                    value = None
                if value not in (None, ""):
                    cache[key_name] = f"{cand['id_type']}:{value}"
                    break
        return cache[key_name]

    def _pack(self, row: dict, items: list, dropped: list[str] | None = None) -> dict | None:
        packed = {}
        for item in items:
            if isinstance(item, str):
                name, value = item, clean(row.get(item))
            elif item.get("constant") is not None:
                name, value = item["as"], item["constant"]
            else:
                name = item["as"]
                try:
                    value = self._source(row, item["path"], item.get("transforms", []))
                except TransformError as exc:
                    # One unreadable extra value drops that value only, not the pack or record.
                    if dropped is None:
                        raise
                    dropped.append(f"{name}: {exc}")
                    continue
            if value is not None:
                plain = isinstance(value, (str, int, float, bool))
                packed[name] = value if plain else str(value)
        return packed or None

    def _value(self, row: dict, spec: dict, cache: dict, dropped: list[str] | None = None) -> Any:
        if spec.get("constant") is not None:
            return spec["constant"]
        if spec.get("derived_key") is not None:
            return self._derived(row, spec["derived_key"], cache)
        if spec.get("coalesce") is not None:
            for sub in spec["coalesce"]:
                try:
                    value = self._value(row, sub, cache)
                except TransformError:
                    value = None
                if value not in (None, ""):
                    return value
            return None
        if spec.get("pack") is not None:
            return self._pack(row, spec["pack"], dropped)
        if spec.get("template") is not None:
            parts = {}
            for name in _PLACEHOLDER.findall(spec["template"]):
                value = self._source(row, name, spec.get("transforms", []))
                if value in (None, ""):
                    return None
                parts[name] = str(value)
            return _PLACEHOLDER.sub(lambda m: parts[m.group(1)], spec["template"])
        return self._source(row, spec["source_path"], spec.get("transforms", []))

    def _present(self, row: dict, ref: str, cache: dict) -> bool:
        if ref in self.keys:
            return self._derived(row, ref, cache) is not None
        return clean(row.get(ref)) is not None

    def _wanted(self, proj: dict, row: dict, cache: dict) -> bool:
        when_all = proj.get("when_present") or []
        when_any = proj.get("when_any_present") or []
        if when_all and not all(self._present(row, ref, cache) for ref in when_all):
            return False
        return not when_any or any(self._present(row, ref, cache) for ref in when_any)

    # -- projection --------------------------------------------------------------------------
    def project(self, row: dict, *, row_number: int | None = None) -> ProjectionResult:
        result: ProjectionResult = ProjectionResult()
        cache: dict[str, str | None] = {}
        for proj in self.mapping["projections"]:
            name, entity = proj["name"], proj["target_entity"]
            if not self._wanted(proj, row, cache):
                result.skipped[name] = result.skipped.get(name, 0) + 1
                continue
            record: dict[str, Any] = {}
            problems: list[str] = []
            dropped: list[str] = []
            required = REQUIRED_FIELDS[entity]
            for spec in proj["fields"]:
                target = spec["target_field"]
                try:
                    value = self._value(row, spec, cache, dropped)
                except TransformError as exc:
                    # Required fields and identifiers reject the record; any other unreadable
                    # value is dropped and reported, so one bad optional cell never loses a row.
                    if target in required:
                        problems.append(f"{target}: {exc}")
                    else:
                        dropped.append(f"{target}: {exc}")
                    value = None
                if value is not None:
                    record[spec["target_field"]] = value
            if entity == "RELATIONSHIP":
                for side in ("from_type", "to_type"):
                    if record.get(side) is not None and record[side] not in NODE_TYPES:
                        problems.append(f"{side} must be one of {sorted(NODE_TYPES)}")
                if "source_relationship_id" not in record and all(
                        record.get(k) for k in EDGE_FIELDS):
                    edge = "|".join(str(record[k]) for k in EDGE_FIELDS)
                    digest = hashlib.sha256(edge.encode()).hexdigest()
                    record["source_relationship_id"] = digest[:32]
            missing = [" or ".join(group) for group in ENTITY_REQUIRED[entity]
                       if not any(record.get(k) not in (None, "") for k in group)]
            if missing or problems:
                result.rejects.append({"projection": name, "target_entity": entity,
                                       "row_number": row_number,
                                       "reasons": problems + [f"missing {m}" for m in missing]})
                continue
            if dropped:
                result.warnings.append({"projection": name, "target_entity": entity,
                                        "row_number": row_number, "dropped": dropped})
            for json_field in JSON_FIELDS & record.keys():
                if not isinstance(record[json_field], str):
                    record[json_field] = json.dumps(record[json_field], ensure_ascii=False,
                                                    sort_keys=True)
            record["_projection"] = name
            result.records.setdefault(entity, []).append(record)
        return result

    def project_rows(self, rows: list[dict]) -> ProjectionResult:
        total = ProjectionResult()
        for i, row in enumerate(rows, start=1):
            total.merge(self.project(row, row_number=i))
        return total
