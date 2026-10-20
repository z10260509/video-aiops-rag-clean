"""Leased human approval, execution and recovery lifecycle for hypothesis v2."""

import json
from typing import TypedDict
from app.aiops.gateway import gateway
from app.aiops.incidents import now, timeline, transition
from app.aiops.knowledge import search
from app.database.session import SessionLocal
from app.models.diagnostics import DiagnosisRun, Incident


class State(TypedDict, total=False):
    incident_id: str
    run_id: str
    namespace: str
    resource_name: str
    evidence_ids: list[str]
    runbook_chunk_ids: list[str]
    source_gaps: list[str]
    validation_status: str
    gaps: list[str]
    proposed_action_type: str
    proposed_action_id: str
    policy_status: str
    policy_reason: str
    approval_status: str
    execution_status: str
    recovery_verified: bool


def thread_id(incident_id, run_id):
    return "incident:" + incident_id + ":run:" + run_id


class RepairLifecycle:
    def __init__(
        self,
        claimed,
        factory=SessionLocal,
        adapter=None,
        llm=None,
        retriever=None,
        action_client=None,
        settings=None,
    ):
        self.claimed = claimed
        self.factory = factory
        self.adapter = adapter or gateway()
        self.llm = llm
        self.retriever = retriever or search
        from app.core.config import get_settings

        self.settings = settings or get_settings()
        self.action_client = action_client

    def heartbeat(self):
        with self.factory() as db:
            row = db.get(DiagnosisRun, self.claimed[0], with_for_update=True)
            if row.status != "running" or row.started_at != self.claimed[2]:
                raise ValueError("Graph lease lost")
            row.started_at = now()
            db.commit()
            self.claimed = (*self.claimed[:2], row.started_at, *self.claimed[3:])

    def plan(self, state):
        self.heartbeat()
        if state.get("proposed_action_type", "none") == "none":
            return {"policy_status": "no_action"}
        from app.aiops.actions import propose
        from app.aiops.executor import KubernetesActionClient

        try:
            with self.factory() as db:
                row = propose(
                    db,
                    db.get(Incident, state["incident_id"]),
                    db.get(DiagnosisRun, state["run_id"]),
                    self.action_client or KubernetesActionClient(),
                    self.settings.aiops_approval_ttl_seconds,
                )
                db.commit()
                return {"proposed_action_id": row.id, "policy_status": "pending"}
        except PermissionError as exc:
            return {"policy_status": "denied", "policy_reason": str(exc)}
        except Exception:
            return {"policy_status": "denied", "policy_reason": "Bound repair target unavailable"}

    def policy_check(self, state):
        if state.get("policy_status") != "pending":
            return {}
        from app.aiops.actions import policy
        from app.aiops.executor import KubernetesActionClient
        from app.models.aiops import Action

        with self.factory() as db:
            action = db.get(Action, state["proposed_action_id"])
            try:
                policy(db, action, self.action_client or KubernetesActionClient(), self.settings)
                incident = db.get(Incident, state["incident_id"])
                if incident.lifecycle == "INVESTIGATING":
                    transition(
                        db,
                        incident,
                        "DIAGNOSED",
                        "policy",
                        "Validated diagnosis with bounded action",
                    )
                transition(
                    db,
                    incident,
                    "WAITING_APPROVAL",
                    "policy",
                    "Authenticated human approval required",
                )
                db.commit()
                return {"policy_status": "allowed"}
            except Exception as exc:
                action.approval_status = "draft"
                db.commit()
                return {
                    "policy_status": "denied",
                    "policy_reason": str(exc)
                    if isinstance(exc, PermissionError)
                    else "Policy source unavailable",
                }

    def approval(self, state):
        from langgraph.types import interrupt
        from app.models.aiops import Action, Approval

        with self.factory() as db:
            action = db.get(Action, state["proposed_action_id"])
            status = action.approval_status
            if action.expires_at <= now() and status == "pending":
                action.approval_status = "expired"
                db.commit()
                status = "expired"
        if status == "pending":
            interrupt({"kind": "human_approval", "action_id": state["proposed_action_id"]})
        # Resume values are never authority; the authenticated API's SQL record is authoritative.
        with self.factory() as db:
            action = db.get(Action, state["proposed_action_id"])
            approval = db.get(Approval, action.id)
            valid = bool(
                approval and approval.binding_hash == action.binding_hash and approval.principal
            )
            return {"approval_status": approval.decision if valid else "expired"}

    def execution(self, state):
        from langgraph.types import interrupt
        from app.aiops.actions import policy
        from app.aiops.executor import KubernetesActionClient
        from app.models.aiops import Action

        with self.factory() as db:
            action = db.get(Action, state["proposed_action_id"])
            if action.execution_status == "pending":
                try:
                    policy(
                        db,
                        action,
                        self.action_client or KubernetesActionClient(),
                        self.settings,
                        require_approval=True,
                    )
                except Exception:
                    action.execution_status = "denied"
                    db.commit()
                    return {"execution_status": "denied"}
                incident = db.get(Incident, state["incident_id"])
                transition(
                    db,
                    incident,
                    "REMEDIATING",
                    "policy",
                    "Second policy check passed; dedicated executor queue",
                )
                action.execution_status = "queued"
                db.commit()
            status = action.execution_status
        if status in {"queued", "running"}:
            interrupt({"kind": "executor_result", "action_id": state["proposed_action_id"]})
        with self.factory() as db:
            return {
                "execution_status": db.get(Action, state["proposed_action_id"]).execution_status
            }

    def verify(self, state):
        from app.models.aiops import Action

        with self.factory() as db:
            action = db.get(Action, state["proposed_action_id"])
            result = json.loads(action.result_json or "{}")
            verification = result.get("verification", {})
            from app.aiops.executor import recovery_valid

            valid = action.execution_status == "succeeded" and recovery_valid(action, verification)
            incident = db.get(Incident, state["incident_id"])
            if valid and incident.lifecycle == "REMEDIATING":
                transition(
                    db,
                    incident,
                    "VERIFYING",
                    "graph-worker",
                    "Original video retry output and deployment recovery verified",
                )
            db.commit()
            return {"recovery_verified": valid}
