import enum
import uuid
from datetime import datetime

from sqlalchemy import BigInteger, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.database.session import Base


class JobStatus(str, enum.Enum):
    QUEUED = "QUEUED"
    PROCESSING = "PROCESSING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class Operation(str, enum.Enum):
    TRANSCODE = "transcode"


class VideoCodec(str, enum.Enum):
    H264 = "h264"
    H265 = "h265"


class CompressionPreset(str, enum.Enum):
    HIGH = "high"
    BALANCED = "balanced"
    SMALL = "small"


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    input_path: Mapped[str] = mapped_column(Text, nullable=False)
    output_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    thumbnail_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    operation: Mapped[str] = mapped_column(String(32), nullable=False)
    target_resolution: Mapped[str | None] = mapped_column(String(16), nullable=True)
    video_codec: Mapped[str] = mapped_column(
        String(16), nullable=False, default=VideoCodec.H264.value
    )
    compression_preset: Mapped[str] = mapped_column(
        String(16), nullable=False, default=CompressionPreset.BALANCED.value
    )
    input_size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    output_size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default=JobStatus.QUEUED.value)
    progress: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, default=datetime.utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    @property
    def thumbnail_available(self) -> bool:
        return bool(self.thumbnail_path)
