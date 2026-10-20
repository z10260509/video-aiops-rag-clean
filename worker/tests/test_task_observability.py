import logging
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.database.session import Base
from app.core.config import Settings
from app.models.job import Job
from app.models.observability import VideoTaskOutcome
from worker.app import tasks


@pytest.fixture
def setup(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'worker.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setattr(tasks, "SessionLocal", factory)
    monkeypatch.setattr(
        tasks, "get_settings", lambda: Settings(_env_file=None, video_metrics_enabled=True)
    )
    monkeypatch.setenv("STORAGE_ROOT", str(tmp_path))
    with factory() as db:
        job = Job(
            original_filename="sample.mp4",
            input_path=str(tmp_path / "input.mp4"),
            operation="transcode",
            target_resolution="480p",
            video_codec="h264",
            compression_preset="balanced",
        )
        db.add(job)
        db.commit()
        job_id = job.id

    def transcode(_input, output, *_options):
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"video")

    monkeypatch.setattr(tasks, "transcode_video", transcode)
    monkeypatch.setattr(tasks, "create_thumbnail", lambda *_args: None)
    yield factory, job_id
    engine.dispose()


@pytest.mark.parametrize("fail", [False, True])
def test_state_outcome_and_log_are_correlated_once(setup, monkeypatch, caplog, fail):
    factory, job_id = setup
    if fail:

        def broken(*_args):
            raise RuntimeError("codec failed password=hidden")

        monkeypatch.setattr(tasks, "transcode_video", broken)
    with caplog.at_level(logging.INFO):
        tasks.run_job(job_id, "task-1")
        tasks.run_job(job_id, "task-1")
    with factory() as db:
        assert db.get(Job, job_id).status == ("FAILED" if fail else "COMPLETED")
        assert db.scalar(select(func.count()).select_from(VideoTaskOutcome)) == 1
        assert db.get(VideoTaskOutcome, "task-1").status == ("failed" if fail else "completed")
    terminal = [
        r
        for r in caplog.records
        if getattr(r, "event", "") in {"processing_failed", "processing_completed"}
    ]
    assert len(terminal) == 1 and terminal[0].job_id == job_id and terminal[0].task_id == "task-1"
    assert "hidden" not in getattr(terminal[0], "error", "")


def test_terminal_commit_failure_does_not_publish_success(setup, monkeypatch, caplog):
    factory, job_id = setup

    def broken_stage(*args, **kwargs):
        raise RuntimeError("simulated outcome storage failure")

    monkeypatch.setattr(tasks, "record_outcome", broken_stage)
    with pytest.raises(RuntimeError), caplog.at_level(logging.INFO):
        tasks.run_job(job_id, "task-1")
    with factory() as db:
        assert db.get(Job, job_id).status == "PROCESSING"
        assert db.scalar(select(func.count()).select_from(VideoTaskOutcome)) == 0
    assert not any(getattr(r, "event", "") == "processing_completed" for r in caplog.records)


def test_sql_commit_failure_rolls_back_terminal_outcome_and_job(setup, monkeypatch, caplog):
    factory, job_id = setup
    from sqlalchemy.orm import Session

    class FailingSession(Session):
        commits = 0

        def commit(self):
            self.commits += 1
            if self.commits == 2:
                raise RuntimeError("simulated terminal commit failure")
            return super().commit()

    monkeypatch.setattr(
        tasks, "SessionLocal", sessionmaker(bind=factory.kw["bind"], class_=FailingSession)
    )
    with pytest.raises(RuntimeError), caplog.at_level(logging.INFO):
        tasks.run_job(job_id, "task-1")
    with factory() as db:
        assert db.get(Job, job_id).status == "PROCESSING"
        assert db.scalar(select(func.count()).select_from(VideoTaskOutcome)) == 0
    assert any(getattr(r, "event", "") == "terminal_commit_failed" for r in caplog.records)
    assert not any(getattr(r, "event", "") == "processing_completed" for r in caplog.records)


def test_metrics_disabled_does_not_require_new_schema_or_task_id(setup, monkeypatch):
    factory, job_id = setup
    monkeypatch.setattr(
        tasks, "get_settings", lambda: Settings(_env_file=None, video_metrics_enabled=False)
    )
    with factory() as db:
        VideoTaskOutcome.__table__.drop(db.get_bind())
    tasks.run_job(job_id, None)
    with factory() as db:
        assert db.get(Job, job_id).status == "COMPLETED"
