import logging
import os
import time
from datetime import datetime
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from worker.app.celery_app import celery_app
from worker.app.ffmpeg_service import create_thumbnail, transcode_video

# The worker shares the backend model/database contract, but does not run the API.
from app.models.job import Job, JobStatus
from app.core.config import get_settings
from app.observability.outcomes import record_outcome
from app.services.logging import redact

logger = logging.getLogger(__name__)
engine = create_engine(
    os.getenv("DATABASE_URL", "postgresql+psycopg://video:video@localhost:5432/video_platform"),
    pool_pre_ping=True,
)
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)


@celery_app.task(name="worker.process_transcode", bind=True)
def process_transcode(self, job_id: str, demo_invalid_codec: bool = False) -> None:
    transcode = None
    if demo_invalid_codec:
        if not get_settings().aiops_video_enabled or not get_settings().aiops_video_demo_enabled:
            raise PermissionError("Uploaded-video demonstration disabled")

        def invalid_encoder(input_path, output_path, *_options):
            import subprocess

            output_path.parent.mkdir(parents=True, exist_ok=True)
            result = subprocess.run(
                [
                    "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-i",
                    str(input_path),
                    "-c:v",
                    "aiops_invalid_encoder",
                    "-t",
                    "1",
                    str(output_path),
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if result.returncode != 0:
                raise RuntimeError(result.stderr)
            raise RuntimeError("Expected invalid encoder failure not observed")

        transcode = invalid_encoder
    run_job(job_id, self.request.id, transcode=transcode)


def run_job(job_id: str, task_id: str | None, *, transcode=None, run_id: str | None = None) -> None:
    """Shared workload execution; the public task retains its original contract."""
    metrics_enabled = get_settings().video_metrics_enabled
    integration_enabled = get_settings().aiops_video_enabled
    if integration_enabled and not task_id:
        raise ValueError("Video integration requires a task ID")
    if metrics_enabled and not task_id:
        raise ValueError("Metrics-enabled execution requires a Celery task ID")
    correlation = {"job_id": job_id, "task_id": task_id or "", "run_id": run_id or ""}
    with SessionLocal() as db:
        job = db.scalar(select(Job).where(Job.id == job_id).with_for_update())
        if not job or job.status != JobStatus.QUEUED.value:
            return
        job.status = JobStatus.PROCESSING.value
        job.progress = 1
        job.started_at = datetime.utcnow()
        if integration_enabled:
            from app.services.video_execution import begin_execution

            if not begin_execution(db, job, task_id):
                db.rollback()
                return
        db.commit()
        started = time.monotonic()
        logger.info("processing_started", extra={**correlation, "event": "processing_started"})

        error = None
        try:
            output_root = Path(os.getenv("STORAGE_ROOT", "storage")) / "output"
            output_path = output_root / f"{job.id}.mp4"
            (transcode or transcode_video)(
                Path(job.input_path),
                output_path,
                job.target_resolution or "",
                job.video_codec,
                job.compression_preset,
            )
            thumbnail_path = (
                Path(os.getenv("STORAGE_ROOT", "storage")) / "thumbnails" / f"{job.id}.jpg"
            )
            create_thumbnail(output_path, thumbnail_path)
            job.output_path = str(output_path)
            job.thumbnail_path = str(thumbnail_path)
            job.output_size_bytes = output_path.stat().st_size
            job.status = JobStatus.COMPLETED.value
            job.progress = 100
            job.completed_at = datetime.utcnow()
        except Exception as exc:
            error = exc
            job.status = JobStatus.FAILED.value
            job.error_message = str(exc)[:4000]
            job.completed_at = datetime.utcnow()
        if integration_enabled:
            from app.services.video_execution import finish_execution

            finish_execution(db, task_id, job, error)
        if metrics_enabled or integration_enabled:
            record_outcome(
                db,
                task_id=task_id,
                job_id=job.id,
                status="failed" if error else "completed",
                started_at=job.started_at,
                finished_at=job.completed_at,
                duration_seconds=time.monotonic() - started,
            )
        try:
            db.commit()
        except Exception:
            db.rollback()
            logger.error(
                "terminal_commit_failed", extra={**correlation, "event": "terminal_commit_failed"}
            )
            raise
        event = "processing_failed" if error else "processing_completed"
        details = {**correlation, "event": event, "status": job.status}
        if error:
            details.update(error_kind=type(error).__name__, error=redact(error))
        logger.log(logging.ERROR if error else logging.INFO, event, extra=details)
