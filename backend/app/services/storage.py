import shutil
from pathlib import Path
from uuid import uuid4

from fastapi import HTTPException, UploadFile, status

from app.core.config import Settings

ALLOWED_MEDIA_TYPES = {"video/mp4", "application/octet-stream"}


def ensure_storage(settings: Settings) -> None:
    for directory in (settings.input_dir, settings.output_dir, settings.thumbnail_dir):
        directory.mkdir(parents=True, exist_ok=True)


def save_upload(upload: UploadFile, settings: Settings) -> Path:
    filename = Path(upload.filename or "").name
    if not filename.lower().endswith(".mp4") or upload.content_type not in ALLOWED_MEDIA_TYPES:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail="Only MP4 uploads are supported",
        )

    settings.input_dir.mkdir(parents=True, exist_ok=True)
    destination = settings.input_dir / f"{uuid4()}.mp4"
    written = 0
    try:
        with destination.open("wb") as target:
            while chunk := upload.file.read(1024 * 1024):
                written += len(chunk)
                if written > settings.max_upload_bytes:
                    raise HTTPException(
                        status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                        detail="Upload is too large",
                    )
                target.write(chunk)
    except Exception:
        destination.unlink(missing_ok=True)
        raise
    return destination


def delete_job_files(*paths: str | None) -> None:
    for value in paths:
        if value:
            Path(value).unlink(missing_ok=True)
