import io
import json
import logging
from datetime import datetime, timedelta
from unittest.mock import Mock

import pytest
from prometheus_client import CollectorRegistry, generate_latest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.database.session import Base
from app.models.observability import VideoTaskOutcome
from app.observability.outcomes import record_outcome
from app.observability.exporter import TaskCollector
from app.services.logging import JsonFormatter


@pytest.fixture
def factory(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'outcomes.db'}")
    Base.metadata.create_all(engine)
    yield sessionmaker(bind=engine)
    engine.dispose()


def stage(db, task_id="task-1", status="failed", duration=0.3):
    now = datetime(2026, 10, 10)
    record_outcome(
        db,
        task_id=task_id,
        job_id="job-1",
        status=status,
        started_at=now,
        finished_at=now + timedelta(seconds=1),
        duration_seconds=duration,
    )


def exposition(factory, client=None):
    registry = CollectorRegistry()
    registry.register(TaskCollector(factory, client or Mock(llen=Mock(return_value=3)), "celery"))
    return generate_latest(registry).decode()


def test_committed_outcomes_are_durable_and_duplicate_safe(factory):
    with factory() as db:
        stage(db)
        stage(db)
        db.commit()
    first = exposition(factory)
    assert 'video_tasks_total{status="failed"} 1.0' in first
    assert 'video_task_duration_seconds_bucket{le="0.5"} 1.0' in first
    assert 'video_task_duration_seconds_bucket{le="0.1"} 0.0' in first
    assert "video_task_duration_seconds_sum 0.3" in first
    assert "video_queue_depth 3.0" in first
    assert "job_id=" not in first and "task_id=" not in first
    # A newly constructed publisher sees the same counters; no in-memory reset.
    assert first == exposition(factory)


def test_rollback_and_conflicting_identity_do_not_add_outcomes(factory):
    with factory() as db:
        stage(db)
        db.rollback()
        assert db.scalar(select(func.count()).select_from(VideoTaskOutcome)) == 0
        stage(db)
        db.commit()
        with pytest.raises(ValueError, match="Conflicting"):
            stage(db, status="completed")
        db.rollback()
    assert 'video_tasks_total{status="failed"} 1.0' in exposition(factory)


def test_source_failures_are_explicit_and_not_fake_zero(factory):
    client = Mock(llen=Mock(side_effect=TimeoutError))
    output = exposition(factory, client)
    assert 'video_exporter_source_up{source="redis"} 0.0' in output
    assert "video_queue_depth " not in output
    broken = Mock(side_effect=RuntimeError("db unavailable"))
    output = exposition(broken)
    assert 'video_exporter_source_up{source="database"} 0.0' in output
    assert "video_tasks_total{" not in output


def test_queue_is_not_caller_selected(factory):
    with pytest.raises(ValueError, match="allowlisted"):
        TaskCollector(factory, Mock(), "other-tenant")


def test_nonfinite_duration_is_rejected(factory):
    with factory() as db, pytest.raises(ValueError, match="finite"):
        stage(db, duration=float("nan"))


def test_json_log_redacts_credentials_and_email_and_limits_bytes():
    record = logging.LogRecord(
        "worker",
        logging.ERROR,
        "",
        0,
        "password=hunter2 token=abcd Bearer abc.def.ghi postgres://user:pass@db user@example.com",
        (),
        None,
    )
    record.error = 'authorization="Bearer verysecret" ' + "x" * 10000
    record.job_id = "job-1"
    output = JsonFormatter().format(record)
    data = json.loads(output)
    assert len(output.encode()) < 8192
    for secret in ["hunter2", "abcd", "abc.def.ghi", "user:pass", "user@example.com", "verysecret"]:
        assert secret not in output
    assert data["job_id"] == "job-1" and data["timestamp"].endswith("+00:00")
