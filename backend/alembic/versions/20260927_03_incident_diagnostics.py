"""incident events and durable diagnostic work

Revision ID: 20260927_03
Revises: 20260915_02
"""

from alembic import op
import sqlalchemy as sa

revision = "20260927_03"
down_revision = "20260915_02"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "event_outbox",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("event_id", sa.String(36), nullable=False, unique=True),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("published_at", sa.DateTime()),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_table(
        "incidents",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("correlation_key", sa.String(512), nullable=False),
        sa.Column("symptom", sa.String(128), nullable=False),
        sa.Column("service", sa.String(128), nullable=False),
        sa.Column("resource_id", sa.String(255), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="open"),
        sa.Column("first_seen_at", sa.DateTime(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(), nullable=False),
    )
    op.create_index(
        "uq_open_incident_key",
        "incidents",
        ["correlation_key"],
        unique=True,
        postgresql_where=sa.text("status = 'open'"),
        sqlite_where=sa.text("status = 'open'"),
    )
    op.create_table(
        "infra_events",
        sa.Column("event_id", sa.String(36), primary_key=True),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("event_type", sa.String(128), nullable=False),
        sa.Column("source", sa.String(128), nullable=False),
        sa.Column("service", sa.String(128), nullable=False),
        sa.Column("resource_id", sa.String(255), nullable=False),
        sa.Column("symptom", sa.String(128), nullable=False),
        sa.Column("severity", sa.String(16), nullable=False),
        sa.Column("occurred_at", sa.DateTime(), nullable=False),
        sa.Column("received_at", sa.DateTime(), nullable=False),
        sa.Column("correlation_key", sa.String(512), nullable=False),
        sa.Column("payload", sa.Text(), nullable=False),
        sa.Column("incident_id", sa.String(36), sa.ForeignKey("incidents.id")),
    )
    op.create_table(
        "diagnosis_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("incident_id", sa.String(36), sa.ForeignKey("incidents.id"), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("started_at", sa.DateTime()),
        sa.Column("finished_at", sa.DateTime()),
        sa.Column("error", sa.Text()),
        sa.Column("model_id", sa.String(128), nullable=False, server_default="fake-v1"),
        sa.Column("prompt_version", sa.String(32), nullable=False, server_default="diagnosis-v1"),
    )
    op.create_index("ix_diagnosis_runs_incident_id", "diagnosis_runs", ["incident_id"])
    op.create_index(
        "uq_active_diagnosis_per_incident",
        "diagnosis_runs",
        ["incident_id"],
        unique=True,
        postgresql_where=sa.text("status in ('pending', 'running')"),
        sqlite_where=sa.text("status in ('pending', 'running')"),
    )
    op.create_table(
        "diagnosis_reports",
        sa.Column("run_id", sa.String(36), sa.ForeignKey("diagnosis_runs.id"), primary_key=True),
        sa.Column("classification", sa.String(64), nullable=False),
        sa.Column("probable_cause", sa.Text(), nullable=False),
        sa.Column("evidence_json", sa.Text(), nullable=False),
        sa.Column("gaps_json", sa.Text(), nullable=False),
        sa.Column("recommendations_json", sa.Text(), nullable=False),
        sa.Column("references_json", sa.Text(), nullable=False),
        sa.Column("conclusion_status", sa.String(32), nullable=False),
        sa.Column("graph_nodes_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    op.create_table(
        "kb_documents",
        sa.Column("id", sa.String(128), primary_key=True),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("service", sa.String(128), nullable=False),
        sa.Column("symptom", sa.String(128), nullable=False),
        sa.Column("updated_at", sa.String(32), nullable=False),
        sa.Column("source_path", sa.String(512), nullable=False, unique=True),
        sa.Column("content_hash", sa.String(64), nullable=False),
    )
    op.create_table(
        "kb_chunks",
        sa.Column("id", sa.String(255), primary_key=True),
        sa.Column(
            "document_id",
            sa.String(128),
            sa.ForeignKey("kb_documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("heading", sa.String(255), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
    )
    op.create_index("ix_kb_chunks_document_id", "kb_chunks", ["document_id"])


def downgrade() -> None:
    op.drop_index("ix_kb_chunks_document_id", table_name="kb_chunks")
    op.drop_table("kb_chunks")
    op.drop_table("kb_documents")
    op.drop_table("diagnosis_reports")
    op.drop_index("uq_active_diagnosis_per_incident", table_name="diagnosis_runs")
    op.drop_index("ix_diagnosis_runs_incident_id", table_name="diagnosis_runs")
    op.drop_table("diagnosis_runs")
    op.drop_table("infra_events")
    op.drop_index("uq_open_incident_key", table_name="incidents")
    op.drop_table("incidents")
    op.drop_table("event_outbox")
