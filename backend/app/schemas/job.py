from datetime import datetime

from pydantic import BaseModel, ConfigDict


class JobCreated(BaseModel):
    id: str
    status: str


class JobRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    original_filename: str
    operation: str
    target_resolution: str | None
    video_codec: str
    compression_preset: str
    input_size_bytes: int | None
    output_size_bytes: int | None
    thumbnail_available: bool
    status: str
    progress: int
    error_message: str | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
