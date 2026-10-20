"""Typed hypotheses and factual checks. A matched assertion is not repair authorization."""

import hashlib
import json
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, model_validator

WORKFLOW = "aiops-hypothesis-v2"
PROMPT_VERSION = "hypothesis-rag-v2"
READ_ONLY_TOOLS = {
    "get_pod_status",
    "get_pod_events",
    "get_pod_logs",
    "query_metric",
    "query_logs",
    "get_task_status",
    "get_queue_depth",
    "probe_video_input",
    "get_encoder_inventory",
}


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Assertion(Strict):
    evidence_id: str = Field(min_length=1, max_length=64)
    path: str = Field(pattern=r"^/[a-zA-Z0-9_/-]{1,180}$")
    operator: Literal["equals", "contains", "greater_than"]
    value_type: Literal["native", "string", "boolean", "number"] = "native"
    expected: str | bool | int | float

    @model_validator(mode="after")
    def scalar_type(self):
        import math

        if self.value_type == "boolean":
            if isinstance(self.expected, str) and self.expected in {"true", "false"}:
                self.expected = self.expected == "true"
            if type(self.expected) is not bool:
                raise ValueError("Boolean assertion requires true/false")
        elif self.value_type == "number":
            if isinstance(self.expected, str):
                try:
                    self.expected = float(self.expected)
                except ValueError:
                    raise ValueError("Finite number required") from None
            if type(self.expected) not in (int, float) or not math.isfinite(self.expected):
                raise ValueError("Finite number required")
        elif self.value_type == "string" and not isinstance(self.expected, str):
            raise ValueError("String assertion required")
        if isinstance(self.expected, str) and len(self.expected) > 1200:
            raise ValueError("Assertion value too long")
        return self


class Hypothesis(Strict):
    id: str = Field(pattern=r"^H[1-3]$")
    cause: str = Field(pattern=r"^[A-Z][A-Z0-9_]{1,63}$")
    explanation: str = Field(min_length=1, max_length=700)
    assertions: list[Assertion] = Field(default_factory=list, max_length=6)
    missing_information: list[str] = Field(default_factory=list, max_length=5)
    required_tools: list[
        Literal[
            "get_pod_status",
            "get_pod_events",
            "get_pod_logs",
            "query_metric",
            "query_logs",
            "get_task_status",
            "get_queue_depth",
            "probe_video_input",
            "get_encoder_inventory",
        ]
    ] = Field(default_factory=list, max_length=5)
    follow_up_checks: list[str] = Field(default_factory=list, max_length=5)
    search_query: str = Field(min_length=1, max_length=400)


class InvestigationRequest(Strict):
    hypothesis_id: str = Field(pattern=r"^H[1-3]$")
    tool_name: Literal[
        "get_pod_status",
        "get_pod_events",
        "get_pod_logs",
        "query_metric",
        "query_logs",
        "get_task_status",
        "get_queue_depth",
        "probe_video_input",
        "get_encoder_inventory",
    ]
    purpose: str = Field(min_length=1, max_length=300)
    metric_key: Literal[
        "failure_total", "queue_depth", "memory", "worker_available", "oom_kills"
    ] = "failure_total"
    query_key: Literal["worker_errors", "worker_tasks"] = "worker_errors"
    lookback_seconds: int = Field(default=300, ge=1, le=900)


class RepairOption(Strict):
    id: str = Field(pattern=r"^P[1-3]$")
    hypothesis_id: str = Field(pattern=r"^H[1-3]$")
    action_type: str = Field(pattern=r"^[a-z][a-z0-9_]{1,79}$")
    rationale: str = Field(min_length=1, max_length=500)
    risk: str = Field(min_length=1, max_length=400)
    prerequisites: list[str] = Field(default_factory=list, max_length=5)
    verification: str = Field(min_length=1, max_length=400)
    runbook_chunk_ids: list[str] = Field(default_factory=list, max_length=6)

    @model_validator(mode="after")
    def repair_is_not_query(self):
        if self.action_type in READ_ONLY_TOOLS:
            raise ValueError("Read-only queries belong in requests, not repair_options")
        return self


class RoundOutput(Strict):
    _validation_issues: list[dict] = PrivateAttr(default_factory=list)
    hypotheses: list[Hypothesis] = Field(min_length=1, max_length=3)
    requests: list[InvestigationRequest] = Field(default_factory=list, max_length=2)
    selected_hypothesis_id: str | None = Field(default=None, pattern=r"^H[1-3]$")
    repair_options: list[RepairOption] = Field(default_factory=list, max_length=3)
    selected_option_id: str | None = Field(default=None, pattern=r"^P[1-3]$")
    summary: str = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def references(self):
        ids = [h.id for h in self.hypotheses]
        plans = [p.id for p in self.repair_options]
        if len(set(ids)) != len(ids) or len(set(plans)) != len(plans):
            raise ValueError("Duplicate hypothesis or plan")
        if self.selected_hypothesis_id and self.selected_hypothesis_id not in ids:
            raise ValueError("Unknown selected hypothesis")
        if any(r.hypothesis_id not in ids for r in self.requests + self.repair_options):
            raise ValueError("Unknown hypothesis reference")
        if self.selected_option_id and self.selected_option_id not in plans:
            raise ValueError("Unknown selected plan")
        return self


def evaluate(hypothesis, records):
    """Evaluate only bounded JSON fields from immutable, server-filtered observations."""
    rows = {r["evidence_id"]: r for r in records}
    checks = []
    for claim in hypothesis.assertions:
        row = rows.get(claim.evidence_id)
        value = None
        exists = False
        if row and not row["errors"] and not row.get("truncated", False):
            value = row.get("data")
            exists = True
            for key in claim.path[1:].split("/"):
                if isinstance(value, dict) and key in value:
                    value = value[key]
                elif isinstance(value, list) and key.isdigit() and int(key) < len(value):
                    value = value[int(key)]
                else:
                    exists = False
                    break
        status = "unknown"
        detail = {}
        if exists:
            observed = value
            if claim.operator == "equals":
                exists = (
                    type(value) is type(claim.expected) and type(value) in (str, bool, int, float)
                ) or (type(value) in (int, float) and type(claim.expected) in (int, float))
                matched = exists and value == claim.expected
            elif claim.operator == "contains":
                if (
                    not isinstance(value, str)
                    or not isinstance(claim.expected, str)
                    or not claim.expected
                ):
                    exists = False
                    matched = False
                else:
                    matched = claim.expected in value
            else:
                if type(value) not in (int, float) or type(claim.expected) not in (int, float):
                    exists = False
                    matched = False
                else:
                    matched = value > claim.expected
            if exists:
                status = "supported" if matched else "contradicted"
            else:
                name = lambda v: (
                    "boolean"
                    if type(v) is bool
                    else "number"
                    if type(v) in (int, float)
                    else "string"
                    if type(v) is str
                    else "object_or_array"
                )
                detail = {
                    "reason": "type_mismatch",
                    "actual_type": name(observed),
                    "expected_type": name(claim.expected),
                    "instruction": "依原觀測型別修正 value_type 與 expected；不得把數字 0 當成布林 false。",
                }
                if type(observed) in (bool, int, float):
                    detail["actual_value"] = observed
        elif row and (row.get("errors") or row.get("truncated")):
            detail = {"reason": "source_unavailable"}
        else:
            detail = {"reason": "missing_evidence_or_field"}
        checks.append({**claim.model_dump(), "status": status, **detail})
    proven_tools = {
        rows[c["evidence_id"]].get("kind")
        for c in checks
        if c["status"] == "supported" and c["evidence_id"] in rows
    }
    missing_tools = sorted(set(hypothesis.required_tools) - proven_tools)
    unbound_gap = bool(hypothesis.missing_information and not hypothesis.required_tools)
    status = "unresolved"
    if any(c["status"] == "contradicted" for c in checks):
        status = "contradicted"
    elif (
        checks
        and all(c["status"] == "supported" for c in checks)
        and not missing_tools
        and not unbound_gap
    ):
        status = "supported"
    return {
        "hypothesis_id": hypothesis.id,
        "status": status,
        "checks": checks,
        "missing_required_tools": missing_tools,
        "reported_questions": hypothesis.missing_information,
        "missing_information": (
            ["Information gap lacks a declared verification tool"]
            if unbound_gap
            else ["Missing supported assertion from " + tool for tool in missing_tools]
        ),
    }


def trusted_records(db, incident, run_id, evidence_ids):
    """IDs, target, time, task and checksum checks precede any model-visible observation."""
    from datetime import timedelta
    from app.models.aiops import Evidence, Timeline
    from sqlalchemy import select, func
    from app.models.diagnostics import DiagnosisRun
    from app.aiops.incidents import now
    from app.aiops.video_bridge import linked_job

    link = linked_job(incident)
    records = []
    run = db.get(DiagnosisRun, run_id)
    started = db.scalar(
        select(func.min(Timeline.created_at)).where(
            Timeline.incident_id == incident.id,
            Timeline.event_key.startswith("hypothesis-tool:" + run_id + ":"),
        )
    ) or (run.started_at if run and run.started_at else now())
    for eid in evidence_ids:
        row = db.get(Evidence, eid)
        if not row or row.incident_id != incident.id or row.run_id != run_id:
            continue
        if hashlib.sha256(row.payload_json.encode()).hexdigest() != row.checksum:
            continue
        if (
            not started - timedelta(seconds=5)
            <= row.requested_at
            <= row.observed_at
            <= min(started + timedelta(minutes=15), now() + timedelta(seconds=5))
        ):
            continue
        target = json.loads(row.query_json)
        data = json.loads(row.payload_json)
        if (
            target.get("namespace") != incident.namespace
            or target.get("pod") != incident.resource_id
        ):
            continue
        if link and row.kind in {"get_task_status", "probe_video_input", "get_encoder_inventory"}:
            if target.get("job_id") != link["job_id"] or target.get("task_id") != link["task_id"]:
                continue
            if not json.loads(row.errors_json) and (
                not isinstance(data, dict)
                or data.get("job_id") != link["job_id"]
                or data.get("task_id") != link["task_id"]
            ):
                continue
        if link and row.kind == "query_logs":
            # Shared workers may log unrelated tasks. Keep only exact linked task lines.
            data = dict(data or {})
            lines = []
            for item in data.get("lines", []):
                try:
                    obj = json.loads(item["line"])
                    if (
                        obj.get("job_id") == link["job_id"]
                        and obj.get("task_id") == link["task_id"]
                    ):
                        lines.append(item)
                except (ValueError, KeyError, TypeError):
                    continue
            data["lines"] = lines
        if row.kind == "query_logs":
            from app.aiops.observation_fields import normalize_logs

            data = normalize_logs(data, incident.namespace, incident.resource_id, link)
        if link and row.kind == "get_pod_logs":
            # Unstructured shared-worker logs cannot prove which job failed.
            continue
        records.append(
            {
                "evidence_id": row.id,
                "kind": row.kind,
                "source": row.source,
                "observed_at": row.observed_at.isoformat() + "Z",
                "data": data,
                "errors": json.loads(row.errors_json),
                "truncated": row.truncated,
            }
        )
    return records


def repair_eligible(kind, hypothesis, records, link):
    """Action preconditions only: never select or generate a diagnosis category here."""
    from app.aiops.actions import SPECS
    from app.aiops.video_bridge import oom_termination_matches

    spec = SPECS.get(kind)
    if (
        not spec
        or hypothesis.cause != spec["root"]
        or spec.get("namespace") != "video-platform"
        or not link
    ):
        return False
    cited = {c.evidence_id for c in hypothesis.assertions}
    rows = [
        r
        for r in records
        if r["evidence_id"] in cited and not r["errors"] and not r.get("truncated")
    ]
    tasks = [
        r
        for r in rows
        if r["kind"] == "get_task_status"
        and r["source"] == "video_task_api"
        and (r["data"] or {}).get("job_present")
    ]
    for row in tasks:
        task = row["data"]
        error = task.get("error") or ""
        if task.get("job_id") != link["job_id"] or task.get("task_id") != link["task_id"]:
            continue
        if (
            spec["root"] == "FFMPEG_INVALID_CODEC"
            and task.get("execution_status") == "failed"
            and any(x in error for x in ["Unknown encoder", "Encoder not found"])
        ):
            return True
        if (
            spec["root"] == "REDIS_CONNECTIVITY"
            and task.get("execution_status") == "failed"
            and any(
                x in error
                for x in [
                    "Name or service not known",
                    "Connection refused",
                    "Error 111",
                    "Error -2",
                ]
            )
        ):
            return True
        if spec["root"] == "WORKER_OOM" and task.get("execution_status") == "running":
            for pod in rows:
                data = pod["data"] or {}
                if (
                    pod["kind"] == "get_pod_status"
                    and pod["source"] == "kubernetes"
                    and data.get("uid") == task.get("pod_uid")
                    and any(
                        c.get("name") == "worker"
                        and oom_termination_matches(
                            c.get("last_state", {}).get("terminated", {}),
                            task.get("started_at", ""),
                        )
                        for c in data.get("containers", [])
                    )
                ):
                    return True
    return False


def guard_action(db, action):
    """Recheck the persisted v2 proof at proposal, review and executor boundaries."""
    from app.models.aiops import Diagnosis
    from app.models.diagnostics import DiagnosisRun, Incident

    run = db.get(DiagnosisRun, action.run_id)
    if not run or run.workflow_kind != WORKFLOW:
        raise PermissionError(
            "Historical workflow is read-only; request a new hypothesis diagnosis"
        )
    report = db.get(Diagnosis, run.id)
    output = json.loads(report.payload_json)
    investigation = output.get("investigation", {})
    candidate = RoundOutput.model_validate(investigation.get("candidate", {}))
    h = next((h for h in candidate.hypotheses if h.id == candidate.selected_hypothesis_id), None)
    plan = next((p for p in candidate.repair_options if p.id == candidate.selected_option_id), None)
    from app.aiops.video_bridge import linked_job

    incident = db.get(Incident, run.incident_id)
    records = trusted_records(db, incident, run.id, investigation.get("evidence_ids", []))
    refs = investigation.get("hypothesis_references", {})
    references_ok = bool(
        h
        and plan
        and plan.runbook_chunk_ids
        and set(plan.runbook_chunk_ids) <= set(refs.get(h.id, []))
    )
    if (
        not references_ok
        or report.validation_status != "validated"
        or not h
        or not plan
        or plan.hypothesis_id != h.id
        or plan.action_type != action.action_type
        or output.get("root_cause_code") != h.cause
        or evaluate(h, records)["status"] != "supported"
        or not repair_eligible(action.action_type, h, records, linked_job(incident))
    ):
        raise PermissionError("Hypothesis evidence does not authorize this repair")
