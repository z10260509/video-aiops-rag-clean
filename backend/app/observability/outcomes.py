from datetime import datetime
import math
from sqlalchemy.orm import Session
from app.models.observability import VideoTaskOutcome


def record_outcome(
    db: Session,
    *,
    task_id: str,
    job_id: str,
    status: str,
    started_at: datetime,
    finished_at: datetime,
    duration_seconds: float,
) -> None:
    """Stage in the caller's transaction; duplicate task delivery cannot add a counter."""
    if not task_id or len(task_id) > 128 or status not in {"completed", "failed"}:
        raise ValueError("Invalid terminal observation")
    if not math.isfinite(duration_seconds):
        raise ValueError("Duration must be finite")
    values = dict(
        task_id=task_id,
        job_id=job_id,
        status=status,
        started_at=started_at,
        finished_at=finished_at,
        duration_seconds=max(0.0, duration_seconds),
    )
    dialect = db.get_bind().dialect.name
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    elif dialect == "sqlite":
        from sqlalchemy.dialects.sqlite import insert
    else:
        raise ValueError("Outcome storage requires PostgreSQL or test SQLite")
    db.execute(
        insert(VideoTaskOutcome).values(**values).on_conflict_do_nothing(index_elements=["task_id"])
    )
    stored = db.get(VideoTaskOutcome, task_id)
    if stored.job_id != job_id or stored.status != status:
        raise ValueError("Conflicting task observation")
