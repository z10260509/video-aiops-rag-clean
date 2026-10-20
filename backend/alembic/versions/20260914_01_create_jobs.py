"""create jobs table

Revision ID: 20260914_01
Revises:
Create Date: 2026-09-14
"""

from alembic import op
import sqlalchemy as sa

revision = "20260914_01"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "jobs",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("original_filename", sa.String(length=255), nullable=False),
        sa.Column("input_path", sa.Text(), nullable=False),
        sa.Column("output_path", sa.Text()),
        sa.Column("thumbnail_path", sa.Text()),
        sa.Column("operation", sa.String(length=32), nullable=False),
        sa.Column("target_resolution", sa.String(length=16)),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("progress", sa.Integer(), nullable=False),
        sa.Column("error_message", sa.Text()),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("started_at", sa.DateTime()),
        sa.Column("completed_at", sa.DateTime()),
    )


def downgrade() -> None:
    op.drop_table("jobs")
