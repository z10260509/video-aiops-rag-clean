"""Durable terminal observations; deliberately survive deletion of media jobs."""

from datetime import datetime

from sqlalchemy import CheckConstraint, DateTime, Float, String
from sqlalchemy.orm import Mapped, mapped_column

from app.database.session import Base


class VideoTaskOutcome(Base):
    __tablename__ = "video_task_outcomes"
    __table_args__ = (
        CheckConstraint("status IN ('completed', 'failed')", name="ck_video_outcome_status"),
        CheckConstraint("duration_seconds >= 0", name="ck_video_outcome_duration"),
    )
    task_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    job_id: Mapped[str] = mapped_column(String(36), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    finished_at: Mapped[datetime] = mapped_column(DateTime, nullable=False, index=True)
    duration_seconds: Mapped[float] = mapped_column(Float, nullable=False)
