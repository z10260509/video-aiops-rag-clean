"""audited human-approved remediation actions

Revision ID: 20260927_06
Revises: 20260927_05
"""

from alembic import op
import sqlalchemy as sa

revision = "20260927_06"
down_revision = "20260927_05"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "remediation_actions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("incident_id", sa.String(36), sa.ForeignKey("incidents.id"), nullable=False),
        sa.Column("job_id", sa.String(36), sa.ForeignKey("jobs.id"), nullable=False),
        sa.Column("action_key", sa.String(64), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("approved_at", sa.DateTime(), nullable=False),
        sa.Column("completed_at", sa.DateTime()),
        sa.Column("result_json", sa.Text()),
        sa.Column("error", sa.Text()),
    )
    op.create_index("ix_remediation_actions_incident_id", "remediation_actions", ["incident_id"])
    op.create_index("ix_remediation_actions_job_id", "remediation_actions", ["job_id"])
    op.create_index(
        "uq_active_remediation_per_job",
        "remediation_actions",
        ["job_id"],
        unique=True,
        postgresql_where=sa.text("status = 'executing'"),
        sqlite_where=sa.text("status = 'executing'"),
    )


def downgrade() -> None:
    op.drop_index("uq_active_remediation_per_job", table_name="remediation_actions")
    op.drop_index("ix_remediation_actions_job_id", table_name="remediation_actions")
    op.drop_index("ix_remediation_actions_incident_id", table_name="remediation_actions")
    op.drop_table("remediation_actions")
