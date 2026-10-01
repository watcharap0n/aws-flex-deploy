"""Publication status against the real 015 view + 021 history projection.

Run with CDP_PUBLICATION_STATUS_TEST_DSN pointing at local poststitch_status_test.
The fixture resets only this explicitly named disposable database's schemas.
"""
import os
from pathlib import Path
import uuid

import psycopg2
import pytest
from psycopg2.extras import RealDictCursor

ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = ROOT / "database/migrations"
MIGRATION = MIGRATIONS / "025_customer_publication_status.sql"


@pytest.fixture
def db():
    dsn = os.environ.get("CDP_PUBLICATION_STATUS_TEST_DSN")
    if not dsn:
        pytest.skip("Set CDP_PUBLICATION_STATUS_TEST_DSN to local poststitch_status_test")
    params = psycopg2.extensions.parse_dsn(dsn)
    assert params.get("host") in {"127.0.0.1", "localhost"}
    assert params.get("dbname") == "poststitch_status_test" or (
        params.get("host") == "127.0.0.1" and params.get("port") == "61577"
        and params.get("dbname") == "poststitch_upload_scope_test")
    conn = psycopg2.connect(dsn)
    conn.autocommit = True
    with conn.cursor() as q:
        q.execute("DROP SCHEMA IF EXISTS connector CASCADE; DROP SCHEMA IF EXISTS platform CASCADE")
        q.execute("DROP SCHEMA IF EXISTS identity CASCADE")
        q.execute((ROOT / "tests/integration/fixtures/customer_history_base.sql").read_text())
        q.execute((MIGRATIONS / "019_customer_typed_history.sql").read_text())
        q.execute((MIGRATIONS / "020_customer_history_recovery.sql").read_text())
        q.execute("""
            ALTER TABLE control.source_landing_batch
              ADD source_code text, ADD batch_ref text, ADD status text,
              ADD output_published_at timestamptz, ADD updated_at timestamptz DEFAULT now();
            ALTER TABLE control.ingestion_batch
              ADD records_received bigint, ADD records_rejected bigint DEFAULT 0,
              ADD completed_at timestamptz, ADD updated_at timestamptz DEFAULT now();
            CREATE TABLE control.stitching_job (
              job_id uuid PRIMARY KEY, ingestion_batch_id uuid, attempt integer,
              status text, ecs_task_arn text, selected_record_count bigint,
              standardized_record_count bigint DEFAULT 1, ready_record_count bigint DEFAULT 1,
              member_record_count bigint, entity_count bigint, submitted_at timestamptz,
              started_at timestamptz, completed_at timestamptz, error_code text, stop_code text,
              updated_at timestamptz DEFAULT now(), created_at timestamptz DEFAULT now());
            CREATE SCHEMA identity;
            CREATE TABLE identity.resolution_run (
              run_id uuid PRIMARY KEY, stitching_job_id uuid UNIQUE);
            CREATE SCHEMA quality;
        """)
        # Use actual validation-run DDL (its FKs target the fixture's real parents).
        quality = (MIGRATIONS / "015_customer_data_quality.sql").read_text()
        q.execute(quality[quality.index("CREATE TABLE IF NOT EXISTS quality.customer_validation_run"):
                          quality.index("CREATE INDEX IF NOT EXISTS ix_customer_validation_run_landing")])
        view = quality[quality.index("CREATE OR REPLACE VIEW control.customer_pipeline_status AS"):
                       quality.index("COMMENT ON VIEW control.customer_pipeline_status")]
        q.execute(view)
        q.execute((MIGRATIONS / "021_customer_history_recovery_outcomes.sql").read_text())
        # This is the 024 outbox interface, not an alternate publication implementation.
        q.execute("""CREATE TABLE control.customer_identity_transfer (
            transfer_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
            resolution_run_id uuid UNIQUE, stitching_job_id uuid UNIQUE,
            ingestion_batch_id uuid, workspace_id uuid, app_id text,
            publication_seq bigint, status text, row_count bigint, source_sha256 text,
            published_at timestamptz, created_at timestamptz DEFAULT now(),
            updated_at timestamptz DEFAULT now(), error_code text, attempts integer DEFAULT 0
        );
        CREATE TABLE control.customer_identity_capture_issue (
            stitching_job_id uuid PRIMARY KEY REFERENCES control.stitching_job(job_id),
            status text NOT NULL DEFAULT 'BLOCKED' CHECK (status='BLOCKED'),
            error_code text NOT NULL, created_at timestamptz NOT NULL DEFAULT clock_timestamp()
        )""")
        # Install the actual private connector-exclusion helper from 024.
        capture = (MIGRATIONS / '024_customer_identity_publication.sql').read_text()
        q.execute(capture[capture.index('CREATE FUNCTION control.customer_identity_connector_lineage'):
                          capture.index('CREATE FUNCTION control.resolve_customer_identity_batches')])
        q.execute('REVOKE ALL ON FUNCTION control.customer_identity_connector_lineage(uuid[]) FROM PUBLIC')
    yield conn
    conn.close()


def install(db):
    # Before implementation, exercise old behavior rather than failing on FileNotFoundError.
    if MIGRATION.exists():
        with db.cursor() as q:
            q.execute(MIGRATION.read_text())


def seed(db):
    landing, batch, job, resolution = [str(uuid.uuid4()) for _ in range(4)]
    with db.cursor() as q:
        q.execute("""INSERT INTO control.source_landing_batch
            (batch_id,status,output_status) VALUES (%s,'SOURCES_INGESTED','PUBLISHED')""", (landing,))
        q.execute("""INSERT INTO control.ingestion_batch
            (batch_id,landing_batch_id,status) VALUES (%s,%s,'completed')""", (batch, landing))
        q.execute("""INSERT INTO control.stitching_job
            (job_id,ingestion_batch_id,attempt,status) VALUES (%s,%s,1,'SUCCEEDED')""", (job, batch))
        q.execute("INSERT INTO identity.resolution_run VALUES (%s,%s)", (resolution, job))
    return batch, job, resolution


def status(db, batch):
    with db.cursor(cursor_factory=RealDictCursor) as q:
        q.execute("SELECT * FROM control.customer_pipeline_status WHERE ingestion_batch_id=%s", (batch,))
        return dict(q.fetchone())


def test_successful_stitch_without_outbox_is_not_publicly_complete(db):
    batch, _, _ = seed(db)
    install(db)
    row = status(db, batch)
    assert row["overall_status"] == "TRANSFER_PENDING"
    assert row["ingestion_status"] == "completed"
    assert row["stitching_status"] == "SUCCEEDED"


@pytest.mark.parametrize("transfer,public", [
    ("PENDING", "TRANSFER_PENDING"), ("RUNNING", "TRANSFERRING"),
    ("FAILED", "TRANSFER_FAILED"), ("BLOCKED", "TRANSFER_BLOCKED"),
    ("PUBLISHED", "COMPLETED"),
])
def test_transfer_transitions_preserve_stage_truth_and_update_clock(db, transfer, public):
    batch, job, resolution = seed(db)
    install(db)
    with db.cursor() as q:
        q.execute("""INSERT INTO control.customer_identity_transfer
            (ingestion_batch_id,stitching_job_id,resolution_run_id,status,updated_at)
            VALUES (%s,%s,%s,%s,'2099-01-01Z')""", (batch, job, resolution, transfer))
    row = status(db, batch)
    assert row["overall_status"] == public
    assert row["ingestion_status"] == "completed"
    assert row["stitching_status"] == "SUCCEEDED"
    assert row["transfer_status"] == transfer
    assert row["updated_at"] == row["transfer_updated_at"]


@pytest.mark.parametrize("warning", ["rejected", "not_ready"])
def test_warnings_only_complete_after_publication(db, warning):
    batch, job, resolution = seed(db)
    with db.cursor() as q:
        if warning == "rejected":
            q.execute("UPDATE control.ingestion_batch SET records_rejected=1")
        else:
            q.execute("UPDATE control.stitching_job SET ready_record_count=0")
    install(db)
    assert status(db, batch)["overall_status"] == "TRANSFER_PENDING"
    with db.cursor() as q:
        q.execute("""INSERT INTO control.customer_identity_transfer
            (ingestion_batch_id,stitching_job_id,resolution_run_id,status)
            VALUES (%s,%s,%s,'PUBLISHED')""", (batch, job, resolution))
    assert status(db, batch)["overall_status"] == "COMPLETED_WITH_WARNINGS"


@pytest.mark.parametrize("mismatch", ["ingestion_batch_id", "resolution_run_id", "stitching_job_id"])
def test_mismatched_published_transfer_never_completes(db, mismatch):
    batch, job, resolution = seed(db)
    install(db)
    values = dict(ingestion_batch_id=batch, stitching_job_id=job, resolution_run_id=resolution)
    values[mismatch] = str(uuid.uuid4())
    with db.cursor() as q:
        q.execute("""INSERT INTO control.customer_identity_transfer
            (ingestion_batch_id,stitching_job_id,resolution_run_id,status)
            VALUES (%(ingestion_batch_id)s,%(stitching_job_id)s,%(resolution_run_id)s,'PUBLISHED')""", values)
    assert status(db, batch)["overall_status"] == "TRANSFER_PENDING"


def test_retry_uses_latest_attempt_not_old_published_job(db):
    batch, job, resolution = seed(db)
    install(db)
    with db.cursor() as q:
        q.execute("""INSERT INTO control.customer_identity_transfer
            (ingestion_batch_id,stitching_job_id,resolution_run_id,status)
            VALUES (%s,%s,%s,'PUBLISHED')""", (batch, job, resolution))
    assert status(db, batch)["overall_status"] == "COMPLETED"
    retry, retry_resolution = str(uuid.uuid4()), str(uuid.uuid4())
    with db.cursor() as q:
        q.execute("""INSERT INTO control.stitching_job
            (job_id,ingestion_batch_id,attempt,status) VALUES (%s,%s,2,'RUNNING')""", (retry, batch))
        q.execute("INSERT INTO identity.resolution_run VALUES (%s,%s)", (retry_resolution, retry))
    assert status(db, batch)["overall_status"] == "STITCHING"
    with db.cursor() as q:
        q.execute("UPDATE control.stitching_job SET status='SUCCEEDED' WHERE job_id=%s", (retry,))
    assert status(db, batch)["overall_status"] == "TRANSFER_PENDING"
    with db.cursor() as q:
        q.execute("""INSERT INTO control.customer_identity_transfer
            (ingestion_batch_id,stitching_job_id,resolution_run_id,status)
            VALUES (%s,%s,%s,'PUBLISHED')""", (batch, retry, retry_resolution))
    assert status(db, batch)["overall_status"] == "COMPLETED"
    assert status(db, batch)["stitching_job_id"] == retry


def test_catalog_order_oid_acl_history_and_reader_preserved(db):
    batch, _, _ = seed(db)
    role = "publication_status_reader_" + uuid.uuid4().hex
    with db.cursor() as q:
        q.execute(f"CREATE ROLE {role}")
        q.execute(f"GRANT USAGE ON SCHEMA control TO {role}")
        q.execute(f"GRANT SELECT ON control.customer_pipeline_status TO {role}")
        q.execute("SELECT oid,relacl FROM pg_class WHERE oid='control.customer_pipeline_status'::regclass")
        before = q.fetchone()
        q.execute("SELECT attname,atttypid,atttypmod FROM pg_attribute WHERE attrelid='control.customer_pipeline_status'::regclass AND attnum>0 ORDER BY attnum")
        columns = q.fetchall()
        q.execute("""INSERT INTO control.customer_history_recovery_outcome
            (attempt_id,ingestion_batch_id,status,reason_code,evidence)
            VALUES (gen_random_uuid(),%s,'FAILED','MISSING_HISTORICAL_MEMBERSHIP','{}')""", (batch,))
    try:
        install(db)
        with db.cursor() as q:
            q.execute("SELECT oid,relacl FROM pg_class WHERE oid='control.customer_pipeline_status'::regclass")
            assert q.fetchone() == before
            q.execute("SELECT attname,atttypid,atttypmod FROM pg_attribute WHERE attrelid='control.customer_pipeline_status'::regclass AND attnum>0 ORDER BY attnum")
            assert q.fetchall()[:len(columns)] == columns
            q.execute(f"SET ROLE {role}")
            row = status(db, batch)
            assert row["history_recovery_status"] == "FAILED"
            assert row["overall_status"] == "TRANSFER_PENDING"
            q.execute("SELECT overall_status FROM control.customer_publication_status")
            assert q.fetchone()[0] == "TRANSFER_PENDING"
    finally:
        with db.cursor() as q:
            q.execute("RESET ROLE")
            q.execute(f"DROP OWNED BY {role}")
            q.execute(f"DROP ROLE {role}")


@pytest.mark.parametrize("stage,expected", [
    ("landing", "LANDING"), ("ingestion", "INGESTING"),
    ("failed", "FAILED"), ("validation", "VALIDATING"),
])
def test_prior_stage_statuses_are_not_replaced(db, stage, expected):
    batch, _, _ = seed(db)
    with db.cursor() as q:
        if stage == "landing":
            q.execute("UPDATE control.source_landing_batch SET output_status=NULL")
        elif stage == "ingestion":
            q.execute("UPDATE control.ingestion_batch SET status='running'")
        elif stage == "failed":
            q.execute("UPDATE control.stitching_job SET status='FAILED'")
        else:
            q.execute("UPDATE control.stitching_job SET status='VALIDATING'")
    install(db)
    assert status(db, batch)["overall_status"] == expected


def test_stale_stitch_success_cannot_hide_failed_ingestion_in_api_contract(db):
    batch, job, resolution = seed(db)
    install(db)
    with db.cursor() as q:
        q.execute("""INSERT INTO control.customer_identity_transfer
            (ingestion_batch_id,stitching_job_id,resolution_run_id,status)
            VALUES (%s,%s,%s,'PUBLISHED')""", (batch, job, resolution))
        q.execute("UPDATE control.ingestion_batch SET status='failed'")
        q.execute("SELECT overall_status FROM control.customer_publication_status WHERE ingestion_batch_id=%s", (batch,))
        assert q.fetchone()[0] == "FAILED"
    assert status(db, batch)["overall_status"] == "FAILED"


def install_real_capture(db):
    stitcher = ROOT.parent / "fargate-user-stitching"
    with db.cursor() as q:
        q.execute("DROP SCHEMA IF EXISTS connector CASCADE; DROP SCHEMA IF EXISTS platform CASCADE")
        q.execute((stitcher / "tests/fixtures/repository_catalog.sql").read_text())
        q.execute((stitcher / "src/identity_stitcher/sql/001_identity_model.sql").read_text())
        q.execute((MIGRATIONS / "014_pipeline_tracking.sql").read_text())
        quality = (MIGRATIONS / "015_customer_data_quality.sql").read_text()
        q.execute(quality[quality.index("CREATE OR REPLACE VIEW control.customer_pipeline_status AS"):
                          quality.index("COMMENT ON VIEW control.customer_pipeline_status")])
        q.execute((MIGRATIONS / "024_customer_identity_publication.sql").read_text())
    install(db)


def test_real_024_capture_drives_publication_status(db):
    from test_customer_identity_publication import batch, finish, member, register

    install_real_capture(db)
    register(db)
    b = batch(db)
    member(db, b)
    finish(db, b)
    assert status(db, b["batch"])["overall_status"] == "TRANSFER_PENDING"
    for transfer_state, public in [("FAILED", "TRANSFER_FAILED"), ("PUBLISHED", "COMPLETED")]:
        with db.cursor() as q:
            q.execute("UPDATE control.customer_identity_transfer SET status=%s WHERE stitching_job_id=%s",
                      (transfer_state, b["job"]))
        row = status(db, b["batch"])
        assert row["overall_status"] == public
        assert row["stitching_status"] == "SUCCEEDED"
        assert row["ingestion_status"] == "completed"


@pytest.mark.parametrize("problem", ["missing_resolution", "unfinished_resolution", "outbox_error"])
def test_real_024_capture_journal_blocks_status_without_changing_stitching(db, problem):
    from test_customer_identity_publication import batch, finish, member, register

    install_real_capture(db)
    register(db)
    b = batch(db)
    member(db, b)
    with db.cursor() as q:
        if problem == "outbox_error":
            q.execute("""CREATE FUNCTION control.synthetic_outbox_failure()
                RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
                    RAISE EXCEPTION 'synthetic private value'; END $$;
                CREATE TRIGGER synthetic_failure BEFORE INSERT ON control.customer_identity_transfer
                FOR EACH ROW EXECUTE FUNCTION control.synthetic_outbox_failure()""")
            finish(db, b)
        else:
            if problem == "missing_resolution":
                # No members/reference FKs are needed for this missing-run case.
                q.execute("DELETE FROM identity.customer_entity_member")
                q.execute("DELETE FROM identity.customer_entity")
                q.execute("DELETE FROM identity.resolution_run")
            q.execute("UPDATE control.stitching_job SET status='SUCCEEDED' WHERE job_id=%s", (b["job"],))
        q.execute("SELECT status,error_code,created_at FROM control.customer_identity_capture_issue WHERE stitching_job_id=%s", (b["job"],))
        issue = q.fetchone()
        assert issue[0] == "BLOCKED"
        assert issue[1] == ("CAPTURE_SQL_ERROR" if problem == "outbox_error" else "NO_SUCCESSFUL_RESOLUTION")
        q.execute("SELECT count(*) FROM control.customer_identity_transfer")
        assert q.fetchone()[0] == 0
        q.execute("SELECT * FROM control.customer_publication_status WHERE ingestion_batch_id=%s", (b["batch"],))
        contract = dict(zip([c.name for c in q.description], q.fetchone()))
    assert contract["overall_status"] == "TRANSFER_BLOCKED"
    row = status(db, b["batch"])
    assert row["overall_status"] == "TRANSFER_BLOCKED"
    for projected in (contract, row):
        assert projected["transfer_status"] == "BLOCKED"
        assert projected["transfer_error_code"] == issue[1]
        assert projected["transfer_updated_at"] == issue[2]
        assert projected["stitching_status"] == "SUCCEEDED"
    assert row["updated_at"] >= issue[2]
    assert row["ingestion_status"] == "completed"


@pytest.mark.parametrize("transfer_state", ["PENDING", "RUNNING", "FAILED", "BLOCKED", "PUBLISHED"])
def test_capture_journal_never_overrides_a_valid_outbox(db, transfer_state):
    batch, job, resolution = seed(db)
    install(db)
    with db.cursor() as q:
        q.execute("""INSERT INTO control.customer_identity_capture_issue(stitching_job_id,error_code,created_at)
            VALUES (%s,'CAPTURE_SQL_ERROR','2099-01-01Z')""", (job,))
        q.execute("""INSERT INTO control.customer_identity_transfer
            (ingestion_batch_id,stitching_job_id,resolution_run_id,status,updated_at)
            VALUES (%s,%s,%s,%s,'2025-01-01Z')""", (batch, job, resolution, transfer_state))
    row = status(db, batch)
    assert row["transfer_status"] == transfer_state
    assert row["transfer_error_code"] is None
    assert row["transfer_updated_at"].year == 2025
    assert row["overall_status"] == {
        "PENDING": "TRANSFER_PENDING", "RUNNING": "TRANSFERRING", "FAILED": "TRANSFER_FAILED",
        "BLOCKED": "TRANSFER_BLOCKED", "PUBLISHED": "COMPLETED"
    }[transfer_state]


def test_capture_journal_only_applies_to_latest_succeeded_job(db):
    batch, job, _ = seed(db)
    install(db)
    with db.cursor() as q:
        q.execute("""INSERT INTO control.customer_identity_capture_issue(stitching_job_id,error_code)
            VALUES (%s,'CAPTURE_SQL_ERROR')""", (job,))
    assert status(db, batch)["overall_status"] == "TRANSFER_BLOCKED"
    retry, resolution = str(uuid.uuid4()), str(uuid.uuid4())
    with db.cursor() as q:
        q.execute("""INSERT INTO control.stitching_job(job_id,ingestion_batch_id,attempt,status)
            VALUES (%s,%s,2,'RUNNING')""", (retry, batch))
        q.execute("INSERT INTO identity.resolution_run VALUES (%s,%s)", (resolution, retry))
    for expected in ("STITCHING", "TRANSFER_PENDING"):
        row = status(db, batch)
        assert row["overall_status"] == expected
        assert row["transfer_error_code"] is None
        assert row["transfer_updated_at"] is None
        with db.cursor() as q:
            q.execute("UPDATE control.stitching_job SET status='SUCCEEDED' WHERE job_id=%s", (retry,))


def test_migration_by_admin_keeps_nonadmin_view_owner_readable(db):
    batch, _, _ = seed(db)
    role = "publication_owner_" + uuid.uuid4().hex
    with db.cursor() as q:
        q.execute(f"CREATE ROLE {role}")
        q.execute(f"GRANT USAGE ON SCHEMA control,raw,quality,identity TO {role}")
        q.execute(f"GRANT SELECT ON ALL TABLES IN SCHEMA control,raw,quality,identity TO {role}")
        q.execute(f"ALTER VIEW control.customer_pipeline_status OWNER TO {role}")
    try:
        install(db)
        with db.cursor() as q:
            q.execute(f"SET ROLE {role}")
            assert status(db, batch)["overall_status"] == "TRANSFER_PENDING"
    finally:
        with db.cursor() as q:
            q.execute("RESET ROLE")
            q.execute("ALTER VIEW control.customer_pipeline_status OWNER TO CURRENT_USER")
            q.execute(f"DROP OWNED BY {role}")
            q.execute(f"DROP ROLE {role}")


def test_missing_dependency_is_a_migration_error(db):
    with db.cursor() as q:
        q.execute("DROP TABLE control.customer_identity_transfer")
        with pytest.raises(psycopg2.errors.UndefinedTable):
            q.execute(MIGRATION.read_text())
        q.execute("ROLLBACK")
    assert status(db, seed(db)[0])["overall_status"] == "COMPLETED"
