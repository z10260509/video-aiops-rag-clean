"""Preserve every manual request key even when requests coalesce into one run."""

from alembic import op
import sqlalchemy as sa

revision = "20261010_11"
down_revision = "20261010_10"
branch_labels = depends_on = None


def upgrade():
    op.create_table(
        "aiops_dispatch_requests",
        sa.Column("key", sa.String(128), primary_key=True),
        sa.Column("incident_id", sa.String(36), sa.ForeignKey("incidents.id"), nullable=False),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("diagnosis_runs.id"), nullable=False),
    )


def downgrade():
    op.drop_table("aiops_dispatch_requests")
