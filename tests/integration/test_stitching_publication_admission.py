"""Publisher/admission concurrency on a disposable local PostgreSQL database."""

import os

import psycopg2

from test_stitching_admission import ROOT, Ecs, admission_module, db, load, seed


def test_publisher_lock_does_not_block_admission_of_idle_writer(monkeypatch, db):
    """The publisher may own its key while an independent job is dispatched."""
    mod = load()
    admission = admission_module()
    ecs = Ecs()
    with db, db.cursor() as cursor:
        cursor.execute((ROOT / 'database/migrations/018_stitching_admission.sql').read_text())
    _, job_id = seed(db)
    holder = psycopg2.connect(os.environ['CDP_TEST_DSN'])
    monkeypatch.setattr(mod, 'boto3', type('Aws', (), {'client': staticmethod(lambda _: ecs)}))
    monkeypatch.setenv('ECS_CLUSTER', 'test')
    monkeypatch.setattr(mod, '_stitching_task_request',
                        lambda batch, job: {'clientToken': 'stitch-'+job,
                                            'taskDefinition': 'pinned:9'})
    monkeypatch.setattr(mod, '_start_stitching_task', lambda *args, **kwargs: 'task-new')
    try:
        with holder.cursor() as cursor:
            cursor.execute('SELECT pg_advisory_lock(%s)', (7413220180,))
        assert admission.tick(db, mod)['job_id'] == job_id
        with db.cursor() as cursor:
            cursor.execute('SELECT state FROM control.stitching_admission WHERE job_id=%s',
                           (job_id,))
            assert cursor.fetchone() == ('LAUNCHED',)
        with holder.cursor() as cursor:
            cursor.execute('SELECT pg_advisory_unlock(%s)', (7413220180,))
            assert cursor.fetchone() == (True,)
    finally:
        holder.close()
