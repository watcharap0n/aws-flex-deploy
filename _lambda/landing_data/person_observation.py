"""Additive evidence for Related people. Never emits a CUSTOMER or changes its projection."""
from __future__ import annotations
import hashlib
import json
import logging
from typing import Any

LOG = logging.getLogger(__name__)


def observations(projector: Any, row: dict, result: Any, lineage: dict, row_number: int):
    # Re-evaluate mapped customer values even when the customer key/condition is absent.
    # This is a separate read model, not an alternative customer validation path.
    facts = [{"entity": e, **r} for e, records in result.records.items()
             if e != "CUSTOMER" for r in records]
    for projection in projector.mapping["projections"]:
        if projection["target_entity"] != "CUSTOMER":
            continue
        fields = {}
        for spec in projection["fields"]:
            try:
                value = projector._value(row, spec, {}, [])
                if value is not None:
                    fields[spec["target_field"]] = value
            except (ValueError, TypeError):
                continue
        name = fields.get("full_name") or " ".join(str(fields.get(k) or "") for k in
                                                   ("first_name", "middle_name", "last_name")).strip()
        if not name:
            continue
        fields["full_name"] = name
        external = fields.get("external_ids") or {}
        if isinstance(external, str):
            try:
                external = json.loads(external)
            except ValueError:
                external = {}
        # Strong personal identifiers are only retained as tenant-scoped conflict tokens.
        tokens = {}
        for k, v in external.items():
            if v is not None and str(v).strip():
                tokens[k] = projector.transforms.apply(str(v), ["hmac"])
        fields["external_ids"] = tokens
        payload = {"customer": fields, "facts": facts}
        serialized = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
        fingerprint = hashlib.sha256((projection["name"] + "\0" + serialized).encode()).hexdigest()
        yield {**lineage, "row_number": row_number, "projection": projection["name"],
               "fingerprint": fingerprint, "payload": serialized}


class ObservationWriter:
    """Best-effort sidecar, with a completion marker. A failed write never changes core statuses."""
    def __init__(self, sink, lineage, object_key, enabled):
        self.sink, self.lineage, self.object_key = sink, lineage, object_key
        self.enabled, self.failed, self.count = enabled, False, 0
        self.buffer = bytearray()

    def add(self, projector, row, result, row_number):
        if not self.enabled or self.failed:
            return
        try:
            for record in observations(projector, row, result, {**self.lineage, "object_key": self.object_key}, row_number):
                self.buffer += (json.dumps(record, ensure_ascii=False, default=str) + "\n").encode()
                self.count += 1
            if len(self.buffer) >= 4 * 1024 * 1024:
                self.flush()
        except Exception:
            self.failed = True
            LOG.exception("related_observation_capture_failed batch=%s", self.lineage["batch_ref"])

    def flush(self):
        if self.buffer:
            self.sink.insert_ndjson("person_observation", bytes(self.buffer))
            self.buffer.clear()

    def close(self):
        if not self.enabled:
            return
        try:
            if not self.failed:
                self.flush()
                marker = {k: self.lineage[k] for k in ("app_id", "dataset_id", "batch_ref", "_version")}
                marker.update(object_key=self.object_key, rows=self.count)
                self.sink.insert_ndjson("person_observation_batch", (json.dumps(marker) + "\n").encode())
                LOG.info("related_observation_capture_completed batch=%s rows=%s", marker["batch_ref"], self.count)
        except Exception:
            self.failed = True
            LOG.exception("related_observation_publish_failed batch=%s", self.lineage["batch_ref"])
