"""Private, fixed video evidence and repair API. Authorization originates in AIOps."""

import hmac
import json
import subprocess
import uuid
from datetime import datetime
from pathlib import Path
from typing import Literal
from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.core.config import get_settings
from app.database.session import get_db
from app.models.job import Job
from app.models.observability import VideoTaskOutcome
from app.models.video_integration import VideoExecution, VideoRepair
from app.services.tasks import celery_app

router = APIRouter(prefix="/internal/aiops", tags=["private-video-integration"])


def identity(kind, authorization):
    settings = get_settings()
    token = getattr(settings, "aiops_video_" + kind + "_token").get_secret_value()
    if (
        not settings.aiops_video_enabled
        or len(token) < 32
        or not hmac.compare_digest(authorization or "", "Bearer " + token)
    ):
        raise HTTPException(401, "Video integration identity required")


def reader(authorization: str | None = Header(default=None)):
    identity("reader", authorization)


def executor(authorization: str | None = Header(default=None)):
    identity("repair", authorization)


def verifier(authorization: str | None = Header(default=None)):
    settings = get_settings()
    tokens = [
        settings.aiops_video_reader_token.get_secret_value(),
        settings.aiops_video_repair_token.get_secret_value(),
    ]
    if not settings.aiops_video_enabled or not any(
        len(t) >= 32 and hmac.compare_digest(authorization or "", "Bearer " + t) for t in tokens
    ):
        raise HTTPException(401, "Video integration identity required")


def video_path(value, directory):
    root = get_settings().storage_root.resolve() / directory
    path = Path(value or "").resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise HTTPException(409, "Retained video file unavailable")
    return path


def execution_json(row, job):
    details = json.loads(row.details_json)
    details.pop("encoder_inventory", None)  # Inventory has its own bounded probe endpoint.
    return {
        **details,
        "task_id": row.task_id,
        "job_id": row.job_id,
        "execution_status": row.status,
        "started_at": row.started_at.isoformat() + "Z",
        "finished_at": row.finished_at.isoformat() + "Z" if row.finished_at else None,
        "pod": row.pod,
        "pod_uid": row.pod_uid,
        "job_present": bool(job),
        "job_status": job.status if job else None,
        "original_filename": job.original_filename if job else None,
        "video_codec": job.video_codec if job else None,
    }


@router.get("/failures", dependencies=[Depends(reader)])
def failures(db: Session = Depends(get_db)):
    rows = db.execute(
        select(VideoExecution, Job)
        .join(Job, Job.id == VideoExecution.job_id)
        .where(
            Job.status.in_(["FAILED", "PROCESSING"]),
            VideoExecution.status.in_(["failed", "running"]),
        )
        .order_by(VideoExecution.started_at.desc())
        .limit(20)
    ).all()
    return {"items": [execution_json(row, job) for row, job in rows]}


@router.get("/jobs/{job_id}", dependencies=[Depends(reader)])
def evidence(job_id: uuid.UUID, task_id: uuid.UUID | None = None, db: Session = Depends(get_db)):
    job = db.get(Job, str(job_id))
    row = (
        db.get(VideoExecution, str(task_id))
        if task_id
        else db.scalar(
            select(VideoExecution)
            .where(VideoExecution.job_id == str(job_id))
            .order_by(VideoExecution.started_at.desc())
            .limit(1)
        )
    )
    if row and row.job_id != str(job_id):
        raise HTTPException(409, "Task belongs to another video")
    if not row:
        return {"found": False, "job_present": bool(job), "job_status": job.status if job else None}
    latest = db.scalar(
        select(VideoExecution)
        .where(VideoExecution.job_id == str(job_id))
        .order_by(VideoExecution.started_at.desc())
        .limit(1)
    )
    try:
        input_available = bool(job and video_path(job.input_path, "input"))
    except HTTPException:
        input_available = False
    reserved = db.scalar(select(VideoRepair).where(VideoRepair.failed_task_id == row.task_id))
    return {
        "found": True,
        **execution_json(row, job),
        "input_available": input_available,
        "latest_task_id": latest.task_id,
        "retry_reserved": bool(reserved),
    }


class Retry(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action_id: uuid.UUID
    job_id: uuid.UUID
    failed_task_id: uuid.UUID
    root_cause: Literal["FFMPEG_INVALID_CODEC", "REDIS_CONNECTIVITY", "WORKER_OOM"]
    video_codec: Literal["h264", "h265"]


def eligible(db, request):
    job = db.get(Job, str(request.job_id), with_for_update=True)
    row = db.get(VideoExecution, str(request.failed_task_id))
    if not job or not row or row.job_id != job.id:
        raise HTTPException(409, "Original task and video required")
    latest = db.scalar(
        select(VideoExecution)
        .where(VideoExecution.job_id == job.id)
        .order_by(VideoExecution.started_at.desc())
        .limit(1)
    )
    if latest.task_id != row.task_id or db.scalar(
        select(VideoRepair).where(VideoRepair.failed_task_id == row.task_id)
    ):
        raise HTTPException(409, "Task changed or retry already reserved")
    details = json.loads(row.details_json)
    interrupted = (
        request.root_cause == "WORKER_OOM"
        and row.status == "running"
        and job.status == "PROCESSING"
    )
    if not interrupted and (
        row.status != "failed"
        or job.status != "FAILED"
        or details.get("error_kind") != request.root_cause
    ):
        raise HTTPException(409, "Failure no longer supports this repair")
    if request.video_codec != (
        "h264" if request.root_cause == "FFMPEG_INVALID_CODEC" else job.video_codec
    ):
        raise HTTPException(409, "Codec does not match bound repair")
    video_path(job.input_path, "input")
    return job, row


@router.post("/preflight", dependencies=[Depends(executor)])
def preflight(request: Retry, db: Session = Depends(get_db)):
    eligible(db, request)
    return {"eligible": True}


@router.post("/repair", dependencies=[Depends(executor)])
def repair(request: Retry, db: Session = Depends(get_db)):
    job, failed = eligible(db, request)
    task = str(uuid.uuid5(uuid.NAMESPACE_URL, "video-repair:" + str(request.action_id)))
    at = datetime.utcnow()
    # Reservation and job state commit precede publishing. Lost acknowledgements are not replayed.
    db.add(
        VideoRepair(
            action_id=str(request.action_id),
            job_id=job.id,
            failed_task_id=failed.task_id,
            task_id=task,
            root_cause=request.root_cause,
            created_at=at,
        )
    )
    if failed.status == "running":
        failed.status = "failed"
        failed.finished_at = at
        details = json.loads(failed.details_json)
        details.update(
            error_kind="WORKER_OOM",
            error="Original worker execution interrupted; recovery authorized with verified Kubernetes OOM evidence",
        )
        failed.details_json = json.dumps(details)
    job.video_codec = request.video_codec
    job.status = "QUEUED"
    job.progress = 0
    job.error_message = None
    job.started_at = None
    job.completed_at = None
    db.commit()
    celery_app.send_task("worker.process_transcode", args=[str(request.job_id)], task_id=task)
    return {"accepted": True, "action_id": str(request.action_id), "task_id": task}


@router.get("/verify/{action_id}", dependencies=[Depends(verifier)])
def verify(action_id: uuid.UUID, db: Session = Depends(get_db)):
    receipt = db.get(VideoRepair, str(action_id))
    if not receipt:
        raise HTTPException(404, "Repair not recorded")
    job = db.get(Job, receipt.job_id)
    outcome = db.get(VideoTaskOutcome, receipt.task_id)
    row = db.get(VideoExecution, receipt.task_id)
    if (
        not job
        or not row
        or not outcome
        or job.status != "COMPLETED"
        or row.status != "completed"
        or outcome.status != "completed"
        or outcome.job_id != job.id
    ):
        raise HTTPException(409, "Original video retry has not completed")
    output = video_path(job.output_path, "output")
    if (
        output.stat().st_mtime < receipt.created_at.timestamp() - 1
        or row.started_at < receipt.created_at
    ):
        raise HTTPException(409, "Fresh output required")
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=codec_name",
            "-of",
            "json",
            str(output),
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=5,
    )
    codec = json.loads(result.stdout)["streams"][0]["codec_name"]
    if codec != {"h264": "h264", "h265": "hevc"}[job.video_codec]:
        raise HTTPException(409, "Output codec mismatch")
    return {
        "action_id": receipt.action_id,
        "job_id": job.id,
        "task_id": receipt.task_id,
        "sample_status": "COMPLETED",
        "new_outcome_completed": True,
        "output_codec": codec,
        "pod_name": row.pod,
        "pod_uid": row.pod_uid,
        "details": json.loads(row.details_json),
        "started_at_epoch": row.started_at.timestamp(),
        "finished_at_epoch": row.finished_at.timestamp(),
        "play_url": "/video/api/jobs/" + job.id + "/video",
        "download_url": "/video/api/jobs/" + job.id + "/download",
    }


@router.get("/jobs/{job_id}/probe/{probe}", dependencies=[Depends(reader)])
def inspect_original_video(
    job_id: uuid.UUID,
    probe: Literal["input", "encoders"],
    task_id: uuid.UUID,
    db: Session = Depends(get_db),
):
    """Fixed read-only probes. Never accept paths, commands, encoders or remote URLs."""
    job = db.get(Job, str(job_id))
    execution = db.get(VideoExecution, str(task_id))
    if not job or not execution or execution.job_id != job.id:
        raise HTTPException(404, "Original video attempt not found")
    identity = {
        "job_id": job.id,
        "task_id": execution.task_id,
        "pod_uid": execution.pod_uid,
        "execution_started_at": execution.started_at.isoformat() + "Z",
    }
    if probe == "encoders":
        inventory = json.loads(execution.details_json).get("encoder_inventory")
        if not inventory or inventory.get("available") is not True:
            raise HTTPException(
                409,
                "Original worker encoder snapshot unavailable; deploy v2 worker for future attempts",
            )
        import re

        names = inventory.get("encoders", [])
        error = json.loads(execution.details_json).get("error") or ""
        match = re.search(r"Unknown encoder ['\"]([a-zA-Z0-9_]{1,80})['\"]", error)
        requested = match.group(1) if match else None
        selected = {"libx264", "libx265"} | ({requested} if requested else set())
        return {
            **identity,
            "probe": "encoders",
            "scope": "worker_execution_snapshot",
            "available": True,
            "observed_at": inventory.get("observed_at"),
            "encoder_count": len(names),
            "requested_encoder": requested,
            "known_encoders": {name: name in names for name in sorted(selected)},
        }
    path = video_path(job.input_path, "input")
    try:
        # Local file protocol only; playlists and network streams are excluded.
        response = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-protocol_whitelist",
                "file",
                "-select_streams",
                "v:0",
                "-show_entries",
                "stream=codec_name,width,height:format=duration",
                "-of",
                "json",
                "-f",
                "mov",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=8,
        )
        from app.services.logging import redact

        if response.returncode:
            return {
                **identity,
                "probe": "input",
                "readable": False,
                "error": redact(response.stderr, 1200),
            }
        data = json.loads(response.stdout)
        streams = data.get("streams", [])[:1]
        return {
            **identity,
            "probe": "input",
            "readable": bool(streams),
            "streams": streams,
            "duration": data.get("format", {}).get("duration"),
        }
    except (subprocess.TimeoutExpired, ValueError):
        raise HTTPException(
            503, "Input probe unavailable; timeout is not evidence of corrupt input"
        ) from None
