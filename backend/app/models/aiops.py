import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database.session import Base


def uid():
    return str(uuid.uuid4())


class Alert(Base):
    __tablename__ = "aiops_alerts"
    __table_args__ = (UniqueConstraint("fingerprint", "starts_at", name="uq_aiops_alert_episode"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    incident_id: Mapped[str] = mapped_column(ForeignKey("incidents.id"), nullable=False, index=True)
    fingerprint: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    labels_json: Mapped[str] = mapped_column(Text, nullable=False)
    annotations_json: Mapped[str] = mapped_column(Text, nullable=False)
    starts_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    ends_at: Mapped[datetime | None] = mapped_column(DateTime)
    received_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class Timeline(Base):
    __tablename__ = "aiops_timeline"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    incident_id: Mapped[str] = mapped_column(ForeignKey("incidents.id"), nullable=False, index=True)
    event_key: Mapped[str] = mapped_column(String(128), unique=True, nullable=False)
    actor: Mapped[str] = mapped_column(String(128), nullable=False)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class Evidence(Base):
    __tablename__ = "aiops_evidence"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    incident_id: Mapped[str] = mapped_column(ForeignKey("incidents.id"), nullable=False, index=True)
    run_id: Mapped[str | None] = mapped_column(ForeignKey("diagnosis_runs.id"), index=True)
    kind: Mapped[str] = mapped_column(String(64), nullable=False)
    source: Mapped[str] = mapped_column(String(128), nullable=False)
    requested_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    observed_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    query_json: Mapped[str] = mapped_column(Text, nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    truncated: Mapped[bool] = mapped_column(default=False, nullable=False)
    errors_json: Mapped[str] = mapped_column(Text, default="[]", nullable=False)


class DispatchRequest(Base):
    __tablename__ = "aiops_dispatch_requests"
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    incident_id: Mapped[str] = mapped_column(ForeignKey("incidents.id"), nullable=False)
    run_id: Mapped[str] = mapped_column(ForeignKey("diagnosis_runs.id"), nullable=False)


class Diagnosis(Base):
    __tablename__ = "aiops_diagnoses"
    run_id: Mapped[str] = mapped_column(ForeignKey("diagnosis_runs.id"), primary_key=True)
    incident_id: Mapped[str] = mapped_column(ForeignKey("incidents.id"), nullable=False, index=True)
    thread_id: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    model_id: Mapped[str] = mapped_column(String(128), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(32), nullable=False)
    payload_json: Mapped[str] = mapped_column(Text, nullable=False)
    validation_status: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class Action(Base):
    __tablename__ = "aiops_actions"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=uid)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("diagnosis_runs.id"), nullable=False, unique=True
    )
    incident_id: Mapped[str] = mapped_column(ForeignKey("incidents.id"), nullable=False, index=True)
    action_type: Mapped[str] = mapped_column(String(64), nullable=False)
    target_json: Mapped[str] = mapped_column(Text, nullable=False)
    params_json: Mapped[str] = mapped_column(Text, nullable=False)
    binding_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    policy_version: Mapped[str] = mapped_column(String(32), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    approval_status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft")
    execution_status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    result_json: Mapped[str | None] = mapped_column(Text)


class Approval(Base):
    __tablename__ = "aiops_approvals"
    action_id: Mapped[str] = mapped_column(ForeignKey("aiops_actions.id"), primary_key=True)
    principal: Mapped[str] = mapped_column(String(128), nullable=False)
    decision: Mapped[str] = mapped_column(String(16), nullable=False)
    reason: Mapped[str] = mapped_column(String(1000), nullable=False)
    binding_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    decided_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
