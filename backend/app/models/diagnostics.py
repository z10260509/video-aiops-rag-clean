import uuid


from datetime import datetime


from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, UniqueConstraint


from sqlalchemy.orm import Mapped, mapped_column


from app.database.session import Base


def _uuid() -> str:
    return str(uuid.uuid4())


class Incident(Base):
    __tablename__ = "incidents"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    correlation_key: Mapped[str] = mapped_column(String(512), nullable=False)
    symptom: Mapped[str] = mapped_column(String(128), nullable=False)
    service: Mapped[str] = mapped_column(String(128), nullable=False)
    resource_id: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(16), default="open", nullable=False)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    namespace: Mapped[str | None] = mapped_column(String(63))
    resource_kind: Mapped[str | None] = mapped_column(String(32))
    alert_fingerprint: Mapped[str | None] = mapped_column(String(64))
    severity: Mapped[str] = mapped_column(
        String(16), default="warning", server_default="warning", nullable=False
    )
    lifecycle: Mapped[str] = mapped_column(
        String(32), default="DETECTED", server_default="DETECTED", nullable=False
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime)
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1", nullable=False)


Index(
    "uq_open_incident_key",
    Incident.correlation_key,
    unique=True,
    postgresql_where=Incident.status == "open",
    sqlite_where=Incident.status == "open",
)


class DiagnosisRun(Base):
    __tablename__ = "diagnosis_runs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    incident_id: Mapped[str] = mapped_column(ForeignKey("incidents.id"), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(20), default="pending", nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    error: Mapped[str | None] = mapped_column(Text)
    model_id: Mapped[str] = mapped_column(String(128), default="not-invoked", nullable=False)
    prompt_version: Mapped[str] = mapped_column(
        String(32), default="hypothesis-rag-v2", nullable=False
    )
    workflow_kind: Mapped[str] = mapped_column(
        String(32), default="aiops-hypothesis-v2", nullable=False
    )
    request_key: Mapped[str | None] = mapped_column(String(128), unique=True)


class KBDocument(Base):
    __tablename__ = "kb_documents"
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    service: Mapped[str] = mapped_column(String(128), nullable=False)
    symptom: Mapped[str] = mapped_column(String(128), nullable=False)
    updated_at: Mapped[str] = mapped_column(String(32), nullable=False)
    source_path: Mapped[str] = mapped_column(String(512), nullable=False, unique=True)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    version: Mapped[str] = mapped_column(
        String(64), default="legacy", server_default="legacy", nullable=False
    )
    kind: Mapped[str] = mapped_column(
        String(32), default="runbook", server_default="runbook", nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(16), default="active", server_default="active", nullable=False
    )
    metadata_json: Mapped[str] = mapped_column(
        Text, default="{}", server_default="{}", nullable=False
    )


class KBChunk(Base):
    __tablename__ = "kb_chunks"
    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    document_id: Mapped[str] = mapped_column(
        ForeignKey("kb_documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    heading: Mapped[str] = mapped_column(String(255), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)

    ordinal: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    content_hash: Mapped[str] = mapped_column(
        String(64), default="", server_default="", nullable=False
    )
    document_version: Mapped[str] = mapped_column(
        String(64), default="legacy", server_default="legacy", nullable=False
    )
    active: Mapped[bool] = mapped_column(
        Boolean, default=True, server_default="true", nullable=False
    )
    metadata_json: Mapped[str] = mapped_column(
        Text, default="{}", server_default="{}", nullable=False
    )


Index(
    "uq_active_diagnosis_per_incident",
    DiagnosisRun.incident_id,
    unique=True,
    postgresql_where=DiagnosisRun.status.in_(["pending", "running"]),
    sqlite_where=DiagnosisRun.status.in_(["pending", "running"]),
)
