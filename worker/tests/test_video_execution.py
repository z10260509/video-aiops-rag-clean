import json
import uuid
from unittest.mock import Mock
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from app.core.config import Settings
from app.database.session import Base
from app.models.job import Job
from app.models.observability import VideoTaskOutcome
from app.models.video_integration import VideoExecution, VideoRepair
from app.aiops.incidents import now
from worker.app import tasks


@pytest.fixture
def worker(tmp_path, monkeypatch):
    engine = create_engine("sqlite:///" + str(tmp_path / "worker.db"))
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine)
    monkeypatch.setattr(tasks, "SessionLocal", factory)
    monkeypatch.setenv("STORAGE_ROOT", str(tmp_path))
    monkeypatch.setenv("POD_NAME", "worker-real")
    monkeypatch.setenv("POD_UID", str(uuid.uuid4()))
    monkeypatch.setattr(
        tasks, "get_settings", lambda: Settings(_env_file=None, aiops_video_enabled=True)
    )
    monkeypatch.setattr(tasks, "create_thumbnail", lambda *_: None)
    with factory() as db:
        job = Job(
            original_filename="uploaded.mp4",
            input_path=str(tmp_path / "my.mp4"),
            operation="transcode",
            status="QUEUED",
            video_codec="h264",
        )
        db.add(job)
        db.commit()
        jid = job.id

    def transcode(_source, output, *_):
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_bytes(b"output")

    monkeypatch.setattr(tasks, "transcode_video", transcode)
    return factory, jid


def test_integration_records_failure_and_same_video_retry(worker, monkeypatch):
    factory, jid = worker
    old = str(uuid.uuid4())
    new = str(uuid.uuid4())

    def broken(*_):
        raise RuntimeError("Unknown encoder 'broken'")

    tasks.run_job(jid, old, transcode=broken)
    with factory() as db:
        assert db.get(Job, jid).status == "FAILED"
        original = db.get(VideoExecution, old)
        assert json.loads(original.details_json)["error_kind"] == "FFMPEG_INVALID_CODEC"
        db.add(
            VideoRepair(
                action_id=str(uuid.uuid4()),
                job_id=jid,
                failed_task_id=old,
                task_id=new,
                root_cause="FFMPEG_INVALID_CODEC",
                created_at=now(),
            )
        )
        db.get(Job, jid).status = "QUEUED"
        db.commit()
    # Redelivered original message cannot steal the reviewed retry.
    tasks.run_job(jid, old)
    with factory() as db:
        assert db.get(Job, jid).status == "QUEUED"
    tasks.run_job(jid, new)
    with factory() as db:
        assert (
            db.get(Job, jid).status == "COMPLETED"
            and db.get(Job, jid).original_filename == "uploaded.mp4"
        )
        assert (
            db.get(VideoExecution, new).status == "completed"
            and db.get(VideoExecution, new).pod == "worker-real"
        )
        assert db.get(VideoTaskOutcome, new).status == "completed"


def test_worker_does_not_enable_fault_from_task_kwarg_without_feature_flags(worker):
    with pytest.raises(PermissionError):
        tasks.process_transcode.run(worker[1], demo_invalid_codec=True)
