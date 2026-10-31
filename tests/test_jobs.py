"""Job runner (Phase 1): Postgres rows, conditional claim, sync/thread modes, stale sweep."""
import threading
from datetime import timedelta

import pytest

from app.extensions import db
from app.models.jobs import Job
from app.services import jobs

CALLS = []


@jobs.handler("test_echo")
def _echo(payload, job):
    CALLS.append((payload, job.id))
    return {"echo": payload.get("value"), "attempt": job.attempts}


@jobs.handler("test_boom")
def _boom(payload, job):
    raise RuntimeError("boom")


FLAKY = {"left": 0}


@jobs.handler("test_flaky")
def _flaky(payload, job):
    if FLAKY["left"] > 0:
        FLAKY["left"] -= 1
        raise RuntimeError("try again")
    return {"ok": True}


@pytest.fixture(autouse=True)
def _reset():
    CALLS.clear()
    FLAKY["left"] = 0


def test_tests_run_jobs_inline():
    assert jobs.mode() == "sync"


def test_enqueue_runs_and_stores_result():
    job = jobs.enqueue("test_echo", {"value": 42}, user_id=7)
    db.session.expire_all()
    job = db.session.get(Job, job.id)
    assert job.status == "done"
    assert job.result == {"echo": 42, "attempt": 1}
    assert job.attempts == 1 and job.user_id == 7
    assert job.started_at is not None and job.finished_at is not None
    assert CALLS == [({"value": 42}, job.id)]


def test_failed_job_records_error():
    job = jobs.enqueue("test_boom", {})
    db.session.expire_all()
    job = db.session.get(Job, job.id)
    assert job.status == "failed"
    assert "RuntimeError: boom" in job.error
    assert job.finished_at is not None


def test_retries_up_to_max_attempts():
    FLAKY["left"] = 1
    job = jobs.enqueue("test_flaky", {}, max_attempts=2)
    db.session.expire_all()
    job = db.session.get(Job, job.id)
    assert job.status == "done" and job.attempts == 2 and job.error is None


def test_unknown_job_type_is_refused():
    with pytest.raises(ValueError):
        jobs.enqueue("no_such_job", {})


def test_claim_is_conditional():
    job = Job(type="test_echo", payload={}, status="queued")
    db.session.add(job)
    db.session.commit()
    assert jobs.claim(job.id) is True
    assert jobs.claim(job.id) is False          # a second process loses the race
    db.session.expire_all()
    assert db.session.get(Job, job.id).attempts == 1


def test_run_job_skips_a_job_another_process_holds():
    job = Job(type="test_echo", payload={}, status="running", attempts=1, started_at=jobs.utcnow())
    db.session.add(job)
    db.session.commit()
    jobs.run_job(job.id)
    assert CALLS == []
    assert db.session.get(Job, job.id).status == "running"


def test_stale_running_job_is_failed_on_read():
    old = jobs.utcnow() - timedelta(minutes=11)
    job = Job(type="test_echo", payload={}, status="running", attempts=1, started_at=old, created_at=old)
    fresh = Job(type="test_echo", payload={}, status="running", attempts=1, started_at=jobs.utcnow())
    db.session.add_all([job, fresh])
    db.session.commit()
    got = jobs.get(job.id)
    assert got.status == "failed" and "10 minutes" in got.error
    assert jobs.get(fresh.id).status == "running"


def test_stale_queued_job_is_failed_on_read():
    old = jobs.utcnow() - timedelta(minutes=15)
    job = Job(type="test_echo", payload={}, status="queued", created_at=old)
    db.session.add(job)
    db.session.commit()
    assert jobs.get(job.id).status == "failed"


def test_thread_mode_runs_in_background(monkeypatch):
    monkeypatch.setenv("JOBS_MODE", "thread")
    job = jobs.enqueue("test_echo", {"value": "bg"})
    for t in threading.enumerate():
        if t.name == f"job-{job.id}":
            t.join(timeout=10)
    db.session.expire_all()
    assert db.session.get(Job, job.id).status == "done"


def test_rq_mode_falls_back_to_a_thread_until_wired(monkeypatch):
    monkeypatch.setenv("JOBS_MODE", "rq")
    job = jobs.enqueue("test_echo", {"value": "rq"})
    for t in threading.enumerate():
        if t.name == f"job-{job.id}":
            t.join(timeout=10)
    db.session.expire_all()
    assert db.session.get(Job, job.id).status == "done"
