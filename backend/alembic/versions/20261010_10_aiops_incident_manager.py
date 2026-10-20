"""Add Alertmanager episodes, evidence, lifecycle and durable dispatch metadata."""

from alembic import op
import sqlalchemy as sa

revision = "20261010_10"
down_revision = "20261010_09"
branch_labels = depends_on = None


def upgrade():
    for column in [
        sa.Column("namespace", sa.String(63)),
        sa.Column("resource_kind", sa.String(32)),
        sa.Column("alert_fingerprint", sa.String(64)),
        sa.Column("severity", sa.String(16), server_default="warning", nullable=False),
        sa.Column("lifecycle", sa.String(32), server_default="DETECTED", nullable=False),
        sa.Column("resolved_at", sa.DateTime()),
        sa.Column("version", sa.Integer(), server_default="1", nullable=False),
    ]:
        op.add_column("incidents", column)
    op.execute("UPDATE incidents SET lifecycle='RESOLVED' WHERE status != 'open'")
    op.add_column(
        "diagnosis_runs",
        sa.Column("workflow_kind", sa.String(32), server_default="legacy", nullable=False),
    )
    op.add_column("diagnosis_runs", sa.Column("request_key", sa.String(128)))
    op.create_index("uq_diagnosis_request_key", "diagnosis_runs", ["request_key"], unique=True)
    op.create_table(
        "aiops_alerts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("incident_id", sa.String(36), sa.ForeignKey("incidents.id"), nullable=False),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("labels_json", sa.Text(), nullable=False),
        sa.Column("annotations_json", sa.Text(), nullable=False),
        sa.Column("starts_at", sa.DateTime(), nullable=False),
        sa.Column("ends_at", sa.DateTime()),
        sa.Column("received_at", sa.DateTime(), nullable=False),
        sa.UniqueConstraint("fingerprint", "starts_at", name="uq_aiops_alert_episode"),
    )
    op.create_index("ix_aiops_alerts_incident_id", "aiops_alerts", ["incident_id"])
    op.create_table(
        "aiops_timeline",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("incident_id", sa.String(36), sa.ForeignKey("incidents.id"), nullable=False),
        sa.Column("event_key", sa.String(128), unique=True, nullable=False),
        sa.Column("actor", sa.String(128), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_index("ix_aiops_timeline_incident_id", "aiops_timeline", ["incident_id"])
    op.create_table(
        "aiops_evidence",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("incident_id", sa.String(36), sa.ForeignKey("incidents.id"), nullable=False),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("diagnosis_runs.id")),
        sa.Column("kind", sa.String(64), nullable=False),
        sa.Column("source", sa.String(128), nullable=False),
        sa.Column("requested_at", sa.DateTime(), nullable=False),
        sa.Column("observed_at", sa.DateTime(), nullable=False),
        sa.Column("query_json", sa.Text(), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("checksum", sa.String(64), nullable=False),
        sa.Column("truncated", sa.Boolean(), nullable=False),
        sa.Column("errors_json", sa.Text(), nullable=False),
    )
    op.create_index("ix_aiops_evidence_incident_id", "aiops_evidence", ["incident_id"])
    op.create_index("ix_aiops_evidence_run_id", "aiops_evidence", ["run_id"])


def downgrade():
    for table in ["aiops_evidence", "aiops_timeline", "aiops_alerts"]:
        op.drop_table(table)
    op.drop_index("uq_diagnosis_request_key", table_name="diagnosis_runs")
    op.drop_column("diagnosis_runs", "request_key")
    op.drop_column("diagnosis_runs", "workflow_kind")
    for name in [
        "version",
        "resolved_at",
        "lifecycle",
        "severity",
        "alert_fingerprint",
        "resource_kind",
        "namespace",
    ]:
        op.drop_column("incidents", name)
