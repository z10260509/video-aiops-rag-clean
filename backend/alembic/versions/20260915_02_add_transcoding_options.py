"""add transcoding options and media sizes

Revision ID: 20260915_02
Revises: 20260914_01
Create Date: 2026-09-15
"""

from alembic import op
import sqlalchemy as sa


revision = "20260915_02"
down_revision = "20260914_01"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "jobs",
        sa.Column("video_codec", sa.String(length=16), nullable=False, server_default="h264"),
    )
    op.add_column(
        "jobs",
        sa.Column(
            "compression_preset", sa.String(length=16), nullable=False, server_default="balanced"
        ),
    )
    op.add_column("jobs", sa.Column("input_size_bytes", sa.BigInteger(), nullable=True))
    op.add_column("jobs", sa.Column("output_size_bytes", sa.BigInteger(), nullable=True))


def downgrade() -> None:
    op.drop_column("jobs", "output_size_bytes")
    op.drop_column("jobs", "input_size_bytes")
    op.drop_column("jobs", "compression_preset")
    op.drop_column("jobs", "video_codec")
