from __future__ import annotations

import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import boto3


def _required(values: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = values.get(key)
        if value is not None and str(value).strip():
            return str(value)
    raise ValueError(f"Database secret is missing one of: {', '.join(keys)}")


@dataclass(frozen=True)
class DatabaseConfig:
    host: str
    port: int
    dbname: str
    user: str
    password: str
    sslmode: str = "require"

    @classmethod
    def from_mapping(cls, values: Mapping[str, Any], *, sslmode: str = "require") -> DatabaseConfig:
        return cls(
            host=_required(values, "host", "hostname"),
            port=int(values.get("port", 5432)),
            dbname=_required(values, "dbname", "database", "DB_NAME"),
            user=_required(values, "username", "user", "DB_USER"),
            password=_required(values, "password", "DB_PASSWORD"),
            sslmode=str(values.get("sslmode", sslmode)),
        )


@dataclass(frozen=True)
class AppConfig:
    database: DatabaseConfig
    default_phone_region: str = "TH"
    match_threshold: float = 0.85
    max_random_training_pairs: int = 1_000_000
    max_em_training_pairs: int = 1_000_000
    max_blocking_group_size: int = 250
    max_candidate_pairs: int = 5_000_000
    max_accepted_pairs: int = 2_000_000
    duckdb_memory_limit: str = "4GB"
    duckdb_threads: int = 2
    pii_hash_key: str | None = None
    normalization_version: str = "v1"
    run_mode: str = "once"
    poll_interval_seconds: int = 300
    stitching_lock_wait_seconds: int = 900
    stitching_lock_poll_seconds: int = 5
    log_level: str = "INFO"


def load_database_config(env: Mapping[str, str] | None = None) -> DatabaseConfig:
    env = os.environ if env is None else env
    secret_name = env.get("DB_SECRET_NAME", "").strip()
    if secret_name:
        region = env.get("AWS_REGION") or env.get("AWS_DEFAULT_REGION")
        client = boto3.client("secretsmanager", region_name=region)
        response = client.get_secret_value(SecretId=secret_name)
        if "SecretString" not in response:
            raise ValueError("DB secret must contain a JSON SecretString")
        payload = json.loads(response["SecretString"])
        if not isinstance(payload, dict):
            raise ValueError("DB secret JSON must be an object")
        return DatabaseConfig.from_mapping(
            payload,
            sslmode=env.get("DB_SSLMODE", "require"),
        )

    local = {
        "host": env.get("DB_HOST", "127.0.0.1"),
        "port": env.get("DB_PORT", "5432"),
        "dbname": env.get("DB_NAME", ""),
        "username": env.get("DB_USER", ""),
        "password": env.get("DB_PASSWORD", ""),
        "sslmode": env.get("DB_SSLMODE", "prefer"),
    }
    return DatabaseConfig.from_mapping(local, sslmode="prefer")


def load_config(env: Mapping[str, str] | None = None) -> AppConfig:
    env = os.environ if env is None else env
    run_mode = env.get("RUN_MODE", "once").lower().strip()
    if run_mode not in {"once", "daemon"}:
        raise ValueError("RUN_MODE must be 'once' or 'daemon'")
    threshold = float(env.get("MATCH_THRESHOLD", "0.85"))
    if not 0 < threshold <= 1:
        raise ValueError("MATCH_THRESHOLD must be greater than 0 and at most 1")
    lock_wait_seconds = int(env.get("STITCHING_LOCK_WAIT_SECONDS", "900"))
    lock_poll_seconds = int(env.get("STITCHING_LOCK_POLL_SECONDS", "5"))
    if lock_wait_seconds <= 0:
        raise ValueError("STITCHING_LOCK_WAIT_SECONDS must be positive")
    if lock_poll_seconds <= 0 or lock_poll_seconds > lock_wait_seconds:
        raise ValueError(
            "STITCHING_LOCK_POLL_SECONDS must be positive and no greater "
            "than STITCHING_LOCK_WAIT_SECONDS"
        )
    max_blocking_group_size = int(env.get("MAX_BLOCKING_GROUP_SIZE", "250"))
    max_em_training_pairs = int(env.get("MAX_EM_TRAINING_PAIRS", "1000000"))
    max_candidate_pairs = int(env.get("MAX_CANDIDATE_PAIRS", "5000000"))
    max_accepted_pairs = int(env.get("MAX_ACCEPTED_PAIRS", "2000000"))
    duckdb_memory_limit = env.get("DUCKDB_MEMORY_LIMIT", "4GB").strip().upper()
    duckdb_threads = int(env.get("DUCKDB_THREADS", "2"))
    if max_blocking_group_size < 2:
        raise ValueError("MAX_BLOCKING_GROUP_SIZE must be at least 2")
    if max_em_training_pairs <= 0:
        raise ValueError("MAX_EM_TRAINING_PAIRS must be positive")
    if max_candidate_pairs <= 0:
        raise ValueError("MAX_CANDIDATE_PAIRS must be positive")
    if max_accepted_pairs <= 0 or max_accepted_pairs > max_candidate_pairs:
        raise ValueError(
            "MAX_ACCEPTED_PAIRS must be positive and no greater than MAX_CANDIDATE_PAIRS"
        )
    if not re.fullmatch(r"[1-9][0-9]*(?:\.[0-9]+)?(?:MB|GB)", duckdb_memory_limit):
        raise ValueError("DUCKDB_MEMORY_LIMIT must be a positive MB or GB value")
    if duckdb_threads <= 0:
        raise ValueError("DUCKDB_THREADS must be positive")
    return AppConfig(
        database=load_database_config(env),
        default_phone_region=env.get("DEFAULT_PHONE_REGION", "TH").upper(),
        match_threshold=threshold,
        max_random_training_pairs=int(env.get("MAX_RANDOM_TRAINING_PAIRS", "1000000")),
        max_em_training_pairs=max_em_training_pairs,
        max_blocking_group_size=max_blocking_group_size,
        max_candidate_pairs=max_candidate_pairs,
        max_accepted_pairs=max_accepted_pairs,
        duckdb_memory_limit=duckdb_memory_limit,
        duckdb_threads=duckdb_threads,
        pii_hash_key=env.get("PII_HASH_KEY") or None,
        normalization_version=env.get("NORMALIZATION_VERSION", "v1"),
        run_mode=run_mode,
        poll_interval_seconds=int(env.get("POLL_INTERVAL_SECONDS", "300")),
        stitching_lock_wait_seconds=lock_wait_seconds,
        stitching_lock_poll_seconds=lock_poll_seconds,
        log_level=env.get("LOG_LEVEL", "INFO").upper(),
    )
