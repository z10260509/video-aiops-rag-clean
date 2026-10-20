"""Video database records; control database authorization remains in AIOps."""

from datetime import datetime
from sqlalchemy import DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from app.database.session import Base


class VideoExecution(Base):
    __tablename__ = "video_job_executions"
    task_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    job_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    pod: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    pod_uid: Mapped[str] = mapped_column(String(36), nullable=False, default="")
    started_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    details_json: Mapped[str] = mapped_column(Text, nullable=False, default="{}")


class VideoRepair(Base):
    __tablename__ = "video_job_repairs"
    action_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    job_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    failed_task_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)
    task_id: Mapped[str] = mapped_column(String(36), nullable=False, unique=True)
    root_cause: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
