"""
Job runner (Siiqo 2.0, Phase 1): jobs are rows in Postgres.

    from app.services import jobs

    @jobs.handler("onboarding_build")
    def run_build(payload, job):
        ...
        return {"ok": True}          # stored as job.result

    job = jobs.enqueue("onboarding_build", {"session_id": 7}, user_id=user.id)

Backends (``JOBS_MODE`` env var or app config, read at enqueue time):
  * ``thread`` (default): a daemon thread runs the job inside an app context.
  * ``sync``: the job runs inline before ``enqueue`` returns (tests set this;
    with TESTING on and no explicit mode, ``sync`` is used too).
  * ``rq``: reserved. See ``_dispatch_rq`` - the hook for moving to RQ once
    ``REDIS_URL`` is set. Until it is implemented, ``rq`` falls back to threads.

Safety across several web processes: a job is claimed with a conditional
UPDATE (``status='queued'`` -> ``'running'``); only the process whose UPDATE
changed the row runs it. A job left ``running`` for more than
``STALE_AFTER`` (10 minutes), or ``queued`` that long, is marked failed by
``expire_stale`` - called on read (``get``/``refresh``), so no scheduler is
needed.
"""
from __future__ import annotations

import logging
import os
import threading
from datetime import datetime, timedelta, timezone
from typing import Callable

from flask import current_app
from sqlalchemy import and_, or_, update

from app.extensions import db
from app.models.jobs import Job

log = logging.getLogger(__name__)

STALE_AFTER = timedelta(minutes=10)
# Interactive jobs a person is waiting on fail faster, so the retry path shows quickly.
STALE_AFTER_BY_TYPE = {"onboarding_build": timedelta(minutes=3)}


def stale_after(job_type: str | None) -> timedelta:
    return STALE_AFTER_BY_TYPE.get(job_type or "", STALE_AFTER)
MODES = ("thread", "sync", "rq")

Handler = Callable[[dict, Job], "dict | None"]
_HANDLERS: dict[str, Handler] = {}

# Modules that register handlers; imported lazily by run_job so a job can run
# in a fresh process (an RQ worker) that never imported the routes.
_HANDLER_MODULES = ("app.services.onboarding_build",)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime | None) -> datetime | None:
    """SQLite returns naive datetimes; treat them as UTC."""
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


# ── Registry ────────────────────────────────────────────────────────────────
def handler(job_type: str):
    def decorator(fn: Handler) -> Handler:
        _HANDLERS[job_type] = fn
        return fn
    return decorator


def _get_handler(job_type: str) -> Handler | None:
    if job_type not in _HANDLERS:
        import importlib
        for module in _HANDLER_MODULES:
            importlib.import_module(module)
    return _HANDLERS.get(job_type)


# ── Mode ────────────────────────────────────────────────────────────────────
def mode() -> str:
    raw = (os.environ.get("JOBS_MODE") or current_app.config.get("JOBS_MODE") or "").strip().lower()
    if raw in MODES:
        return raw
    if current_app.config.get("TESTING"):
        return "sync"
    return "thread"


# ── Enqueue / dispatch ──────────────────────────────────────────────────────
def enqueue(job_type: str, payload: dict | None = None, *, user_id: int | None = None,
            max_attempts: int = 1) -> Job:
    """Insert a queued job, commit, and hand it to the backend. Commits the session."""
    if _get_handler(job_type) is None:
        raise ValueError(f"No handler registered for job type {job_type!r}")
    job = Job(type=job_type, payload=payload or {}, status="queued", user_id=user_id,
              max_attempts=max(1, int(max_attempts)))
    db.session.add(job)
    db.session.commit()
    _dispatch(job.id)
    return job


def _dispatch(job_id: int) -> None:
    m = mode()
    if m == "sync":
        run_job(job_id)
        return
    if m == "rq":
        try:
            _dispatch_rq(job_id)
            return
        except NotImplementedError:
            log.warning("JOBS_MODE=rq is not implemented yet; running job %s on a thread", job_id)
    app = current_app._get_current_object()
    thread = threading.Thread(target=_run_in_app, args=(app, job_id), name=f"job-{job_id}", daemon=True)
    thread.start()


def _dispatch_rq(job_id: int) -> None:
    # ── RQ HOOK ─────────────────────────────────────────────────────────────
    # When REDIS_URL is set and the `rq` package is added, replace this body:
    #
    #     from redis import Redis
    #     from rq import Queue
    #     Queue("siiqo", connection=Redis.from_url(os.environ["REDIS_URL"])) \
    #         .enqueue("app.services.jobs.run_job_standalone", job_id)
    #
    # and run a worker (Procfile: `worker: rq worker siiqo`). The job row,
    # the conditional claim in run_job and the stale sweep stay as they are,
    # so the thread and RQ backends can even run side by side.
    raise NotImplementedError("RQ backend not wired yet")


def run_job_standalone(job_id: int) -> None:
    """Entry point for an out-of-process worker (RQ): builds its own app."""
    from app import create_app
    app = create_app()
    _run_in_app(app, job_id)


def _run_in_app(app, job_id: int) -> None:
    with app.app_context():
        try:
            run_job(job_id)
        except Exception:  # noqa: BLE001 - never let a thread die noisily
            log.exception("Job %s crashed outside its handler", job_id)
        finally:
            db.session.remove()


# ── Execution ───────────────────────────────────────────────────────────────
def claim(job_id: int) -> bool:
    """queued -> running, atomically. True only for the caller that changed the row."""
    now = utcnow()
    res = db.session.execute(
        update(Job)
        .where(Job.id == job_id, Job.status == "queued")
        .values(status="running", attempts=Job.attempts + 1, started_at=now, updated_at=now)
        .execution_options(synchronize_session=False)
    )
    db.session.commit()
    return res.rowcount == 1


def run_job(job_id: int) -> Job | None:
    """Claim and run one job. Returns the job row (or None if another process has it)."""
    while True:
        if not claim(job_id):
            return db.session.get(Job, job_id)
        job = db.session.get(Job, job_id)
        db.session.refresh(job)
        fn = _get_handler(job.type)
        try:
            if fn is None:
                raise LookupError(f"No handler registered for job type {job.type!r}")
            result = fn(dict(job.payload or {}), job)
        except Exception as exc:  # noqa: BLE001 - failures are recorded, not raised
            db.session.rollback()
            job = db.session.get(Job, job_id)
            log.warning("Job %s (%s) failed on attempt %s: %s", job_id, job.type, job.attempts, exc)
            retry = job.attempts < job.max_attempts
            job.status = "queued" if retry else "failed"
            job.error = f"{type(exc).__name__}: {exc}"[:2000]
            job.finished_at = None if retry else utcnow()
            db.session.commit()
            if retry:
                continue
            return job
        job = db.session.get(Job, job_id)
        job.status = "done"
        job.result = result if isinstance(result, dict) else ({"value": result} if result is not None else None)
        job.error = None
        job.finished_at = utcnow()
        db.session.commit()
        return job


# ── Reads ───────────────────────────────────────────────────────────────────
def expire_stale(now: datetime | None = None) -> int:
    """Mark jobs failed that have sat in running or queued longer than STALE_AFTER."""
    now = now or utcnow()
    total = 0
    special = list(STALE_AFTER_BY_TYPE)
    groups = [(Job.type == t, STALE_AFTER_BY_TYPE[t]) for t in special]
    groups.append((Job.type.notin_(special), STALE_AFTER))
    for type_filter, limit in groups:
        cutoff = now - limit
        res = db.session.execute(
            update(Job)
            .where(type_filter, or_(
                and_(Job.status == "running", Job.started_at < cutoff),
                and_(Job.status == "queued", Job.created_at < cutoff),
            ))
            .values(status="failed", error=f"Timed out: no progress for {int(limit.total_seconds() // 60)} minutes",
                    finished_at=now, updated_at=now)
            .execution_options(synchronize_session=False)
        )
        total += res.rowcount or 0
    db.session.commit()
    return total


def is_stale(job: Job, now: datetime | None = None) -> bool:
    now = now or utcnow()
    limit = stale_after(job.type)
    if job.status == "running":
        return (_aware(job.started_at) or now) < now - limit
    if job.status == "queued":
        return (_aware(job.created_at) or now) < now - limit
    return False


def refresh(job: Job | None) -> Job | None:
    """Reload a job, failing it first if it is stale."""
    if job is None:
        return None
    if is_stale(job):
        expire_stale()
    db.session.refresh(job)
    return job


def get(job_id: int | None) -> Job | None:
    if not job_id:
        return None
    return refresh(db.session.get(Job, job_id))
