"""Add diagnosis reports; official checkpointer manages its own tables."""

from alembic import op
import sqlalchemy as sa

revision = "20261010_13"
down_revision = "20261010_12"
branch_labels = depends_on = None


def upgrade():
    op.create_table(
        "aiops_diagnoses",
        sa.Column("run_id", sa.String(36), sa.ForeignKey("diagnosis_runs.id"), primary_key=True),
        sa.Column(
            "incident_id", sa.String(36), sa.ForeignKey("incidents.id"), nullable=False, index=True
        ),
        sa.Column("thread_id", sa.String(128), nullable=False, unique=True),
        sa.Column("model_id", sa.String(128), nullable=False),
        sa.Column("prompt_version", sa.String(32), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("validation_status", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )


def downgrade():
    op.drop_table("aiops_diagnoses")
