"""Background jobs (imports, folder scans, thumbnails) in a single worker thread.

Jobs run one after another, progress is stored in the ``jobs`` table so the UI
can show it.
"""
import logging
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import models
from database import SessionLocal

logger = logging.getLogger("jobs")

_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="job")
_handlers = {}


def handler(kind: str):
    def register(func):
        _handlers[kind] = func
        return func

    return register


def submit(db, kind: str, params: dict | None = None) -> models.Job:
    job = models.Job(kind=kind, status="queued", params=params or {})
    db.add(job)
    db.commit()
    db.refresh(job)
    _executor.submit(_run, job.id)
    return job


def progress(job_id: int, done: int | None = None, total: int | None = None, message: str | None = None):
    """Stores progress in its own session, so the job's own transaction is not touched."""
    db = SessionLocal()
    try:
        job = db.get(models.Job, job_id)
        if job is None:
            return
        if done is not None:
            job.progress = done
        if total is not None:
            job.total = total
        if message is not None:
            job.message = message
        db.commit()
    finally:
        db.close()


def _run(job_id: int):
    db = SessionLocal()
    try:
        job = db.get(models.Job, job_id)
        job.status = "running"
        db.commit()
        func = _handlers[job.kind]
        result = func(db, job) or {}
        db.commit()
        job = db.get(models.Job, job_id)
        db.refresh(job)
        job.status = "done"
        job.result = result
        job.finished_at = datetime.now(timezone.utc)
        db.commit()
    except Exception as exc:
        logger.error("Job %s failed:\n%s", job_id, traceback.format_exc())
        db.rollback()
        job = db.get(models.Job, job_id)
        if job:
            job.status = "failed"
            job.message = str(exc)[:2000]
            job.finished_at = datetime.now(timezone.utc)
            db.commit()
    finally:
        db.close()


def recover(db):
    """Marks jobs that were interrupted by a restart as failed."""
    for job in db.query(models.Job).filter(models.Job.status.in_(["queued", "running"])):
        job.status = "failed"
        job.message = "interrupted by restart"
    db.commit()


def running(db, kinds: list[str]):
    return (
        db.query(models.Job)
        .filter(models.Job.kind.in_(kinds), models.Job.status.in_(["queued", "running"]))
        .order_by(models.Job.id.desc())
        .first()
    )


def wait_idle(timeout: float = 30.0):
    """For tests: blocks until all submitted jobs are finished."""
    _executor.submit(lambda: None).result(timeout=timeout)
