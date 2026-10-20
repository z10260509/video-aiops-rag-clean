"""Transactional Alertmanager episodes; no model or tool call in the receiver."""

import hashlib
import json
import re
from datetime import datetime, timezone

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.models.aiops import Alert, Timeline, DispatchRequest
from app.models.diagnostics import DiagnosisRun, Incident
from app.services.logging import redact

STATES = {
    "DETECTED": {"INVESTIGATING", "RESOLVED", "ESCALATED", "CANCELLED", "FAILED"},
    "INVESTIGATING": {"DIAGNOSED", "RESOLVED", "ESCALATED", "CANCELLED", "FAILED"},
    "DIAGNOSED": {"WAITING_APPROVAL", "RESOLVED_NO_ACTION", "RESOLVED", "ESCALATED", "CANCELLED"},
    "WAITING_APPROVAL": {"REMEDIATING", "REJECTED", "CANCELLED", "ESCALATED", "RESOLVED"},
    "REMEDIATING": {"VERIFYING", "FAILED", "ESCALATED"},
    "VERIFYING": {"RESOLVED", "ESCALATED", "FAILED"},
    "ESCALATED": {"RESOLVED"},
    "RESOLVED": set(),
    "RESOLVED_NO_ACTION": set(),
    "REJECTED": set(),
    "CANCELLED": set(),
    "FAILED": set(),
}


def now():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def safe_map(values):
    return {
        k: "[REDACTED]"
        if re.search(r"(?i)password|passwd|token|secret|authorization|api.?key", k)
        else redact(v, 2048)
        for k, v in values.items()
    }


def timeline(db, incident, event, actor, key, payload):
    if db.scalar(select(Timeline.id).where(Timeline.event_key == key)):
        return
    db.add(
        Timeline(
            incident_id=incident.id,
            event_key=key,
            actor=actor,
            event_type=event,
            payload_json=json.dumps(payload),
            created_at=now(),
        )
    )
    db.flush()


def lock_key(db: Session, key: str):
    if db.get_bind().dialect.name == "postgresql":
        number = int.from_bytes(hashlib.sha256(key.encode()).digest()[:8], "big", signed=True)
        db.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": number})


def transition(db, incident, state, actor, reason, expected_version=None):
    if expected_version is not None and expected_version != incident.version:
        raise ValueError("Incident version conflict")
    if state == incident.lifecycle:
        return
    if state not in STATES.get(incident.lifecycle, set()):
        raise ValueError("Invalid lifecycle transition")
    before = incident.lifecycle
    incident.lifecycle = state
    incident.version += 1
    if state in {"RESOLVED", "RESOLVED_NO_ACTION", "REJECTED", "CANCELLED", "FAILED", "ESCALATED"}:
        incident.status = "closed"
        if state.startswith("RESOLVED"):
            incident.resolved_at = now()
        for run in db.scalars(
            select(DiagnosisRun).where(
                DiagnosisRun.incident_id == incident.id,
                DiagnosisRun.workflow_kind == "aiops-hypothesis-v2",
                DiagnosisRun.status == "pending",
            )
        ):
            run.status = "cancelled"
            run.finished_at = now()
    timeline(
        db,
        incident,
        "lifecycle_changed",
        actor,
        f"{incident.id}:{incident.version}:{state}",
        {"from": before, "to": state, "reason": redact(reason, 1000)},
    )


def reopen_video_for_analysis(db, incident, actor, settings):
    # This is intentionally NOT a generic state transition or webhook path.
    from app.models.aiops import Action

    if (
        incident.lifecycle != "ESCALATED"
        or incident.status != "closed"
        or not settings.aiops_graph_enabled
    ):
        raise ValueError("只有轉交人工且尚未恢復的影片事件可重新開啟。")
    if db.scalar(
        select(Timeline.id)
        .where(Timeline.incident_id == incident.id, Timeline.event_type == "incident_deleted")
        .limit(1)
    ):
        raise ValueError("已刪除的事件不能重新開啟。")
    actions = list(
        db.scalars(select(Action).where(Action.incident_id == incident.id).with_for_update())
    )
    if any(
        a.execution_status in {"queued", "running", "uncertain"}
        or (a.approval_status == "approved" and a.execution_status == "pending")
        for a in actions
    ):
        raise ValueError("已有待執行、執行中或結果不確定的修復；請先查核原修復結果。")
    from app.aiops.video_bridge import verify_reopen

    link = verify_reopen(incident, settings)
    superseded = []
    for action in actions:
        if action.execution_status == "pending" and action.approval_status in {"draft", "pending"}:
            action.approval_status = "superseded"
            superseded.append(action.id)
    incident.lifecycle = "INVESTIGATING"
    incident.status = "open"
    incident.resolved_at = None
    incident.version += 1
    timeline(
        db,
        incident,
        "incident_reopened",
        actor,
        f"{incident.id}:{incident.version}:reopened",
        {
            "from": "ESCALATED",
            "to": "INVESTIGATING",
            "reason": "Operator requested new diagnosis; original failed attempt rechecked",
            **link,
            "superseded_action_ids": superseded,
            "approval_reused": False,
        },
    )


def schedule(db, incident, actor, request_key=None, *, reopen_video=False, settings=None):
    if request_key:
        receipt = db.get(DispatchRequest, request_key)
        if receipt:
            return db.get(DiagnosisRun, receipt.run_id)
    from app.core.config import get_settings

    settings = settings or get_settings()
    if incident.status != "open" and not (
        incident.lifecycle in {"RESOLVED", "RESOLVED_NO_ACTION", "ESCALATED"}
    ):
        raise ValueError("Incident is no longer active")
    active = db.scalar(
        select(DiagnosisRun).where(
            DiagnosisRun.incident_id == incident.id,
            DiagnosisRun.status.in_(["pending", "running", "waiting"]),
        )
    )
    if active:
        if active.workflow_kind != "aiops-hypothesis-v2":
            raise ValueError(
                "An historical workflow is still active; finish or cancel it before a new diagnosis"
            )
        if request_key:
            db.add(DispatchRequest(key=request_key, incident_id=incident.id, run_id=active.id))
            db.flush()
        return active
    if (
        reopen_video
        and incident.lifecycle == "ESCALATED"
        and incident.status == "closed"
        and incident.namespace == "video-platform"
    ):
        reopen_video_for_analysis(db, incident, actor, settings)
    run = DiagnosisRun(
        incident_id=incident.id,
        workflow_kind="aiops-hypothesis-v2",
        model_id="not-invoked",
        prompt_version="evidence-v1",
        request_key=request_key,
    )
    db.add(run)
    db.flush()
    if request_key:
        db.add(DispatchRequest(key=request_key, incident_id=incident.id, run_id=run.id))
    timeline(db, incident, "analysis_queued", actor, f"run:{run.id}", {"run_id": run.id})
    return run


def correlation(alert):
    return "am:" + ":".join(alert.labels[k] for k in ["namespace", "alertname", "pod"])


def receive(db, payload, allowed_namespaces):
    allowed = {x.strip() for x in allowed_namespaces.split(",") if x.strip()}
    if any(a.labels["namespace"] not in allowed for a in payload.alerts):
        raise PermissionError("Namespace outside allowlist")
    results = []
    for item in sorted(payload.alerts, key=lambda a: (correlation(a), a.startsAt, a.fingerprint)):
        key = correlation(item)
        lock_key(db, key)
        start = item.startsAt.astimezone(timezone.utc).replace(tzinfo=None)
        end = item.endsAt.astimezone(timezone.utc).replace(tzinfo=None) if item.endsAt else None
        alert = db.scalar(
            select(Alert)
            .where(Alert.fingerprint == item.fingerprint, Alert.starts_at == start)
            .with_for_update()
        )
        duplicate = False
        if alert:
            incident = db.get(Incident, alert.incident_id, with_for_update=True)
            if incident.correlation_key != key:
                raise ValueError("Alert episode target mismatch")
            # A resolved episode is a tombstone: late firing cannot resurrect it.
            duplicate = alert.status == item.status or alert.status == "resolved"
            if not duplicate:
                alert.status = "resolved"
                alert.ends_at = end
                alert.received_at = now()
        else:
            prior_end = db.scalar(
                select(func.max(Alert.ends_at)).where(
                    Alert.fingerprint == item.fingerprint, Alert.status == "resolved"
                )
            )
            stale = bool(prior_end and start <= prior_end)
            effective = "resolved" if stale else item.status
            incident = (
                db.scalar(
                    select(Incident)
                    .where(Incident.correlation_key == key, Incident.status == "open")
                    .with_for_update()
                )
                if effective == "firing"
                else None
            )
            created = incident is None
            if created:
                incident = Incident(
                    correlation_key=key,
                    symptom=item.labels["alertname"],
                    service="transcoder",
                    resource_id=item.labels["pod"],
                    namespace=item.labels["namespace"],
                    resource_kind="Pod",
                    alert_fingerprint=item.fingerprint,
                    severity=item.labels.get("severity", "warning")[:16],
                    first_seen_at=start,
                    last_seen_at=start,
                )
                db.add(incident)
                db.flush()
                timeline(
                    db,
                    incident,
                    "incident_detected",
                    "alertmanager",
                    f"incident:{incident.id}",
                    {"alertname": item.labels["alertname"], "namespace": incident.namespace},
                )
            alert = Alert(
                incident_id=incident.id,
                fingerprint=item.fingerprint,
                name=item.labels["alertname"],
                status=effective,
                starts_at=start,
                ends_at=prior_end if stale else end,
                labels_json=json.dumps(safe_map(item.labels)),
                annotations_json=json.dumps(safe_map(item.annotations)),
                received_at=now(),
            )
            db.add(alert)
            db.flush()
            if created and effective == "firing":
                schedule(db, incident, "alertmanager")
        if not duplicate:
            incident.last_seen_at = max(incident.last_seen_at, end or start)
            timeline(
                db,
                incident,
                "alert_" + alert.status,
                "alertmanager",
                f"alert:{alert.id}:{alert.status}",
                {"alert_id": alert.id, "fingerprint": alert.fingerprint},
            )
            if alert.status == "resolved" and not db.scalar(
                select(Alert.id)
                .where(Alert.incident_id == incident.id, Alert.status == "firing")
                .limit(1)
            ):
                active_graph = db.scalar(
                    select(DiagnosisRun.id)
                    .where(
                        DiagnosisRun.incident_id == incident.id,
                        DiagnosisRun.workflow_kind == "aiops-hypothesis-v2",
                        DiagnosisRun.status.in_(["pending", "running", "waiting"]),
                    )
                    .limit(1)
                )
                if active_graph:
                    timeline(
                        db,
                        incident,
                        "alert_cleared_workflow_active",
                        "alertmanager",
                        "alert-clear-active:" + alert.id,
                        {
                            "run_id": active_graph,
                            "reason": "Alert cleared; diagnosis, approval or recovery verification is still pending",
                        },
                    )
                elif "RESOLVED" in STATES.get(incident.lifecycle, set()):
                    transition(
                        db, incident, "RESOLVED", "alertmanager", "All alert episodes resolved"
                    )
        results.append({"incident_id": incident.id, "alert_id": alert.id, "duplicate": duplicate})
    db.commit()
    return results
