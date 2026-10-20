"""persist operator answers for iterative incident diagnosis

Revision ID: 20260928_07
Revises: 20260927_06
"""

from alembic import op
import sqlalchemy as sa

revision = "20260928_07"
down_revision = "20260927_06"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "human_supplements",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("incident_id", sa.String(36), sa.ForeignKey("incidents.id"), nullable=False),
        sa.Column("question", sa.String(500), nullable=False),
        sa.Column("answer", sa.Text(), nullable=False),
        sa.Column("submitted_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_human_supplements_incident_id", "human_supplements", ["incident_id"])


def downgrade() -> None:
    op.drop_index("ix_human_supplements_incident_id", table_name="human_supplements")
    op.drop_table("human_supplements")
