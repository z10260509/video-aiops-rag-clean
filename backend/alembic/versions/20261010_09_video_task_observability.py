"""Durable, replay-safe video task observations; no changes to existing tables."""

from alembic import op
import sqlalchemy as sa

revision = "20261010_09"
down_revision = "20260929_08"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "video_task_outcomes",
        sa.Column("task_id", sa.String(128), primary_key=True),
        sa.Column("job_id", sa.String(36), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("started_at", sa.DateTime(), nullable=False),
        sa.Column("finished_at", sa.DateTime(), nullable=False),
        sa.Column("duration_seconds", sa.Float(), nullable=False),
        sa.CheckConstraint("status IN ('completed', 'failed')", name="ck_video_outcome_status"),
        sa.CheckConstraint("duration_seconds >= 0", name="ck_video_outcome_duration"),
    )
    op.create_index("ix_video_task_outcomes_finished_at", "video_task_outcomes", ["finished_at"])


def downgrade() -> None:
    op.drop_table("video_task_outcomes")
