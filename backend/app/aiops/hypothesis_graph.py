"""Hypothesis-driven investigation, with persisted budgets and the existing approval tail."""

import hashlib
import json
from datetime import timezone
from typing import TypedDict
import uuid
from langgraph.graph import START, END, StateGraph
from sqlalchemy import select, func
from app.aiops.lifecycle import RepairLifecycle, State, thread_id
from app.aiops.gateway import ToolRequest, ToolResult, save, sanitize
from app.aiops.hypotheses import (
    WORKFLOW,
    PROMPT_VERSION,
    RoundOutput,
    evaluate,
    trusted_records,
    repair_eligible,
)
from app.aiops.hypothesis_model import hypothesis_model
from app.aiops.hypothesis_validation import parse_round, validation_details
from app.aiops.incidents import now, timeline, transition, STATES
from app.aiops.knowledge import SearchInput
from app.models.aiops import Diagnosis, Evidence, Timeline
from app.models.diagnostics import Incident, DiagnosisRun, KBChunk

MAX_ROUNDS = 3
MAX_TOOLS = 8
# These queries have no model-selected metric, log filter or time-window parameters.
# Their namespace, Pod and original task are bound by request(), never by the model.
FIXED_REQUIRED_TOOLS = frozenset(
    {"get_task_status", "get_pod_status", "get_encoder_inventory", "probe_video_input"}
)


class InvestigationState(State, total=False):
    round: int
    successful_rounds: int
    candidate: dict
    hypotheses: list[dict]
    assessments: list[dict]
    investigation_history: list[dict]
    hypothesis_references: dict
    requests: list[dict]
    tools_used: int
    investigation_error: str
    stop_reason: str
    task_link: dict
    validation_issues: list[dict]
    model_error_kind: str
    scheduled_requests: list[dict]


def observation_id(run_id, request):
    return str(
        uuid.uuid5(
            uuid.NAMESPACE_URL,
            run_id + json.dumps(request.model_dump(mode="json"), sort_keys=True),
        )
    )


def preview(value, depth=0):
    """Preserve original JSON paths and array indexes in the model's bounded view."""
    if depth > 9:
        return None
    if isinstance(value, str):
        return value[:1200]
    if isinstance(value, list):
        return [preview(x, depth + 1) for x in value[:6]]
    if isinstance(value, dict):
        return {k: preview(v, depth + 1) for k, v in list(value.items())[:30]}
    return value


class HypothesisRuntime(RepairLifecycle):
    workflow_kind = WORKFLOW

    def __init__(
        self,
        claimed,
        factory=None,
        adapter=None,
        llm=None,
        retriever=None,
        action_client=None,
        settings=None,
    ):
        from app.core.config import get_settings
        from app.database.session import SessionLocal

        settings = settings or get_settings()
        super().__init__(
            claimed,
            factory or SessionLocal,
            adapter,
            llm or hypothesis_model(settings),
            retriever,
            action_client,
            settings,
        )

    def observations(self, state):
        with self.factory() as db:
            return trusted_records(
                db,
                db.get(Incident, state["incident_id"]),
                state["run_id"],
                state.get("evidence_ids", []),
            )

    def linked_task(self, state):
        from app.aiops.video_bridge import linked_job

        with self.factory() as db:
            link = linked_job(db.get(Incident, state["incident_id"]))
        return link or state.get("task_link", {})

    def request(self, state, name, **options):
        link = self.linked_task(state)
        if (
            name in {"probe_video_input", "get_encoder_inventory"}
            and state["namespace"] != "video-platform"
        ):
            raise ValueError("Probe only supports original videos")
        if name == "get_pod_logs" and state["namespace"] == "video-platform":
            raise ValueError("Use task-bound query_logs for shared video workers")
        normalized = {}
        if name == "query_metric":
            normalized["metric_key"] = options.get("metric_key", "failure_total")
        if name == "query_logs":
            normalized["query_key"] = options.get("query_key", "worker_errors")
        if name in {
            "query_metric",
            "query_logs",
            "get_pod_events",
            "get_pod_logs",
            "get_queue_depth",
        }:
            normalized["lookback_seconds"] = options.get("lookback_seconds", 300)
        kwargs = {
            "tool_name": name,
            "namespace": state["namespace"],
            "pod": state["resource_name"],
            **normalized,
        }
        if link and name in {
            "get_task_status",
            "query_logs",
            "probe_video_input",
            "get_encoder_inventory",
        }:
            kwargs.update(job_id=link["job_id"], task_id=link["task_id"])
        if name in {"get_task_status", "probe_video_input", "get_encoder_inventory"} and not link:
            raise ValueError("Linked original task required")
        return ToolRequest(**kwargs)

    def probe(self, state, request):
        """Reserve an ordinal before querying; interrupted queries never silently exceed budget."""
        raw = request.model_dump(mode="json")
        eid = observation_id(state["run_id"], request)
        with self.factory() as db:
            existing = db.get(Evidence, eid)
            if existing:
                return eid
            key = (
                "hypothesis-tool:"
                + state["run_id"]
                + ":"
                + hashlib.sha256(json.dumps(raw, sort_keys=True).encode()).hexdigest()
            )
            prior = db.scalar(select(Timeline.id).where(Timeline.event_key == key))
            count = db.scalar(
                select(func.count())
                .select_from(Timeline)
                .where(
                    Timeline.incident_id == state["incident_id"],
                    Timeline.event_type == "hypothesis_tool_requested",
                    Timeline.event_key.startswith("hypothesis-tool:" + state["run_id"] + ":"),
                )
            )
            if not prior:
                if count >= MAX_TOOLS:
                    raise ValueError("Tool budget exhausted")
                timeline(
                    db,
                    db.get(Incident, state["incident_id"]),
                    "hypothesis_tool_requested",
                    "investigator",
                    key,
                    {"run_id": state["run_id"], "ordinal": count + 1, "request": raw},
                )
                db.commit()
        if prior:
            stamp = now().replace(tzinfo=timezone.utc)
            result = ToolResult(
                evidence_id=eid,
                tool_name=request.tool_name,
                source="investigation",
                target=raw,
                requested_at=stamp,
                observed_at=stamp,
                errors=["interrupted_readonly_query"],
                data=None,
            )
        else:
            try:
                result = self.adapter.call(request)
            except Exception as exc:
                stamp = now().replace(tzinfo=timezone.utc)
                result = ToolResult(
                    evidence_id=eid,
                    tool_name=request.tool_name,
                    source="investigation",
                    target=raw,
                    requested_at=stamp,
                    observed_at=stamp,
                    errors=[type(exc).__name__],
                    data=None,
                )
        with self.factory() as db:
            # A model/test adapter may not redirect a persisted observation to another target.
            if result.target != raw:
                raise ValueError("Tool result target mismatch")
            result = save(db, state["incident_id"], result, state["run_id"])
            db.commit()
            return result.evidence_id

    def seed(self, state):
        self.heartbeat()
        ids = list(state.get("evidence_ids", []))
        link = self.linked_task(state)
        names = ["get_task_status", "get_pod_status"] if link else ["get_pod_status", "query_logs"]
        for name in names:
            ids.append(self.probe(state, self.request(state, name)))
        # An alert's task link comes from structured logs, never from a model.
        task_link = {}
        if not link:
            with self.factory() as db:
                for eid in ids:
                    row = db.get(Evidence, eid)
                    for line in (
                        (json.loads(row.payload_json or "{}") or {}).get("lines", [])
                        if row.kind == "query_logs"
                        else []
                    ):
                        try:
                            item = json.loads(line["line"])
                            if (
                                item.get("namespace") == state["namespace"]
                                and item.get("pod") == state["resource_name"]
                            ):
                                task_link = {
                                    "job_id": str(uuid.UUID(item["job_id"])),
                                    "task_id": str(uuid.UUID(item["task_id"])),
                                }
                                break
                        except (ValueError, KeyError, TypeError):
                            continue
            if task_link:
                ids.append(
                    self.probe(
                        {**state, "task_link": task_link},
                        self.request({**state, "task_link": task_link}, "get_task_status"),
                    )
                )
        return {
            "evidence_ids": list(dict.fromkeys(ids)),
            "task_link": task_link,
            "round": 0,
            "successful_rounds": 0,
            "investigation_history": [],
            "hypothesis_references": {},
            "tools_used": len(set(ids)),
        }

    def context(self, state):
        with self.factory() as db:
            refs = [
                {
                    "chunk_id": row.id,
                    "heading": row.heading,
                    "text": row.content[:1000],
                    "version": row.document_version,
                }
                for cid in state.get("runbook_chunk_ids", [])[:12]
                if (row := db.get(KBChunk, cid))
            ]
        from app.aiops.actions import SPECS

        return {
            "namespace": state["namespace"],
            "symptom": "video processing incident",
            "round": state.get("round", 0) + 1,
            "observations": [
                {
                    **preview(r),
                    "data": {k: preview(v) for k, v in r["data"].items() if k != "error_kind"}
                    if r["kind"] == "get_task_status" and isinstance(r.get("data"), dict)
                    else preview(r.get("data")),
                }
                for r in self.observations(state)
            ],
            "references": refs,
            "previous_candidate": state.get("candidate"),
            "prior_assessments": state.get("assessments", []),
            "tool_budget_remaining": 0
            if state.get("round", 0) + 1 >= MAX_ROUNDS
            else max(0, MAX_TOOLS - state.get("tools_used", 0)),
            "rounds_remaining": MAX_ROUNDS - state.get("round", 0),
            "executable_actions": [
                {"action_type": k, "required_cause": v["root"]}
                for k, v in SPECS.items()
                if v["namespace"] == state["namespace"]
            ],
            "read_only_tools": [
                "get_pod_status",
                "get_pod_events",
                "query_logs",
                "query_metric",
                "get_queue_depth",
                "get_task_status",
            ]
            + (
                ["probe_video_input", "get_encoder_inventory"]
                if state["namespace"] == "video-platform"
                else ["get_pod_logs"]
            ),
            "model_feedback": {
                "error": state.get("investigation_error", ""),
                "issues": state.get("validation_issues", []),
                "assertion_corrections": [
                    {"hypothesis_id": a["hypothesis_id"], **c}
                    for a in state.get("assessments", [])
                    for c in a.get("checks", [])
                    if c.get("reason") == "type_mismatch"
                ],
            },
            "request_errors": state.get("source_gaps", []),
            "note": "Assertions use paths relative to original observation.data. Model previews may omit long values; absence from preview is not negative evidence.",
        }

    def hypothesize(self, state):
        self.heartbeat()
        number = state.get("round", 0) + 1
        try:
            value = self.llm.investigate(self.context(state))
            candidate = value if isinstance(value, RoundOutput) else parse_round(value)
            previous = state.get("candidate", {})
            known = {h["id"]: h["cause"] for h in previous.get("hypotheses", [])}
            if any(h.id in known and known[h.id] != h.cause for h in candidate.hypotheses):
                raise ValueError("Hypothesis ID cannot change cause")
            if any(hid not in {h.id for h in candidate.hypotheses} for hid in known):
                raise ValueError("Prior hypotheses must remain visible")
            with self.factory() as db:
                timeline(
                    db,
                    db.get(Incident, state["incident_id"]),
                    "hypotheses_proposed",
                    "model",
                    f"hypotheses:{state['run_id']}:{number}",
                    {
                        "run_id": state["run_id"],
                        "round": number,
                        "candidate": sanitize(candidate.model_dump()),
                    },
                )
                db.commit()
            return {
                "round": number,
                "successful_rounds": state.get("successful_rounds", 0) + 1,
                "investigation_error": "",
                "model_error_kind": "",
                "validation_issues": candidate._validation_issues,
                "stop_reason": "",
                "candidate": candidate.model_dump(),
                "requests": [r.model_dump() for r in candidate.requests],
                "hypotheses": [h.model_dump() for h in candidate.hypotheses],
            }
        except Exception as exc:
            import httpx

            kind = (
                "format_error"
                if isinstance(exc, ValueError)
                else "model_timeout"
                if isinstance(exc, (TimeoutError, httpx.TimeoutException))
                else "model_connection_error"
                if isinstance(exc, httpx.HTTPError)
                else "model_error"
            )
            issues = validation_details(exc) if kind == "format_error" else []
            feedback = json.dumps(issues, ensure_ascii=False) if issues else kind
            return {
                "round": number,
                "investigation_error": feedback,
                "model_error_kind": kind,
                "validation_issues": issues,
                "requests": [],
                "stop_reason": "" if kind == "format_error" and number < MAX_ROUNDS else kind,
            }

    def retrieve_hypotheses(self, state):
        self.heartbeat()
        ids = list(state.get("runbook_chunk_ids", []))
        mapping = dict(state.get("hypothesis_references", {}))
        entries = []
        if state.get("investigation_error"):
            return {}
        for h in state.get("hypotheses", []):
            found = []
            try:
                with self.factory() as db:
                    result = self.retriever(
                        db,
                        SearchInput(
                            query=h["search_query"] + " 驗證與反證 排除條件",
                            service="transcoder",
                            top_k=2,
                        ),
                    )
                    found = [r["chunk_id"] for r in result["items"]]
            except Exception:
                pass
            mapping[h["id"]] = list(dict.fromkeys(mapping.get(h["id"], []) + found))
            ids.extend(found)
            entries.append(
                {"hypothesis_id": h["id"], "query": h["search_query"], "chunk_ids": found}
            )
        with self.factory() as db:
            timeline(
                db,
                db.get(Incident, state["incident_id"]),
                "hypothesis_runbooks_retrieved",
                "retriever",
                f"hypothesis-rag:{state['run_id']}:{state['round']}",
                {"run_id": state["run_id"], "round": state["round"], "retrievals": entries},
            )
            db.commit()
        return {
            "runbook_chunk_ids": list(dict.fromkeys(ids))[:18],
            "hypothesis_references": mapping,
        }

    def investigation_queue(self, state, records):
        """Complete model-declared fixed checks before optional model requests.

        The model still supplies hypotheses, expected values and evidence citations.
        Never infer a metric/log selector or pick a repair from the failure category.
        """
        from app.aiops.hypotheses import InvestigationRequest

        candidate = RoundOutput.model_validate(state["candidate"])
        blocked = (
            {
                a["hypothesis_id"]
                for history in state.get("investigation_history", [])
                for a in history.get("assessments", [])
                if a["status"] == "contradicted"
            }
            | {
                h.id
                for h in candidate.hypotheses
                if evaluate(h, records)["status"] == "contradicted"
            }
            | {
                issue["hypothesis_id"]
                for issue in state.get("validation_issues", [])
                if issue.get("hypothesis_id")
            }
        )
        selected = candidate.selected_hypothesis_id
        leading = next(
            (
                h
                for h in candidate.hypotheses
                if h.id not in blocked and (h.id == selected if selected else True)
            ),
            None,
        )
        # A failed or truncated snapshot is still a completed query. Repeating it
        # cannot create new evidence; the model must report its uncertainty.
        completed = {r["kind"] for r in records}
        queue = []
        if leading:
            for tool in dict.fromkeys(leading.required_tools):
                if tool in FIXED_REQUIRED_TOOLS and tool in completed:
                    continue
                explicit = next(
                    (
                        r
                        for r in candidate.requests
                        if r.hypothesis_id == leading.id and r.tool_name == tool
                    ),
                    None,
                )
                if explicit:
                    queue.append((explicit, "model_required_request"))
                elif tool in FIXED_REQUIRED_TOOLS:
                    queue.append(
                        (
                            InvestigationRequest(
                                hypothesis_id=leading.id,
                                tool_name=tool,
                                purpose="Complete model-declared required read-only check",
                            ),
                            "required_fixed_check",
                        )
                    )
        queue.extend((r, "model_request") for r in candidate.requests)
        return queue

    def investigate(self, state):
        self.heartbeat()
        ids = list(state.get("evidence_ids", []))
        rejected = []
        scheduled = []
        known = set(ids)
        if state["round"] < MAX_ROUNDS and not state.get("investigation_error"):
            queue = self.investigation_queue(state, self.observations(state))
        else:
            queue = []
        for value, origin in queue:
            if len(scheduled) >= min(2, max(0, MAX_TOOLS - len(set(ids)))):
                break
            try:
                request = self.request(
                    state,
                    value.tool_name,
                    lookback_seconds=value.lookback_seconds,
                    metric_key=value.metric_key,
                    query_key=value.query_key,
                )
                eid = observation_id(state["run_id"], request)
                if eid in known:
                    continue
                known.add(eid)
                scheduled.append(
                    {
                        "hypothesis_id": value.hypothesis_id,
                        "origin": origin,
                        "request": request.model_dump(mode="json"),
                    }
                )
            except (ValueError, PermissionError) as exc:
                rejected.append(type(exc).__name__)
        if scheduled:
            with self.factory() as db:
                timeline(
                    db,
                    db.get(Incident, state["incident_id"]),
                    "hypothesis_tools_scheduled",
                    "investigator",
                    f"tool-schedule:{state['run_id']}:{state['round']}",
                    {"run_id": state["run_id"], "round": state["round"], "queries": scheduled},
                )
                db.commit()
        for item in scheduled:
            try:
                ids.append(self.probe(state, ToolRequest.model_validate(item["request"])))
            except (ValueError, PermissionError) as exc:
                rejected.append(type(exc).__name__)
        return {
            "evidence_ids": list(dict.fromkeys(ids)),
            "tools_used": len(set(ids)),
            "source_gaps": (["Investigation request rejected"] if rejected else []),
            "scheduled_requests": scheduled,
        }

    def assess(self, state):
        self.heartbeat()
        assessments = []
        if not state.get("investigation_error"):
            candidate = RoundOutput.model_validate(state["candidate"])
            records = self.observations(state)
            for h in candidate.hypotheses:
                assessment = evaluate(h, records)
                issues = [
                    e for e in state.get("validation_issues", []) if e.get("hypothesis_id") == h.id
                ]
                if issues:
                    assessment.update(status="invalid", validation_errors=issues)
                if any(
                    old["hypothesis_id"] == h.id and old["status"] == "contradicted"
                    for round in state.get("investigation_history", [])
                    for old in round["assessments"]
                ):
                    assessment["status"] = "contradicted"
                    assessment["prior_contradiction_retained"] = True
                assessments.append(assessment)
        history = state.get("investigation_history", []) + [
            {
                "round": state["round"],
                "candidate": state.get("candidate"),
                "assessments": assessments,
                "model_error": state.get("investigation_error", ""),
                "model_error_kind": state.get("model_error_kind", ""),
                "validation_issues": state.get("validation_issues", []),
                "evidence_ids": state.get("evidence_ids", []),
                "scheduled_requests": state.get("scheduled_requests", []),
            }
        ]
        with self.factory() as db:
            timeline(
                db,
                db.get(Incident, state["incident_id"]),
                "hypotheses_checked",
                "verifier",
                f"hypothesis-check:{state['run_id']}:{state['round']}",
                {"run_id": state["run_id"], "round": state["round"], "assessments": assessments},
            )
            db.commit()
        reason = state.get("stop_reason", "")
        selected = next(
            (
                a
                for a in assessments
                if a["hypothesis_id"]
                == (state.get("candidate") or {}).get("selected_hypothesis_id")
            ),
            None,
        )
        corrections = bool(state.get("validation_issues")) or any(
            c.get("reason") == "type_mismatch" for a in assessments for c in a.get("checks", [])
        )
        selected_option = (state.get("candidate") or {}).get("selected_option_id")
        selected_plan_invalid = any(
            e.get("option_id") == selected_option
            for e in state.get("validation_issues", [])
            if e.get("option_id")
        )
        if not reason and state["round"] >= MAX_ROUNDS:
            reason = (
                "format_error"
                if selected_plan_invalid
                else "completed_with_warnings"
                if selected and selected["status"] == "supported" and corrections
                else "format_error"
                if selected and selected["status"] == "invalid"
                else "round_budget"
                if selected and selected["status"] == "supported"
                else "evidence_incomplete"
            )
        elif not reason and state.get("tools_used", 0) >= MAX_TOOLS:
            reason = "tool_budget"
        elif (
            not reason
            and not state.get("investigation_error")
            and not corrections
            and not state.get("requests")
            and state.get("successful_rounds", 0) >= 2
        ):
            reason = "model_finished"
        return {"assessments": assessments, "investigation_history": history, "stop_reason": reason}

    def compose(self, state):
        self.heartbeat()
        records = self.observations(state)
        candidate = None
        selected = None
        plan = None
        gaps = []
        if state.get("candidate") and not state.get("investigation_error"):
            candidate = RoundOutput.model_validate(state["candidate"])
            selected = next(
                (h for h in candidate.hypotheses if h.id == candidate.selected_hypothesis_id), None
            )
            plan = next(
                (p for p in candidate.repair_options if p.id == candidate.selected_option_id), None
            )
        assessment = next(
            (
                a
                for a in state.get("assessments", [])
                if selected and a["hypothesis_id"] == selected.id
            ),
            {},
        )
        supported = assessment.get("status") == "supported" and len(selected.assertions) >= 2
        if state.get("investigation_error"):
            gaps.append(state["investigation_error"])
        elif not supported:
            gaps.append("選定假說缺少足夠且未被反駁的工具證據。")
            if assessment.get("missing_required_tools"):
                gaps.append(
                    "必要工具尚未取得有效引用：" + "、".join(assessment["missing_required_tools"])
                )
        action = "none"
        policy_reason = "No executable reviewed plan; human investigation required"
        refs = list(plan.runbook_chunk_ids) if plan else []
        references_ok = bool(
            selected
            and plan
            and plan.hypothesis_id == selected.id
            and refs
            and set(refs) <= set(state.get("hypothesis_references", {}).get(selected.id, []))
        )
        invalid_plan = bool(
            plan and any(e.get("option_id") == plan.id for e in state.get("validation_issues", []))
        )
        if invalid_plan:
            gaps.append("選定修復方案格式或引用不符，尚不能建立修復草案；不會自動改選其他方案。")
            policy_reason = "選定修復方案格式或引用不符；需修正方案後重新審核。"
        elif plan and not references_ok:
            gaps.append("修復方案缺少有效且對應此假說的 Runbook 引用。")
        if (
            supported
            and references_ok
            and repair_eligible(plan.action_type, selected, records, self.linked_task(state))
        ):
            action = plan.action_type
            policy_reason = None
        evidence_ids = (
            list(dict.fromkeys(c.evidence_id for c in selected.assertions)) if selected else []
        )
        valid = supported and not gaps
        reason = state.get("stop_reason")
        if not valid and reason in {"model_finished", "completed_with_warnings", "round_budget"}:
            reason = "evidence_incomplete"
        option_checks = []
        for option in candidate.repair_options if candidate else []:
            h = next(x for x in candidate.hypotheses if x.id == option.hypothesis_id)
            check = next(
                (a for a in state.get("assessments", []) if a["hypothesis_id"] == h.id), {}
            )
            cited = bool(option.runbook_chunk_ids) and set(option.runbook_chunk_ids) <= set(
                state.get("hypothesis_references", {}).get(h.id, [])
            )
            eligible = (
                check.get("status") == "supported"
                and len(h.assertions) >= 2
                and cited
                and repair_eligible(option.action_type, h, records, self.linked_task(state))
            )
            errors = [
                e for e in state.get("validation_issues", []) if e.get("option_id") == option.id
            ]
            option_checks.append(
                {
                    "option_id": option.id,
                    "eligible_for_policy": bool(eligible) and not errors,
                    "validation_errors": errors,
                    "reason": "方案格式或引用錯誤，不能執行。"
                    if errors
                    else "May proceed to target policy checks; human approval still required"
                    if eligible
                    else "Advisory only or action preconditions not satisfied",
                }
            )
        output = {
            "root_cause_code": selected.cause if supported else "INSUFFICIENT_EVIDENCE",
            "summary": candidate.summary if candidate else "無法完成假說調查，需人工查核。",
            "alternative_causes": [
                h.explanation
                for h in candidate.hypotheses
                if h.id != candidate.selected_hypothesis_id
                and not any(
                    a["hypothesis_id"] == h.id and a["status"] == "contradicted"
                    for a in state.get("assessments", [])
                )
            ]
            if candidate
            else [],
            "evidence_ids": evidence_ids,
            "runbook_chunk_ids": refs if references_ok else [],
            "missing_evidence": gaps,
            "follow_up_checks": selected.follow_up_checks if selected else [],
            "required_observations": [],
            "proposed_action_type": action if valid else "none",
            "rationale": plan.rationale if plan else "",
            "investigation": {
                "version": PROMPT_VERSION,
                "field_catalog_version": "scalar-catalog-v2-stable",
                "candidate": state.get("candidate"),
                "assessments": state.get("assessments", []),
                "history": state.get("investigation_history", []),
                "evidence_ids": state.get("evidence_ids", []),
                "hypothesis_references": state.get("hypothesis_references", {}),
                "repair_option_checks": option_checks,
                "rounds": state["round"],
                "tools_used": state.get("tools_used", 0),
                "stop_reason": reason,
                "validation_issues": state.get("validation_issues", []),
                "model_error_kind": state.get("model_error_kind", ""),
                "interpretation": "Assertions are tool-supported; causal explanation remains a model hypothesis.",
            },
        }
        with self.factory() as db:
            row = db.get(Diagnosis, state["run_id"])
            if not row:
                row = Diagnosis(
                    run_id=state["run_id"],
                    incident_id=state["incident_id"],
                    thread_id=thread_id(state["incident_id"], state["run_id"]),
                    model_id=self.llm.model_id,
                    prompt_version=PROMPT_VERSION,
                    created_at=now(),
                )
                db.add(row)
            row.model_id = self.llm.model_id[:128]
            row.prompt_version = PROMPT_VERSION
            row.payload_json = json.dumps(sanitize(output), ensure_ascii=False)
            row.validation_status = "validated" if valid else "insufficient_evidence"
            db.commit()
        return {
            "validation_status": "validated" if valid else "insufficient_evidence",
            "gaps": gaps,
            "stop_reason": reason,
            "proposed_action_type": action if valid else "none",
            "policy_reason": policy_reason,
            "policy_status": "no_action",
        }

    def plan(self, state):
        if not self.settings.aiops_hypothesis_remediation_enabled:
            return {
                "policy_status": "no_action",
                "policy_reason": "Hypothesis v2 is in read-only rollout mode; no repair proposal is created",
            }
        return super().plan(state)

    def finalize(self, state):
        self.heartbeat()
        with self.factory() as db:
            run = db.get(DiagnosisRun, state["run_id"], with_for_update=True)
            incident = db.get(Incident, state["incident_id"], with_for_update=True)
            row = db.get(Diagnosis, run.id)
            output = json.loads(row.payload_json)
            output.update(
                validation_gaps=state.get("gaps", []),
                policy_status=state.get("policy_status", "no_action"),
                policy_reason=state.get("policy_reason"),
                action_id=state.get("proposed_action_id"),
                recovery_verified=state.get("recovery_verified", False),
                suggestions_are_observations=False,
            )
            row.payload_json = json.dumps(output, ensure_ascii=False)
            run.prompt_version = PROMPT_VERSION
            run.model_id = row.model_id
            run.status = "completed"
            run.finished_at = now()
            target = (
                "RESOLVED"
                if state.get("recovery_verified")
                else "REJECTED"
                if state.get("approval_status") == "rejected"
                else "ESCALATED"
            )
            if target in STATES.get(incident.lifecycle, set()):
                transition(
                    db,
                    incident,
                    target,
                    "hypothesis-worker",
                    "Hypothesis investigation and recovery outcome recorded",
                )
            timeline(
                db,
                incident,
                "diagnosis_finalized",
                "hypothesis-worker",
                "diagnosis:" + run.id,
                {
                    "run_id": run.id,
                    "workflow_kind": WORKFLOW,
                    "validation_status": row.validation_status,
                    "rounds": state.get("round"),
                    "tools_used": state.get("tools_used"),
                    "mutation_executed": state.get("execution_status")
                    in {"succeeded", "uncertain"},
                },
            )
            db.commit()
        return {}


def build_hypothesis(runtime, checkpointer, interrupt_after=None):
    b = StateGraph(InvestigationState)
    nodes = {
        "initial_observations": runtime.seed,
        "propose_hypotheses": runtime.hypothesize,
        "retrieve_hypothesis_runbooks": runtime.retrieve_hypotheses,
        "investigate_hypotheses": runtime.investigate,
        "check_hypotheses": runtime.assess,
        "compose_hypothesis_report": runtime.compose,
        "plan_remediation": runtime.plan,
        "policy_check": runtime.policy_check,
        "approval_interrupt": runtime.approval,
        "execute_approved_action": runtime.execution,
        "verify_recovery": runtime.verify,
        "finalize_report": runtime.finalize,
    }
    for name, fn in nodes.items():
        b.add_node(name, fn)
    b.add_edge(START, "initial_observations")
    b.add_edge("initial_observations", "propose_hypotheses")
    b.add_edge("propose_hypotheses", "retrieve_hypothesis_runbooks")
    b.add_edge("retrieve_hypothesis_runbooks", "investigate_hypotheses")
    b.add_edge("investigate_hypotheses", "check_hypotheses")
    b.add_conditional_edges(
        "check_hypotheses",
        lambda s: "finish" if s.get("stop_reason") else "loop",
        {"finish": "compose_hypothesis_report", "loop": "propose_hypotheses"},
    )
    b.add_edge("compose_hypothesis_report", "plan_remediation")
    b.add_edge("plan_remediation", "policy_check")
    b.add_conditional_edges(
        "policy_check",
        lambda s: "approve" if s.get("policy_status") == "allowed" else "finish",
        {"approve": "approval_interrupt", "finish": "finalize_report"},
    )
    b.add_conditional_edges(
        "approval_interrupt",
        lambda s: "execute" if s.get("approval_status") == "approved" else "finish",
        {"execute": "execute_approved_action", "finish": "finalize_report"},
    )
    b.add_edge("execute_approved_action", "verify_recovery")
    b.add_edge("verify_recovery", "finalize_report")
    b.add_edge("finalize_report", END)
    return b.compile(checkpointer=checkpointer, interrupt_after=interrupt_after or [])
