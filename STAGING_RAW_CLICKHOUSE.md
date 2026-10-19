# Retired staging customer profile mirror

The manual `raw-clickhouse-mirror` was stopped on 2026-10-05. Its direct profile
projection has been replaced by the production worker pipeline. Do not restart
its Compose service alongside the identity/publication workers.

See [STAGING_RUNTIME_PARITY.md](STAGING_RUNTIME_PARITY.md) for the current runtime,
schema contract, deployment configuration, and verification evidence. Historical
staging tables and delivery ledgers are retained.
