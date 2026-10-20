from pathlib import Path
from typing import Annotated

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import FileResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import Settings, get_settings
from app.database.session import get_db
from app.models.job import CompressionPreset, Job, JobStatus, Operation, VideoCodec
from app.schemas.job import JobCreated, JobRead
from app.services.storage import delete_job_files, save_upload
from app.services.tasks import enqueue_job
from app.services.metrics import JOB_CREATIONS

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


def get_job_or_404(job_id: str, db: Session) -> Job:
    job = db.get(Job, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@router.post("", response_model=JobCreated, status_code=status.HTTP_202_ACCEPTED)
def create_job(
    file: UploadFile = File(...),
    operation: Operation = Form(...),
    target_resolution: str = Form(...),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    video_codec: VideoCodec = Form(VideoCodec.H264),
    compression_preset: CompressionPreset = Form(CompressionPreset.BALANCED),
    demonstrate_failure: bool = Form(False),
    authorization: str | None = Header(default=None),
):
    demonstrate_failure = demonstrate_failure is True
    if demonstrate_failure:
        import hmac

        token = settings.aiops_operator_token.get_secret_value()
        if (
            not settings.aiops_video_enabled
            or not settings.aiops_video_demo_enabled
            or len(token) < 32
            or not hmac.compare_digest(authorization or "", "Bearer " + token)
        ):
            raise HTTPException(403, "Operator authorization required for failure demonstration")
    if operation != Operation.TRANSCODE or target_resolution not in {"720p", "480p"}:
        raise HTTPException(status_code=422, detail="Only 720p or 480p transcoding is supported")
    input_path = save_upload(file, settings)
    job = Job(
        original_filename=Path(file.filename or "video.mp4").name,
        input_path=str(input_path),
        operation=operation.value,
        target_resolution=target_resolution,
        video_codec=video_codec.value,
        compression_preset=compression_preset.value,
        input_size_bytes=input_path.stat().st_size,
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    JOB_CREATIONS.labels(
        codec=video_codec.value,
        compression_preset=compression_preset.value,
        resolution=target_resolution,
    ).inc()
    try:
        if demonstrate_failure:
            enqueue_job(job.id, demonstrate_failure=True)
        else:
            enqueue_job(job.id)
    except Exception as exc:
        # The file/record remain visible, with a meaningful failed state, if Redis is unavailable.
        job.status = JobStatus.FAILED.value
        job.error_message = "Unable to queue job"
        if settings.aiops_video_enabled:
            import json, os, uuid
            from datetime import datetime
            from app.models.video_integration import VideoExecution
            from app.services.logging import redact

            error = redact(exc, 1200)
            kind = (
                "REDIS_CONNECTIVITY"
                if any(
                    s in error
                    for s in [
                        "Name or service not known",
                        "Connection refused",
                        "Error 111",
                        "Error -2",
                    ]
                )
                else "UNKNOWN"
            )
            db.add(
                VideoExecution(
                    task_id=getattr(exc, "video_task_id", str(uuid.uuid4())),
                    job_id=job.id,
                    status="failed",
                    pod=os.getenv("POD_NAME", os.getenv("HOSTNAME", "")),
                    pod_uid=os.getenv("POD_UID", ""),
                    started_at=job.created_at,
                    finished_at=datetime.utcnow(),
                    details_json=json.dumps(
                        {"error": error, "error_kind": kind, "stage": "enqueue"}
                    ),
                )
            )
        db.commit()
        raise HTTPException(status_code=503, detail="Unable to queue job")
    return JobCreated(id=job.id, status=job.status)


@router.get("", response_model=list[JobRead])
def list_jobs(
    db: Session = Depends(get_db),
    limit: Annotated[int | None, Query(ge=1, le=100)] = None,
    offset: Annotated[int, Query(ge=0)] = 0,
):
    # Keep the array contract for existing clients; the UI requests bounded pages.
    query = select(Job).order_by(Job.created_at.desc(), Job.id.desc()).offset(offset)
    if limit is not None:
        query = query.limit(limit)
    return db.scalars(query).all()


@router.get("/{job_id}", response_model=JobRead)
def get_job(job_id: str, db: Session = Depends(get_db)):
    return get_job_or_404(job_id, db)


def ranged_file_response(
    path: Path, request: Request, media_type: str, filename: str | None = None
):
    file_size = path.stat().st_size
    range_header = request.headers.get("range")
    headers = {"Accept-Ranges": "bytes"}
    if not range_header:
        return FileResponse(path, media_type=media_type, filename=filename, headers=headers)
    try:
        units, raw_range = range_header.split("=", 1)
        start_text, end_text = raw_range.split("-", 1)
        if units != "bytes":
            raise ValueError
        start = int(start_text) if start_text else 0
        end = int(end_text) if end_text else file_size - 1
        if start < 0 or end < start or start >= file_size:
            raise ValueError
        end = min(end, file_size - 1)
    except ValueError:
        raise HTTPException(status_code=416, detail="Invalid Range header")

    def stream():
        with path.open("rb") as source:
            source.seek(start)
            yield source.read(end - start + 1)

    headers.update(
        {
            "Content-Range": f"bytes {start}-{end}/{file_size}",
            "Content-Length": str(end - start + 1),
        }
    )
    return StreamingResponse(stream(), status_code=206, media_type=media_type, headers=headers)


def completed_output(job_id: str, db: Session) -> Job:
    job = get_job_or_404(job_id, db)
    if (
        job.status != JobStatus.COMPLETED.value
        or not job.output_path
        or not Path(job.output_path).is_file()
    ):
        raise HTTPException(status_code=404, detail="Processed video is not available")
    return job


@router.get("/{job_id}/video")
def play_video(job_id: str, request: Request, db: Session = Depends(get_db)):
    job = completed_output(job_id, db)
    return ranged_file_response(Path(job.output_path), request, "video/mp4")


@router.get("/{job_id}/download")
def download_video(job_id: str, request: Request, db: Session = Depends(get_db)):
    job = completed_output(job_id, db)
    return ranged_file_response(
        Path(job.output_path), request, "video/mp4", filename=f"processed-{job.original_filename}"
    )


@router.get("/{job_id}/thumbnail")
def get_thumbnail(job_id: str, db: Session = Depends(get_db)):
    job = get_job_or_404(job_id, db)
    if (
        job.status != JobStatus.COMPLETED.value
        or not job.thumbnail_path
        or not Path(job.thumbnail_path).is_file()
    ):
        raise HTTPException(status_code=404, detail="Thumbnail is not available")
    return FileResponse(job.thumbnail_path, media_type="image/jpeg")


@router.delete("/{job_id}", status_code=204)
def delete_job(job_id: str, db: Session = Depends(get_db)):
    job = get_job_or_404(job_id, db)
    if job.status not in {JobStatus.COMPLETED.value, JobStatus.FAILED.value}:
        raise HTTPException(status_code=409, detail="Only completed or failed jobs can be deleted")
    delete_job_files(job.input_path, job.output_path, job.thumbnail_path)
    db.delete(job)
    db.commit()
