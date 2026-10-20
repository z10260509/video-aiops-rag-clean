"""persist grounded structured Gemma analysis

Revision ID: 20260927_05
Revises: 20260927_04
"""

from alembic import op
import sqlalchemy as sa

revision = "20260927_05"
down_revision = "20260927_04"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("diagnosis_reports", sa.Column("ai_analysis_json", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("diagnosis_reports", "ai_analysis_json")
