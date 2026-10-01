"""Build only the two changed Python modules from pinned deployed image bytes.

This does not build, push, register a task definition, or alter AWS. Keep the
unmodified installed layers, including the serving package absent from the
local checkout. Never use an unpinned image/tag or the dirty checkout as FROM.
"""
from __future__ import annotations

import hashlib
import io
import json

from pathlib import Path
import subprocess
import sys
import tarfile

REGISTRY = "538783577018.dkr.ecr.ap-southeast-7.amazonaws.com/customer-identity-stitcher"
IMAGES = {
    "stitcher": (
        "sha256:705641cdedf7898fb790084f7d1749c519252ad7d4a4fa7dcdcb8f91af27f01e",
        "/usr/local/lib/python3.12/site-packages/identity_stitcher/service.py",
        "4c6f9c95364cade28e2b51a11452bb107d07bcebc81dbb52dae4ee7ae8073387",
    ),
    "publisher": (
        "sha256:07bedf77951361108326d6e0518a664e4aa5ac02319f187467af145a510b293b",
        "/opt/publication/identity_stitcher/publication.py",
        "1f12d1b11f83b974b70ea5df46dee17bb441bb72ef232f11e5bf7cba615381df",
    ),
}


def replace_once(source: str, old: str, new: str) -> str:
    if source.count(old) != 1:
        raise RuntimeError(f"deployed baseline has drifted: {source.count(old)} occurrences")
    return source.replace(old, new, 1)


def patch_stitcher(source: str) -> str:
    source = replace_once(source, "import splink\n", "import splink\nfrom psycopg2.errors import SerializationFailure\n")
    old = '''            LOGGER.info("Resolution run %s started", run_id)
            stage_started = time.perf_counter()
            result = resolve_identities(conn, config)
            _log_stage("resolve", stage_started, job_id=job_id, run_id=run_id)
            stage_started = time.perf_counter()
            metrics = persist_resolution(
                conn,
                run_id,
                result,
                source_count,
                stitching_job_id=job_id,
            )
'''
    new = '''            LOGGER.info("Resolution run %s started", run_id)
            # create_run committed the intent. Pin matching, global persistence,
            # and capture to one MVCC frame without locking out file ingestion.
            for snapshot_attempt in range(3):
                try:
                    conn.set_session(isolation_level="REPEATABLE READ")
                    snapshot_selected = count_raw_customers(conn, batch_ids) if batch_ids else source_count
                    if snapshot_selected != selected_count:
                        raise RuntimeError("Selected batch changed before global match snapshot")
                    snapshot_source_count = count_raw_customers(conn)
                    stage_started = time.perf_counter()
                    result = resolve_identities(conn, config)
                    _log_stage("resolve", stage_started, job_id=job_id, run_id=run_id)
                    stage_started = time.perf_counter()
                    metrics = persist_resolution(
                        conn,
                        run_id,
                        result,
                        snapshot_source_count,
                        stitching_job_id=job_id,
                    )
                    break
                except SerializationFailure:
                    conn.rollback()
                    if snapshot_attempt == 2:
                        raise
                    LOGGER.warning("Retrying full resolution transaction after serialization failure: %s", run_id)
'''
    return replace_once(source, old, new)


def patch_publisher(source: str) -> str:
    old = '''                          AND b.app_id=e.app_id AND b.source_customer_id=e.user_id
                        ORDER BY b.standardized_record_id LIMIT 2'''
    new = '''                          AND b.app_id=e.app_id AND b.source_customer_id=e.user_id
                          AND b.standardized_record_id::text=e.payload->>'standardized_record_id'
                          AND b.raw_customer_id::text=e.payload->>'raw_customer_id'
                          AND b.customer_entity_id::text=e.payload->>'customer_entity_id'
                          AND b.source_system_id::text=e.payload->>'source_system_id'
                        ORDER BY b.standardized_record_id LIMIT 2'''
    return replace_once(source, old, new)


def installed_bytes(image: str, path: str) -> bytes:
    cid = subprocess.run(["docker", "create", "--entrypoint", "/bin/true", image],
                         capture_output=True, text=True, check=True).stdout.strip()
    try:
        result = subprocess.run(["docker", "cp", f"{cid}:{path}", "-"],
                                capture_output=True, check=True)
        with tarfile.open(fileobj=io.BytesIO(result.stdout)) as archive:
            members = [member for member in archive if member.isfile()]
            if len(members) != 1:
                raise RuntimeError("Expected exactly one installed source file")
            member = archive.extractfile(members[0])
            if member is None:
                raise RuntimeError("Installed source file could not be extracted")
            return member.read()
    finally:
        subprocess.run(["docker", "rm", cid], capture_output=True, check=True)


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit("usage: build_customer_overlap_images.py PRIVATE_OUTPUT_DIR")
    out = Path(sys.argv[1]).expanduser().resolve()
    out.mkdir(mode=0o700, parents=True, exist_ok=True)
    if out.stat().st_mode & 0o077:
        raise RuntimeError("output directory must be private (0700)")
    receipt = {}
    for kind, (digest, path, baseline_sha) in IMAGES.items():
        image = f"{REGISTRY}@{digest}"
        baseline = installed_bytes(image, path)
        if hashlib.sha256(baseline).hexdigest() != baseline_sha:
            raise RuntimeError(f"{kind} installed image source digest mismatch")
        candidate = (patch_stitcher if kind == "stitcher" else patch_publisher)(baseline.decode())
        target = out / f"{kind}-context"
        target.mkdir(mode=0o700, exist_ok=True)
        module = target / Path(path).name
        module.write_text(candidate)
        module.chmod(0o600)
        dockerfile = target / "Dockerfile"
        dockerfile.write_text(f"FROM --platform=linux/amd64 {image}\n"
                              f"COPY --chown=app:app {module.name} {path}\n")
        dockerfile.chmod(0o600)
        receipt[kind] = {"baseline_image": image, "baseline_module_sha256": baseline_sha,
                         "candidate_module_sha256": hashlib.sha256(module.read_bytes()).hexdigest(),
                         "container_module_path": path, "context": str(target)}
    report = out / "sources.json"
    report.write_text(json.dumps(receipt, sort_keys=True, indent=2) + "\n")
    report.chmod(0o600)
    print(json.dumps(receipt, sort_keys=True))


if __name__ == "__main__":
    main()
