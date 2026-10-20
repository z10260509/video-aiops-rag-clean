"""Server approval bindings and one-attempt action ledger; no workload changes."""

from alembic import op
import sqlalchemy as sa

revision = "20261010_14"
down_revision = "20261010_13"
branch_labels = depends_on = None


def upgrade():
    op.create_table(
        "aiops_actions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "run_id", sa.String(36), sa.ForeignKey("diagnosis_runs.id"), nullable=False, unique=True
        ),
        sa.Column(
            "incident_id", sa.String(36), sa.ForeignKey("incidents.id"), nullable=False, index=True
        ),
        *[sa.Column(name, sa.Text(), nullable=False) for name in ["target_json", "params_json"]],
        *[
            sa.Column(name, sa.String(64), nullable=False)
            for name in ["action_type", "binding_hash"]
        ],
        sa.Column("policy_version", sa.String(32), nullable=False),
        sa.Column("idempotency_key", sa.String(64), nullable=False, unique=True),
        sa.Column("approval_status", sa.String(16), nullable=False),
        sa.Column("execution_status", sa.String(16), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("expires_at", sa.DateTime(), nullable=False),
        sa.Column("started_at", sa.DateTime()),
        sa.Column("finished_at", sa.DateTime()),
        sa.Column("result_json", sa.Text()),
    )
    op.create_table(
        "aiops_approvals",
        sa.Column("action_id", sa.String(36), sa.ForeignKey("aiops_actions.id"), primary_key=True),
        sa.Column("principal", sa.String(128), nullable=False),
        sa.Column("decision", sa.String(16), nullable=False),
        sa.Column("reason", sa.String(1000), nullable=False),
        sa.Column("binding_hash", sa.String(64), nullable=False),
        sa.Column("decided_at", sa.DateTime(), nullable=False),
    )


def downgrade():
    op.drop_table("aiops_approvals")
    op.drop_table("aiops_actions")
