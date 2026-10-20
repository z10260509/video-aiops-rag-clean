"""V2 video repair whitelist and persisted, bound human authorization."""

import hashlib
import json
import re
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field
from app.aiops.incidents import now, timeline
from app.core.config import get_settings
from app.models.aiops import Approval
from app.models.diagnostics import Incident, DiagnosisRun
from app.services.logging import redact

SPECS = {
    "retry_video_ffmpeg_h264": {
        "namespace": "video-platform",
        "deployment": "worker",
        "root": "FFMPEG_INVALID_CODEC",
        "policy": "video-codec-v1",
    },
    "restore_video_redis_and_retry": {
        "namespace": "video-platform",
        "deployment": "worker",
        "root": "REDIS_CONNECTIVITY",
        "policy": "video-redis-v1",
    },
    "increase_video_worker_memory_and_retry": {
        "namespace": "video-platform",
        "deployment": "worker",
        "root": "WORKER_OOM",
        "policy": "video-oom-v1",
    },
}


def spec_for(action_type):
    if action_type not in SPECS:
        raise PermissionError("Action outside policy")
    return SPECS[action_type]


def configure(client, action_type, action=None):
    fn = getattr(client, "configure", None)
    if fn:
        fn(action_type, action)


def pod_allowed(name, deployment="worker"):
    return (
        isinstance(name, str)
        and name.startswith(deployment + "-")
        and re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?", name) is not None
    )


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["approved", "rejected"]
    reason: str = Field(min_length=5, max_length=1000)
    binding_hash: str = Field(pattern=r"^[a-f0-9]{64}$")


def binding(action):
    raw = json.dumps(
        {
            "incident_id": action.incident_id,
            "run_id": action.run_id,
            "action_type": action.action_type,
            "target": json.loads(action.target_json),
            "params": json.loads(action.params_json),
            "policy_version": action.policy_version,
            "expires_at": action.expires_at.isoformat(),
        },
        sort_keys=True,
    )
    return hashlib.sha256(raw.encode()).hexdigest()


def policy(db, action, client, settings=None, require_approval=False):
    settings = settings or get_settings()
    if not settings.aiops_approval_enabled or not settings.remediation_enabled:
        raise PermissionError("Remediation disabled")
    if not settings.aiops_hypothesis_remediation_enabled:
        raise PermissionError("Hypothesis repairs disabled")
    from app.aiops.hypotheses import guard_action
    from app.aiops.video_actions import video_policy

    spec_for(action.action_type)
    guard_action(db, action)
    return video_policy(db, action, client, settings, require_approval)


def propose(db, incident, run, client, ttl=600):
    from types import SimpleNamespace
    from sqlalchemy import select
    from app.models.aiops import Action, Diagnosis
    from app.aiops.hypotheses import guard_action
    from app.aiops.video_actions import video_propose

    kind = json.loads(db.get(Diagnosis, run.id).payload_json).get("proposed_action_type")
    spec_for(kind)
    guard_action(db, SimpleNamespace(run_id=run.id, action_type=kind))
    old = db.scalar(select(Action).where(Action.run_id == run.id))
    return old if old else video_propose(db, incident, run, client, ttl)


def decide(db, action, request, principal, client, settings=None):
    old = db.get(Approval, action.id)
    if old:
        if (
            old.decision == request.decision
            and old.principal == principal
            and old.binding_hash == request.binding_hash
        ):
            return action
        raise ValueError("Decision already recorded")
    if request.binding_hash != action.binding_hash:
        raise ValueError("Approval binding mismatch")
    if action.approval_status != "pending":
        raise ValueError("Action not pending approval")
    if request.decision == "approved":
        policy(db, action, client, settings)
    elif action.expires_at <= now():
        raise PermissionError("Approval expired")
    db.add(
        Approval(
            action_id=action.id,
            principal=principal,
            decision=request.decision,
            reason=redact(request.reason, 1000),
            binding_hash=action.binding_hash,
            decided_at=now(),
        )
    )
    action.approval_status = request.decision
    incident = db.get(Incident, action.incident_id)
    timeline(
        db,
        incident,
        "action_" + request.decision,
        principal,
        "decision:" + action.id,
        {
            "action_id": action.id,
            "reason": redact(request.reason, 1000),
            "binding_hash": action.binding_hash,
        },
    )
    run = db.get(DiagnosisRun, action.run_id)
    if run.status == "waiting":
        run.status = "pending"
    db.flush()
    return action


def action_json(row):
    return {
        "id": row.id,
        "run_id": row.run_id,
        "action_type": row.action_type,
        "target": json.loads(row.target_json),
        "params": json.loads(row.params_json),
        "binding_hash": row.binding_hash,
        "policy_version": row.policy_version,
        "approval_status": row.approval_status,
        "execution_status": row.execution_status,
        "expires_at": row.expires_at,
        "result": json.loads(row.result_json) if row.result_json else None,
    }
