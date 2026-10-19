"""Local-only PostgreSQL connector app lineage; no AWS or shared database writes."""

from __future__ import annotations

import os
from pathlib import Path
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql

ROOT = Path(__file__).resolve().parents[2]
ADMIN = "host=127.0.0.1 port=55441 user=postgres dbname=postgres connect_timeout=3"


@pytest.fixture
def db():
    if os.getenv("CDP_CONNECTOR_APP_PG_TESTS") != "1":
        pytest.skip("set CDP_CONNECTOR_APP_PG_TESTS=1 for disposable local PostgreSQL")
    name = "cdp_connector_app_" + uuid4().hex
    api_role, worker_role = "ca_" + uuid4().hex, "cw_" + uuid4().hex
    with psycopg.connect(ADMIN, autocommit=True) as admin:
        if admin.execute("SELECT 1 FROM pg_database WHERE datname=%s", (name,)).fetchone():
            pytest.fail("refusing to reuse existing database")
        for role in (api_role, worker_role):
            admin.execute(sql.SQL("CREATE ROLE {} NOLOGIN").format(sql.Identifier(role)))
        admin.execute(sql.SQL("CREATE DATABASE {} TEMPLATE template0 ENCODING 'UTF8'").format(sql.Identifier(name)))
        try:
            with psycopg.connect(ADMIN.replace("dbname=postgres", f"dbname={name}"), autocommit=True) as conn:
                conn.execute("SELECT set_config('cdp.connector_api_role',%s,false)", (api_role,))
                conn.execute("SELECT set_config('cdp.connector_worker_role',%s,false)", (worker_role,))
                conn.execute((ROOT / "tests/integration/fixtures/customer_history_base.sql").read_text())
                conn.execute("""
                    ALTER TABLE control.ingestion_batch ADD COLUMN batch_ref text;
                    ALTER TABLE control.source_landing_batch ADD COLUMN batch_ref text, ADD COLUMN source_code text;
                    CREATE TABLE control.source_system(
                        source_system_id bigint PRIMARY KEY, source_code varchar(50) UNIQUE,
                        source_name text, source_type text);

                    CREATE TABLE control.stitching_job(job_id uuid PRIMARY KEY, ingestion_batch_id uuid);
                    CREATE SCHEMA platform;
                    CREATE SCHEMA connector;
                    CREATE TABLE platform.workspace(
                        workspace_id uuid PRIMARY KEY, app_id text CHECK(app_id ~ '^[0-9]+$'));
                    CREATE TABLE connector.connection(
                        connection_id uuid PRIMARY KEY, workspace_id uuid REFERENCES platform.workspace,
                        source_system_id bigint REFERENCES control.source_system, source_code text);
                    CREATE TABLE connector.operation(
                        operation_id uuid PRIMARY KEY, connection_id uuid REFERENCES connector.connection,
                        status varchar(20) NOT NULL DEFAULT 'FAILED');
                    CREATE TABLE connector.dataset(
                        dataset_id uuid PRIMARY KEY,workspace_id uuid REFERENCES platform.workspace,
                        connection_id uuid REFERENCES connector.connection);
                    CREATE TABLE connector.sync_run(
                        sync_run_id uuid PRIMARY KEY,workspace_id uuid REFERENCES platform.workspace,
                        dataset_id uuid REFERENCES connector.dataset,
                        ingestion_batch_id uuid UNIQUE REFERENCES control.ingestion_batch,
                        status varchar(20) NOT NULL DEFAULT 'QUEUED',
                        landing_batch_id uuid UNIQUE REFERENCES control.source_landing_batch,
                        stitching_job_id uuid UNIQUE REFERENCES control.stitching_job);
                    CREATE FUNCTION raw.set_customer_updated_at() RETURNS trigger LANGUAGE plpgsql AS $$
                    BEGIN NEW.updated_at=now(); RETURN NEW; END $$;
                    CREATE TRIGGER trg_raw_customer_updated_at BEFORE UPDATE ON raw.customer
                    FOR EACH ROW EXECUTE FUNCTION raw.set_customer_updated_at();
                """)
                for file in ("019_customer_typed_history.sql", "022_customer_bulk_ingestion.sql", "026_customer_app_scope.sql"):
                    conn.execute((ROOT / "database/migrations" / file).read_text())
                yield conn
        finally:
            admin.execute(sql.SQL("DROP DATABASE {}").format(sql.Identifier(name)))
            for role in (api_role, worker_role):
                admin.execute(sql.SQL("DROP ROLE {}").format(sql.Identifier(role)))


def _lineage(conn, app: str | None = "00042", *, source_id: int = 11,
             workspace=None):
    workspace = workspace or uuid4()
    connection, dataset, sync, landing, batch, job = (uuid4() for _ in range(6))
    source_code = "dc1-" + connection.hex
    conn.execute("INSERT INTO control.source_system(source_system_id,source_code,source_name,source_type) "
                 "VALUES(%s,%s,'fixture','database_connector')", (source_id, source_code))
    conn.execute("INSERT INTO platform.workspace VALUES(%s,%s)", (workspace, app))
    conn.execute("INSERT INTO connector.connection VALUES(%s,%s,%s,'customers')", (connection, workspace, source_id))
    conn.execute("INSERT INTO connector.dataset VALUES(%s,%s,%s)", (dataset, workspace, connection))
    conn.execute("INSERT INTO control.source_landing_batch(batch_id,source_code,batch_ref) VALUES(%s,%s,%s)",
                 (landing, source_code, 'db-' + sync.hex))
    conn.execute("INSERT INTO control.ingestion_batch(batch_id,landing_batch_id,source_system_id,batch_ref,metadata) "
                 "VALUES(%s,%s,%s,%s,jsonb_build_object('connector_sync_run_id',%s::text))",
                 (batch, landing, source_id, 'db-' + sync.hex, str(sync)))
    conn.execute("INSERT INTO control.stitching_job VALUES(%s,%s)", (job, batch))
    conn.execute("INSERT INTO connector.sync_run(sync_run_id,workspace_id,dataset_id,ingestion_batch_id,"
                 "landing_batch_id,stitching_job_id) VALUES(%s,%s,%s,%s,%s,%s)",
                 (sync, workspace, dataset, batch, landing, job))
    return workspace, batch, sync


def test_two_apps_same_customer_key_do_not_share_raw_identity(db) -> None:
    db.execute((ROOT / "database/migrations/040_connector_app_lineage.sql").read_text())
    first_workspace = uuid4()
    # This UUID deliberately has the same first eight hex characters as the first.
    second_workspace = type(first_workspace)(hex=first_workspace.hex[:8] + "a" * 24)
    assert first_workspace != second_workspace
    first, batch_one, _ = _lineage(db, "00042", workspace=first_workspace)
    second, batch_two, _ = _lineage(db, "42", source_id=12, workspace=second_workspace)
    for workspace, batch, app, source in (
        (first, batch_one, "00042", 11), (second, batch_two, "42", 12)
    ):
        assert db.execute(
            "SELECT control.bind_connector_batch_app_id(%s,%s,%s)", (batch, workspace, app)
        ).fetchone() == (app,)
        inserted = db.execute(
            "INSERT INTO raw.customer_current(batch_id,source_system_id,source_customer_id) "
            "VALUES(%s,%s,'same-key') RETURNING raw_customer_id,app_id", (batch, source)
        ).fetchone()
        assert inserted[1] == app
    identities = db.execute(
        "SELECT raw_customer_id,app_id,source_system_id FROM raw.customer_current "
        "WHERE source_customer_id='same-key' ORDER BY app_id"
    ).fetchall()
    assert len(identities) == 2
    assert len({row[0] for row in identities}) == 2
    assert len({row[2] for row in identities}) == 2
    assert db.execute(
        "SELECT count(DISTINCT app_id) FROM raw.customer WHERE source_customer_id='same-key'"
    ).fetchone() == (2,)


def test_legacy_source_quarantined_before_new_app_bound_sync(db) -> None:
    workspace, old_batch, old_sync = _lineage(db)
    db.execute("UPDATE control.source_system SET source_code='legacy-connector' WHERE source_system_id=11")
    db.execute("UPDATE control.source_landing_batch SET source_code='legacy-connector' "
               "WHERE batch_id=(SELECT landing_batch_id FROM connector.sync_run WHERE sync_run_id=%s)",
               (old_sync,))
    old_batch_source = db.execute("SELECT source_system_id FROM control.ingestion_batch "
                                  "WHERE batch_id=%s", (old_batch,)).fetchone()[0]
    db.execute("UPDATE connector.sync_run SET status='FAILED' WHERE sync_run_id=%s", (old_sync,))
    db.execute((ROOT / "database/migrations/040_connector_app_lineage.sql").read_text())
    connection_id, dataset_id, source_id = db.execute(
        "SELECT c.connection_id,d.dataset_id,c.source_system_id FROM connector.connection c "
        "JOIN connector.dataset d USING(connection_id) WHERE c.workspace_id=%s", (workspace,)
    ).fetchone()
    assert source_id is None  # Old batch keeps its source, but new sync cannot adopt it.
    assert db.execute("SELECT source_system_id FROM control.ingestion_batch WHERE batch_id=%s",
                      (old_batch,)).fetchone() == (old_batch_source,)
    with db.transaction():
        with pytest.raises(psycopg.Error, match="connector.*legacy.*reactivat"):
            db.execute("UPDATE connector.sync_run SET status='QUEUED' WHERE sync_run_id=%s", (old_sync,))
    source_id = 12
    db.execute("INSERT INTO control.source_system(source_system_id,source_code,source_name,source_type) "
               "VALUES(%s,%s,'fresh','database_connector')", (source_id, "dc1-" + connection_id.hex))
    db.execute("UPDATE connector.connection SET source_system_id=%s WHERE connection_id=%s",
               (source_id, connection_id))
    with db.transaction():
        with pytest.raises(psycopg.Error, match="connector.*legacy.*reactivat"):
            db.execute("UPDATE connector.sync_run SET status='QUEUED' WHERE sync_run_id=%s", (old_sync,))
    new_sync, landing, batch, job = (uuid4() for _ in range(4))
    new_ref = "db-" + new_sync.hex
    db.execute("INSERT INTO control.source_landing_batch(batch_id,source_code,batch_ref) "
               "VALUES(%s,%s,%s)", (landing, "dc1-" + connection_id.hex, new_ref))
    db.execute("INSERT INTO control.ingestion_batch(batch_id,landing_batch_id,source_system_id,batch_ref,metadata) "
               "VALUES(%s,%s,%s,%s,jsonb_build_object('connector_sync_run_id',%s::text))",
               (batch, landing, source_id, new_ref, str(new_sync)))
    db.execute("INSERT INTO control.stitching_job VALUES(%s,%s)", (job, batch))
    db.execute("INSERT INTO connector.sync_run(sync_run_id,workspace_id,dataset_id,ingestion_batch_id,"
               "landing_batch_id,stitching_job_id) VALUES(%s,%s,%s,%s,%s,%s)",
               (new_sync, workspace, dataset_id, batch, landing, job))
    assert db.execute("SELECT control.bind_connector_batch_app_id(%s,%s,'00042')",
                      (batch, workspace)).fetchone() == ("00042",)
    assert db.execute("INSERT INTO raw.customer_current(batch_id,source_system_id,source_customer_id) "
                      "VALUES(%s,%s,'new-key') RETURNING app_id", (batch, source_id)).fetchone() == ("00042",)


def test_old_completed_source_with_customer_history_is_not_reused(db) -> None:
    workspace, old_batch, old_sync = _lineage(db)
    db.execute("UPDATE control.source_system SET source_code='legacy-connector' WHERE source_system_id=11")
    db.execute("UPDATE control.source_landing_batch SET source_code='legacy-connector' "
               "WHERE batch_id=(SELECT landing_batch_id FROM connector.sync_run WHERE sync_run_id=%s)",
               (old_sync,))
    old_id = db.execute("INSERT INTO raw.customer_current(batch_id,source_system_id,source_customer_id) "
                        "VALUES(%s,11,'same-key') RETURNING raw_customer_id", (old_batch,)).fetchone()[0]
    db.execute("UPDATE connector.sync_run SET status='FAILED' WHERE sync_run_id=%s", (old_sync,))
    db.execute((ROOT / "database/migrations/040_connector_app_lineage.sql").read_text())
    connection_id, dataset_id = db.execute("SELECT c.connection_id,d.dataset_id FROM connector.connection c "
                                           "JOIN connector.dataset d USING(connection_id) WHERE c.workspace_id=%s",
                                           (workspace,)).fetchone()
    db.execute("INSERT INTO control.source_system(source_system_id,source_code,source_name,source_type) "
               "VALUES(12,%s,'fresh','database_connector')", ("dc1-" + connection_id.hex,))
    db.execute("UPDATE connector.connection SET source_system_id=12 WHERE connection_id=%s", (connection_id,))
    sync, landing, batch, job = (uuid4() for _ in range(4))
    ref = 'db-' + sync.hex
    db.execute("INSERT INTO control.source_landing_batch(batch_id,source_code,batch_ref) VALUES(%s,%s,%s)",
               (landing, "dc1-" + connection_id.hex, ref))
    db.execute("INSERT INTO control.ingestion_batch(batch_id,landing_batch_id,source_system_id,batch_ref,metadata) "
               "VALUES(%s,%s,12,%s,jsonb_build_object('connector_sync_run_id',%s::text))",
               (batch, landing, ref, str(sync)))
    db.execute("INSERT INTO control.stitching_job VALUES(%s,%s)", (job, batch))
    db.execute("INSERT INTO connector.sync_run(sync_run_id,workspace_id,dataset_id,ingestion_batch_id,"
               "landing_batch_id,stitching_job_id) VALUES(%s,%s,%s,%s,%s,%s)",
               (sync, workspace, dataset_id, batch, landing, job))
    db.execute("SELECT control.bind_connector_batch_app_id(%s,%s,'00042')", (batch, workspace))
    fresh_id = db.execute("INSERT INTO raw.customer_current(batch_id,source_system_id,source_customer_id) "
                          "VALUES(%s,12,'same-key') RETURNING raw_customer_id", (batch,)).fetchone()[0]
    assert fresh_id != old_id
    assert db.execute("SELECT source_system_id FROM raw.customer WHERE raw_customer_id=%s",
                      (old_id,)).fetchone() == (11,)


def test_active_legacy_sync_blocks_source_quarantine(db) -> None:
    _, batch, sync = _lineage(db)
    db.execute("UPDATE control.source_system SET source_code='legacy-connector' WHERE source_system_id=11")
    with db.transaction():
        with pytest.raises(psycopg.Error, match="old active sync"):
            db.execute((ROOT / "database/migrations/040_connector_app_lineage.sql").read_text())
    assert db.execute("SELECT source_system_id FROM control.ingestion_batch WHERE batch_id=%s",
                      (batch,)).fetchone() == (11,)
    assert db.execute("SELECT status FROM connector.sync_run WHERE sync_run_id=%s",
                      (sync,)).fetchone() == ("QUEUED",)


def test_dispatched_old_operation_blocks_source_quarantine(db) -> None:
    workspace, _, sync = _lineage(db)
    db.execute("UPDATE control.source_system SET source_code='legacy-connector' WHERE source_system_id=11")
    db.execute("UPDATE connector.sync_run SET status='FAILED' WHERE sync_run_id=%s", (sync,))
    connection_id = db.execute("SELECT connection_id FROM connector.connection WHERE workspace_id=%s",
                               (workspace,)).fetchone()[0]
    operation_id = uuid4()
    db.execute("INSERT INTO connector.operation(operation_id,connection_id,status) "
               "VALUES(%s,%s,'DISPATCHED')", (operation_id, connection_id))
    with db.transaction():
        with pytest.raises(psycopg.Error, match="old active connector operation"):
            db.execute((ROOT / "database/migrations/040_connector_app_lineage.sql").read_text())
    assert db.execute("SELECT source_system_id FROM connector.connection WHERE connection_id=%s",
                      (connection_id,)).fetchone() == (11,)


def test_existing_source_code_cannot_be_claimed_by_another_connection(db) -> None:
    db.execute((ROOT / "database/migrations/040_connector_app_lineage.sql").read_text())
    _, batch, _ = _lineage(db)
    contender = uuid4()
    db.execute("INSERT INTO platform.workspace VALUES(%s,'42')", (contender,))
    with db.transaction():
        with pytest.raises(psycopg.errors.UniqueViolation, match="uq_connection_source_system"):
            db.execute("INSERT INTO connector.connection VALUES(%s,%s,11,'customers')", (uuid4(), contender))
    assert db.execute("SELECT * FROM control.connector_batch_app_scope(%s)", (batch,)).fetchone() == ("00042", None)


def test_connection_source_uniqueness_is_enforced_by_index(db) -> None:
    db.execute((ROOT / "database/migrations/040_connector_app_lineage.sql").read_text())
    definition = db.execute("SELECT indexdef FROM pg_indexes WHERE schemaname='connector' "
                            "AND indexname='uq_connection_source_system'").fetchone()
    assert definition and "UNIQUE INDEX" in definition[0] and "source_system_id" in definition[0]


def test_new_dataset_can_register_its_source_on_first_sync(db) -> None:
    db.execute((ROOT / "database/migrations/040_connector_app_lineage.sql").read_text())
    workspace, connection, dataset = uuid4(), uuid4(), uuid4()
    db.execute("INSERT INTO platform.workspace VALUES(%s,'00042')", (workspace,))
    db.execute(
        "INSERT INTO connector.connection(connection_id,workspace_id,source_code) "
        "VALUES(%s,%s,'customers')", (connection, workspace)
    )
    db.execute("INSERT INTO connector.dataset VALUES(%s,%s,%s)", (dataset, workspace, connection))
    db.execute(
        "INSERT INTO control.source_system(source_system_id,source_code,source_name,source_type) "
        "VALUES(11,%s,'fixture','database_connector')", ('dc1-' + connection.hex,)
    )
    db.execute("UPDATE connector.connection SET source_system_id=11 WHERE connection_id=%s", (connection,))
    assert db.execute(
        "SELECT source_system_id FROM connector.connection WHERE connection_id=%s", (connection,)
    ).fetchone() == (11,)
    with db.transaction():
        with pytest.raises(psycopg.Error, match="connector ownership is immutable"):
            db.execute("UPDATE connector.connection SET source_system_id=NULL WHERE connection_id=%s", (connection,))


@pytest.mark.parametrize("target,assignment", [
    ("connector.connection", "workspace_id"),
    ("connector.dataset", "workspace_id"),
    ("connector.sync_run", "workspace_id"),
])
def test_registered_connector_cannot_reparent_identity_to_other_app(db, target: str, assignment: str) -> None:
    db.execute((ROOT / "database/migrations/040_connector_app_lineage.sql").read_text())
    workspace, batch, sync = _lineage(db)
    db.execute("SELECT control.bind_connector_batch_app_id(%s,%s,'00042')", (batch, workspace))
    other = uuid4()
    db.execute("INSERT INTO platform.workspace VALUES(%s,'42')", (other,))
    row = db.execute(
        "SELECT s.sync_run_id,d.dataset_id,c.connection_id FROM connector.sync_run s "
        "JOIN connector.dataset d ON d.dataset_id=s.dataset_id "
        "JOIN connector.connection c ON c.connection_id=d.connection_id WHERE s.sync_run_id=%s", (sync,)
    ).fetchone()
    key_column, key = {
        "connector.connection": ("connection_id", row[2]),
        "connector.dataset": ("dataset_id", row[1]),
        "connector.sync_run": ("sync_run_id", row[0]),
    }[target]
    with db.transaction():
        with pytest.raises(psycopg.Error, match="connector.*ownership.*immutable"):
            db.execute(f"UPDATE {target} SET {assignment}=%s WHERE {key_column}=%s", (other, key))
    assert db.execute("SELECT * FROM control.connector_batch_app_scope(%s)", (batch,)).fetchone() == ("00042", None)


def test_existing_database_owner_can_install_without_fictitious_runtime_roles(db) -> None:
    # The staging API and batch task both use the DB owner. This should not
    # grant two unrelated role names and claim those are the runtime principals.
    db.execute("SELECT set_config('cdp.connector_api_role','',false)")
    db.execute("SELECT set_config('cdp.connector_worker_role','',false)")
    db.execute("SELECT set_config('cdp.connector_owner_runtime','verified-owner-only',false)")
    db.execute((ROOT / "database/migrations/040_connector_app_lineage.sql").read_text())
    workspace, batch, _ = _lineage(db)
    assert db.execute("SELECT control.bind_connector_batch_app_id(%s,%s,'00042')",
                      (batch, workspace)).fetchone() == ("00042",)
    assert db.execute("SELECT * FROM control.connector_batch_app_scope(%s)",
                      (batch,)).fetchone() == ("00042", None)
    assert db.execute("SELECT has_function_privilege('public',"
                      "'control.bind_connector_batch_app_id(uuid,uuid,text)','EXECUTE')").fetchone() == (False,)


def test_runtime_functions_are_executable_only_by_configured_roles(db) -> None:
    db.execute((ROOT / "database/migrations/040_connector_app_lineage.sql").read_text())
    workspace, batch, _ = _lineage(db)
    api_role = db.execute("SELECT current_setting('cdp.connector_api_role')").fetchone()[0]
    worker_role = db.execute("SELECT current_setting('cdp.connector_worker_role')").fetchone()[0]
    assert db.execute("SELECT has_function_privilege('public',"
                      "'control.bind_connector_batch_app_id(uuid,uuid,text)','EXECUTE')").fetchone() == (False,)
    assert db.execute("SELECT has_function_privilege(%s,"
                      "'control.bind_connector_batch_app_id(uuid,uuid,text)','EXECUTE')", (api_role,)).fetchone() == (True,)
    assert db.execute("SELECT has_function_privilege(%s,"
                      "'control.connector_batch_app_scope(uuid)','EXECUTE')", (worker_role,)).fetchone() == (True,)
    with db.transaction():
        db.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(worker_role)))
        assert db.execute("SELECT * FROM control.connector_batch_app_scope(%s)",
                          (batch,)).fetchone() == ("00042", None)
    with db.transaction():
        db.execute(sql.SQL("SET LOCAL ROLE {}").format(sql.Identifier(api_role)))
        assert db.execute("SELECT control.bind_connector_batch_app_id(%s,%s,'00042')",
                          (batch, workspace)).fetchone() == ("00042",)


def test_038_refuses_to_install_after_connector_app_binding(db) -> None:
    db.execute((ROOT / "database/migrations/040_connector_app_lineage.sql").read_text())
    with db.transaction():
        with pytest.raises(psycopg.Error, match="connector app lineage"):
            db.execute((ROOT / "database/migrations/038_customer_forward_source_namespace.sql").read_text())


def test_037_refuses_to_install_after_connector_app_binding(db) -> None:
    db.execute((ROOT / "database/migrations/040_connector_app_lineage.sql").read_text())
    db.execute("SET cdp.customer_source_namespace_opt_in='reviewed-file-upload-only'")
    with db.transaction():
        with pytest.raises(psycopg.Error, match="connector app lineage"):
            db.execute((ROOT / "database/migrations/037_customer_app_source_namespace.sql").read_text())


def test_040_refuses_to_install_after_037_or_038_route_markers(db) -> None:
    db.execute("CREATE TABLE control.customer_forward_route(route_id integer)")
    with db.transaction():
        with pytest.raises(psycopg.Error, match="037/038"):
            db.execute((ROOT / "database/migrations/040_connector_app_lineage.sql").read_text())
    assert db.execute("SELECT to_regprocedure('control.bind_connector_batch_app_id(uuid,uuid,text)')").fetchone() == (None,)


def test_connector_binding_derives_literal_app_through_raw_and_staging(db) -> None:
    db.execute((ROOT / "database/migrations/040_connector_app_lineage.sql").read_text())
    workspace, batch, _ = _lineage(db)
    assert db.execute("SELECT app_id FROM control.ingestion_batch WHERE batch_id=%s", (batch,)).fetchone() == (None,)
    assert db.execute("SELECT control.bind_connector_batch_app_id(%s,%s,%s)", (batch, workspace, "00042")).fetchone() == ("00042",)
    assert db.execute("SELECT app_id FROM control.ingestion_batch WHERE batch_id=%s", (batch,)).fetchone() == ("00042",)
    # The legacy status accessor is upload-only (also consumed by backfill and
    # publication); connector provenance has a separate verified accessor.
    assert db.execute("SELECT * FROM control.customer_app_scope_status(%s)", (batch,)).fetchone() == (None, "CONNECTOR_EXCLUDED")
    assert db.execute("SELECT * FROM control.connector_batch_app_scope(%s)", (batch,)).fetchone() == ("00042", None)
    raw = db.execute("INSERT INTO raw.customer_current(batch_id,source_system_id,source_customer_id,first_name) "
                     "VALUES(%s,11,'alice','Alice') RETURNING raw_customer_id,app_id", (batch,)).fetchone()
    assert raw[1] == "00042"
    assert db.execute("SELECT app_id FROM raw.customer WHERE batch_id=%s", (batch,)).fetchone() == ("00042",)
    db.execute("INSERT INTO staging.customer_profile(batch_id,raw_customer_id,source_system_id,source_customer_id) "
               "VALUES(%s,%s,11,'alice')", (batch, raw[0]))
    assert db.execute("SELECT app_id FROM staging.customer_profile").fetchone() == ("00042",)
    db.execute("INSERT INTO staging.customer_profile_history(batch_id,raw_customer_id,source_system_id,source_customer_id) "
               "VALUES(%s,%s,11,'alice')", (batch, raw[0]))
    assert db.execute("SELECT app_id FROM staging.customer_profile_history").fetchone() == ("00042",)

@pytest.mark.parametrize("target", ["staging.customer_profile", "staging.customer_profile_history"])
def test_staging_cannot_label_other_app_raw_identity_as_connector_app(db, target: str) -> None:
    db.execute((ROOT / "database/migrations/040_connector_app_lineage.sql").read_text())
    a_workspace, a_batch, _ = _lineage(db, "00042", source_id=11)
    b_workspace, b_batch, _ = _lineage(db, "42", source_id=12)
    db.execute("SELECT control.bind_connector_batch_app_id(%s,%s,'00042')", (a_batch, a_workspace))
    db.execute("SELECT control.bind_connector_batch_app_id(%s,%s,'42')", (b_batch, b_workspace))
    a_raw = db.execute(
        "INSERT INTO raw.customer_current(batch_id,source_system_id,source_customer_id) "
        "VALUES(%s,11,'same-person') RETURNING raw_customer_id", (a_batch,)
    ).fetchone()[0]
    with db.transaction():
        with pytest.raises(psycopg.Error, match="staging.*(identity|app|source)|foreign key constraint"):
            db.execute(
                f"INSERT INTO {target}(batch_id,raw_customer_id,source_system_id,source_customer_id) "
                "VALUES(%s,%s,11,'same-person')", (b_batch, a_raw)
            )
    assert db.execute(f"SELECT count(*) FROM {target} WHERE batch_id=%s", (b_batch,)).fetchone() == (0,)

@pytest.mark.parametrize("target", ["staging.customer_profile", "staging.customer_profile_history"])
def test_staging_cannot_label_wrong_source_key_as_connector_identity(db, target: str) -> None:
    db.execute((ROOT / "database/migrations/040_connector_app_lineage.sql").read_text())
    workspace, batch, _ = _lineage(db)
    db.execute("SELECT control.bind_connector_batch_app_id(%s,%s,'00042')", (batch, workspace))
    raw_id = db.execute(
        "INSERT INTO raw.customer_current(batch_id,source_system_id,source_customer_id) "
        "VALUES(%s,11,'actual') RETURNING raw_customer_id", (batch,)
    ).fetchone()[0]
    with db.transaction():
        with pytest.raises(psycopg.Error, match="staging.*(identity|app|source)"):
            db.execute(
                f"INSERT INTO {target}(batch_id,raw_customer_id,source_system_id,source_customer_id) "
                "VALUES(%s,%s,11,'forged')", (batch, raw_id)
            )
    assert db.execute(f"SELECT count(*) FROM {target} WHERE batch_id=%s", (batch,)).fetchone() == (0,)


@pytest.mark.parametrize("first_app", ["00042", "42"])
def test_connector_bulk_merge_persists_literal_app_in_current_history_and_membership(db, first_app: str) -> None:
    db.execute((ROOT / "database/migrations/040_connector_app_lineage.sql").read_text())
    apps = (first_app, "42" if first_app == "00042" else "00042")
    for source_id, app in enumerate(apps, 11):
        workspace, batch, _ = _lineage(db, app, source_id=source_id)
        db.execute("SELECT control.bind_connector_batch_app_id(%s,%s,%s)", (batch, workspace, app))
        with db.transaction():
            db.execute("CREATE TEMP TABLE customer_bulk_stage "
                       "(LIKE raw.customer_current INCLUDING DEFAULTS INCLUDING CONSTRAINTS) ON COMMIT DROP")
            db.execute("CREATE UNIQUE INDEX ON pg_temp.customer_bulk_stage (source_customer_id)")
            db.execute("INSERT INTO pg_temp.customer_bulk_stage(batch_id,source_system_id,source_customer_id,first_name) "
                       "VALUES(%s,%s,'bulk-key','Alice')", (batch, source_id))
            assert db.execute("SELECT raw.apply_customer_bulk(%s,%s)", (batch, source_id)).fetchone() == (1,)
        assert db.execute("SELECT app_id FROM raw.customer_current WHERE source_customer_id='bulk-key' "
                          "AND source_system_id=%s", (source_id,)).fetchone() == (app,)
        assert db.execute("SELECT app_id FROM raw.customer WHERE batch_id=%s", (batch,)).fetchone() == (app,)
        assert db.execute("SELECT count(*) FROM raw.customer_batch_member WHERE ingestion_batch_id=%s", (batch,)).fetchone() == (1,)
    assert db.execute("SELECT count(DISTINCT raw_customer_id) FROM raw.customer_current "
                      "WHERE source_customer_id='bulk-key'").fetchone() == (2,)


def test_wrong_app_cannot_bind_or_mutate_connector_batch(db) -> None:
    db.execute((ROOT / "database/migrations/040_connector_app_lineage.sql").read_text())
    workspace, batch, _ = _lineage(db)
    with db.transaction():
        with pytest.raises(psycopg.Error, match="app_id"):
            db.execute("SELECT control.bind_connector_batch_app_id(%s,%s,'42')", (batch, workspace))
    assert db.execute("SELECT app_id FROM control.ingestion_batch WHERE batch_id=%s", (batch,)).fetchone() == (None,)
    db.execute("SELECT control.bind_connector_batch_app_id(%s,%s,'00042')", (batch, workspace))
    with db.transaction():
        with pytest.raises(psycopg.Error, match="app_id"):
            db.execute("UPDATE control.ingestion_batch SET app_id='42' WHERE batch_id=%s", (batch,))


def test_unbound_connector_batch_cannot_accept_direct_raw_customer(db) -> None:
    db.execute((ROOT / "database/migrations/040_connector_app_lineage.sql").read_text())
    _, batch, _ = _lineage(db)
    with db.transaction():
        with pytest.raises(psycopg.Error, match="connector.*app_id.*unbound"):
            db.execute(
                "INSERT INTO raw.customer_current(batch_id,source_system_id,source_customer_id) "
                "VALUES(%s,11,'direct-unbound')", (batch,)
            )
    assert db.execute("SELECT count(*) FROM raw.customer_current WHERE source_customer_id='direct-unbound'").fetchone() == (0,)


def test_bound_connector_batch_rejects_direct_raw_with_other_source(db) -> None:
    db.execute((ROOT / "database/migrations/040_connector_app_lineage.sql").read_text())
    workspace, batch, _ = _lineage(db)
    db.execute("INSERT INTO control.source_system(source_system_id,source_code,source_name,source_type) "
               "VALUES(12,'other','Other','file')")
    db.execute("SELECT control.bind_connector_batch_app_id(%s,%s,'00042')", (batch, workspace))
    with db.transaction():
        with pytest.raises(psycopg.Error, match="source conflicts with batch"):
            db.execute("INSERT INTO raw.customer_current(batch_id,source_system_id,source_customer_id) "
                       "VALUES(%s,12,'other-source')", (batch,))
    assert db.execute("SELECT count(*) FROM raw.customer_current WHERE source_customer_id='other-source'").fetchone() == (0,)


@pytest.mark.parametrize("change", [
    "metadata='{}'::jsonb",
    "batch_ref='2026-09-27-120001-00042-abcdef123456'",
    "source_system_id=12",
    "landing_batch_id=NULL",
])
def test_connector_cannot_reclassify_bound_lineage(db, change: str) -> None:
    db.execute((ROOT / "database/migrations/040_connector_app_lineage.sql").read_text())
    workspace, batch, _ = _lineage(db)
    db.execute("SELECT control.bind_connector_batch_app_id(%s,%s,'00042')", (batch, workspace))
    with db.transaction():
        with pytest.raises(psycopg.Error, match="connector.*lineage"):
            db.execute(f"UPDATE control.ingestion_batch SET {change} WHERE batch_id=%s", (batch,))
    assert db.execute("SELECT app_id FROM control.ingestion_batch WHERE batch_id=%s", (batch,)).fetchone() == ("00042",)


def test_unbound_workspace_cannot_bind_and_upload_ref_still_parses(db) -> None:
    db.execute((ROOT / "database/migrations/040_connector_app_lineage.sql").read_text())
    workspace, batch, _ = _lineage(db, None)
    with db.transaction():
        with pytest.raises(psycopg.Error, match="app_id"):
            db.execute("SELECT control.bind_connector_batch_app_id(%s,%s,'00042')", (batch, workspace))
    landing, upload = uuid4(), uuid4()
    ref = "2026-09-27-120001-00042-abcdef123456"
    db.execute("INSERT INTO control.source_system(source_system_id,source_code,source_name,source_type) "
               "VALUES(12,'file-upload','File upload','s3_csv')")
    db.execute("INSERT INTO control.source_landing_batch(batch_id,source_code,batch_ref) VALUES(%s,%s,%s)",
               (landing, 'file-upload', ref))
    db.execute("INSERT INTO control.ingestion_batch(batch_id,landing_batch_id,source_system_id,batch_ref) "
               "VALUES(%s,%s,12,%s)", (upload, landing, ref))
    assert db.execute("SELECT app_id FROM control.ingestion_batch WHERE batch_id=%s", (upload,)).fetchone() == ("00042",)


def test_upload_cannot_reuse_connector_physical_source_id(db) -> None:
    db.execute((ROOT / "database/migrations/040_connector_app_lineage.sql").read_text())
    _lineage(db)
    source_code = db.execute("SELECT source_code FROM control.source_system WHERE source_system_id=11").fetchone()[0]
    landing, batch = uuid4(), uuid4()
    ref = "2026-09-27-120001-42-abcdef123456"
    db.execute("INSERT INTO control.source_landing_batch(batch_id,source_code,batch_ref) VALUES(%s,%s,%s)",
               (landing, source_code, ref))
    with db.transaction():
        with pytest.raises(psycopg.Error, match="connector source.*upload"):
            db.execute("INSERT INTO control.ingestion_batch(batch_id,landing_batch_id,source_system_id,batch_ref) "
                       "VALUES(%s,%s,11,%s)", (batch, landing, ref))
    assert db.execute("SELECT count(*) FROM control.ingestion_batch WHERE batch_id=%s", (batch,)).fetchone() == (0,)


@pytest.mark.parametrize("change", [
    "source_code='upload-lookalike'",
    "source_type='s3_csv'",
])
def test_registered_source_identity_cannot_be_reclassified(db, change: str) -> None:
    db.execute((ROOT / "database/migrations/040_connector_app_lineage.sql").read_text())
    workspace, batch, _ = _lineage(db)
    db.execute("SELECT control.bind_connector_batch_app_id(%s,%s,'00042')", (batch, workspace))
    with db.transaction():
        with pytest.raises(psycopg.Error, match="connector source identity is immutable"):
            db.execute(f"UPDATE control.source_system SET {change} WHERE source_system_id=11")
    assert db.execute("SELECT * FROM control.connector_batch_app_scope(%s)", (batch,)).fetchone() == (
        "00042", None,
    )


def test_quarantined_source_cannot_be_recoded_or_reattached(db) -> None:
    workspace, _, sync = _lineage(db)
    db.execute("UPDATE control.source_system SET source_code='legacy-connector' WHERE source_system_id=11")
    db.execute("UPDATE connector.sync_run SET status='FAILED' WHERE sync_run_id=%s", (sync,))
    db.execute((ROOT / "database/migrations/040_connector_app_lineage.sql").read_text())
    connection_id = db.execute("SELECT connection_id FROM connector.connection WHERE workspace_id=%s",
                               (workspace,)).fetchone()[0]
    assert db.execute("SELECT source_system_id FROM control.connector_legacy_source_quarantine "
                      "WHERE source_system_id=11").fetchone() == (11,)
    with db.transaction():
        with pytest.raises(psycopg.Error, match="quarantin"):
            db.execute("UPDATE control.source_system SET source_code=%s WHERE source_system_id=11",
                       ("dc1-" + connection_id.hex,))
    with db.transaction():
        with pytest.raises(psycopg.Error, match="ownership|quarantin"):
            db.execute("UPDATE connector.connection SET source_system_id=11 WHERE connection_id=%s",
                       (connection_id,))


@pytest.mark.parametrize("target", ["raw.customer_current", "raw.customer", "raw.customer_batch_member"])
def test_upload_row_cannot_borrow_connector_source(db, target: str) -> None:
    db.execute((ROOT / "database/migrations/040_connector_app_lineage.sql").read_text())
    _lineage(db)
    landing, batch = uuid4(), uuid4()
    ref = "2026-09-27-120001-00042-abcdef123456"
    db.execute("INSERT INTO control.source_system(source_system_id,source_code,source_name,source_type) "
               "VALUES(12,'file-upload','File upload','s3_csv')")
    db.execute("INSERT INTO control.source_landing_batch(batch_id,source_code,batch_ref) "
               "VALUES(%s,'file-upload',%s)", (landing, ref))
    db.execute("INSERT INTO control.ingestion_batch(batch_id,landing_batch_id,source_system_id,batch_ref) "
               "VALUES(%s,%s,12,%s)", (batch, landing, ref))
    if target == "raw.customer_batch_member":
        other_landing, other_batch = uuid4(), uuid4()
        other_ref = "2026-09-27-120002-00042-abcdef123456"
        db.execute("INSERT INTO control.source_landing_batch(batch_id,source_code,batch_ref) "
                   "VALUES(%s,'file-upload',%s)", (other_landing, other_ref))
        db.execute("INSERT INTO control.ingestion_batch(batch_id,landing_batch_id,source_system_id,batch_ref) "
                   "VALUES(%s,%s,12,%s)", (other_batch, other_landing, other_ref))
        real = db.execute("INSERT INTO raw.customer_current(batch_id,source_system_id,source_customer_id) "
                          "VALUES(%s,12,'real') RETURNING raw_customer_id", (other_batch,)).fetchone()[0]
        statement = "INSERT INTO raw.customer_batch_member(ingestion_batch_id,raw_customer_id,source_system_id,source_customer_id) VALUES(%s,%s,11,'real')"
        args = (batch, real)
    else:
        statement = f"INSERT INTO {target}(batch_id,source_system_id,source_customer_id) VALUES(%s,11,'borrowed')"
        args = (batch,)
    with db.transaction():
        with pytest.raises(psycopg.Error, match="source|connector"):
            db.execute(statement, args)


def test_member_cannot_claim_wrong_raw_identity_or_unbound_connector(db) -> None:
    db.execute((ROOT / "database/migrations/040_connector_app_lineage.sql").read_text())
    workspace, batch, _ = _lineage(db)
    other_landing, other_batch = uuid4(), uuid4()
    other_ref = "2026-09-27-120002-00042-abcdef123456"
    db.execute("INSERT INTO control.source_landing_batch(batch_id,source_code,batch_ref) "
               "VALUES(%s,'file-upload',%s)", (other_landing, other_ref))
    db.execute("INSERT INTO control.source_system(source_system_id,source_code,source_name,source_type) "
               "VALUES(12,'file-upload','File upload','s3_csv')")
    db.execute("INSERT INTO control.ingestion_batch(batch_id,landing_batch_id,source_system_id,batch_ref) "
               "VALUES(%s,%s,12,%s)", (other_batch, other_landing, other_ref))
    old_raw = db.execute("INSERT INTO raw.customer_current(batch_id,source_system_id,source_customer_id) "
                         "VALUES(%s,12,'old') RETURNING raw_customer_id", (other_batch,)).fetchone()[0]
    with db.transaction():
        with pytest.raises(psycopg.Error, match="unbound"):
            db.execute("INSERT INTO raw.customer_batch_member(ingestion_batch_id,raw_customer_id,source_system_id,source_customer_id) VALUES(%s,%s,11,'old')",
                       (batch, old_raw))
    db.execute("SELECT control.bind_connector_batch_app_id(%s,%s,'00042')", (batch, workspace))
    row = db.execute("INSERT INTO raw.customer_current(batch_id,source_system_id,source_customer_id) "
                     "VALUES(%s,11,'real') RETURNING raw_customer_id", (batch,)).fetchone()[0]
    other_workspace, other_connector_batch, _ = _lineage(db, source_id=13)
    db.execute("SELECT control.bind_connector_batch_app_id(%s,%s,'00042')",
               (other_connector_batch, other_workspace))
    with db.transaction():
        with pytest.raises(psycopg.Error, match="identity|source"):
            db.execute("INSERT INTO raw.customer_batch_member(ingestion_batch_id,raw_customer_id,source_system_id,source_customer_id) VALUES(%s,%s,11,'wrong')",
                       (other_connector_batch, row))


def test_upload_current_backfill_remains_available_after_040(db) -> None:
    db.execute((ROOT / "database/migrations/040_connector_app_lineage.sql").read_text())
    landing, batch = uuid4(), uuid4()
    ref = "2026-09-27-120001-00042-abcdef123456"
    db.execute("INSERT INTO control.source_system(source_system_id,source_code,source_name,source_type) "
               "VALUES(12,'file-upload','File upload','s3_csv')")
    db.execute("INSERT INTO control.source_landing_batch(batch_id,source_code,batch_ref) VALUES(%s,'file-upload',%s)", (landing, ref))
    db.execute("INSERT INTO control.ingestion_batch(batch_id,landing_batch_id,source_system_id,batch_ref) VALUES(%s,%s,12,%s)",
               (batch, landing, ref))
    db.execute("INSERT INTO raw.customer_current(batch_id,source_system_id,source_customer_id) "
               "VALUES(%s,12,'upload')", (batch,))
    assert db.execute("SELECT scanned_count,app_id,error_code FROM control.backfill_customer_app_id('raw.customer_current',%s,NULL,1)",
                      (batch,)).fetchone() == (1, "00042", None)

def test_upload_staging_backfill_remains_available_after_040(db) -> None:
    db.execute((ROOT / "database/migrations/040_connector_app_lineage.sql").read_text())
    landing, batch = uuid4(), uuid4()
    ref = "2026-09-27-120001-00042-abcdef123456"
    db.execute("INSERT INTO control.source_system(source_system_id,source_code,source_name,source_type) "
               "VALUES(12,'file-upload','File upload','s3_csv')")
    db.execute("INSERT INTO control.source_landing_batch(batch_id,source_code,batch_ref) "
               "VALUES(%s,'file-upload',%s)", (landing, ref))
    db.execute("INSERT INTO control.ingestion_batch(batch_id,landing_batch_id,source_system_id,batch_ref) "
               "VALUES(%s,%s,12,%s)", (batch, landing, ref))
    raw_id = db.execute("INSERT INTO raw.customer_current(batch_id,source_system_id,source_customer_id) "
                        "VALUES(%s,12,'upload') RETURNING raw_customer_id", (batch,)).fetchone()[0]
    db.execute("INSERT INTO staging.customer_profile(batch_id,raw_customer_id,source_system_id,source_customer_id) "
               "VALUES(%s,%s,12,'upload')", (batch, raw_id))
    assert db.execute(
        "SELECT scanned_count,app_id,error_code FROM "
        "control.backfill_customer_app_id('staging.customer_profile',%s,NULL,1)", (batch,)
    ).fetchone() == (1, "00042", None)
