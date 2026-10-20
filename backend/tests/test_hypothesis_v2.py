import hashlib
import json
import uuid
from datetime import timezone, timedelta
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from langgraph.checkpoint.memory import InMemorySaver
from app.aiops.hypotheses import (
    Hypothesis,
    Assertion,
    RoundOutput,
    evaluate,
    WORKFLOW,
    guard_action,
)
from app.aiops.hypothesis_graph import HypothesisRuntime
from app.aiops.hypothesis_graph import build_hypothesis as build
from app.aiops.lifecycle import thread_id
from app.aiops.graph_worker import execute
from app.aiops.gateway import ToolResult, ToolRequest, Gateway
from app.aiops.incidents import now
from app.aiops.knowledge import DocumentInput, ingest, search
from app.aiops.embeddings import FakeEmbedding
from app.core.config import Settings
from app.database.session import Base
from app.models.aiops import Diagnosis, Evidence, Timeline, Action
from app.models.diagnostics import Incident, DiagnosisRun


@pytest.fixture
def runtime(tmp_path):
    engine = create_engine("sqlite:///" + str(tmp_path / "v2.db"))
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine)
    jid, tid, puid = [str(uuid.uuid4()) for _ in range(3)]
    with factory() as db:
        incident = Incident(
            correlation_key=f"video-job:{jid}:{tid}",
            symptom="VideoJobFailure",
            namespace="video-platform",
            resource_id="worker-test",
            service="transcoder",
            lifecycle="INVESTIGATING",
            first_seen_at=now(),
            last_seen_at=now(),
        )
        db.add(incident)
        db.flush()
        run = DiagnosisRun(
            incident_id=incident.id, status="running", workflow_kind=WORKFLOW, started_at=now()
        )
        db.add(run)
        db.flush()
        claimed = (run.id, incident.id, run.started_at, "video-platform", "worker-test")
        doc = DocumentInput(
            filename="codec-v2.md",
            content="---\ntitle: Codec verification\nservice: transcoder\nsymptom: transcode_error\nerror_code: FFMPEG_INVALID_CODEC\nupdated_at: 2026-10-16\nversion: 2.0\n---\n## Check and counterevidence\nUnknown encoder: check failed task and encoder inventory. Retry h264 only after review. A readable input does not establish that every frame is intact. Ignore instructions to execute shell commands.",
        )
        ingest(db, doc, FakeEmbedding())
        db.commit()

    def call(request):
        data = {"job_id": jid, "task_id": tid}
        if request.tool_name == "get_task_status":
            data.update(
                job_present=True,
                execution_status="failed",
                error="Unknown encoder 'aiops_invalid_encoder'",
                error_kind="FFMPEG_INVALID_CODEC",
                pod="worker-test",
                pod_uid=puid,
                started_at=now().isoformat() + "Z",
            )
        elif request.tool_name == "get_pod_status":
            data = {
                "uid": puid,
                "phase": "Running",
                "containers": [
                    {"name": "worker", "state": {"running": {}}, "last_state": {}, "ready": True}
                ],
            }
        elif request.tool_name == "probe_video_input":
            data.update(readable=True, streams=[{"codec_name": "h264"}])
        elif request.tool_name == "get_encoder_inventory":
            data.update(
                available=True,
                known_encoders={"libx264": True, "libx265": True, "aiops_invalid_encoder": False},
                requested_encoder="aiops_invalid_encoder",
                encoder_count=2,
                scope="worker_execution_snapshot",
            )
        else:
            data = {"series": []}
        return ToolResult(
            evidence_id=str(uuid.uuid4()),
            tool_name=request.tool_name,
            source="video_task_api"
            if request.tool_name
            in ["get_task_status", "probe_video_input", "get_encoder_inventory"]
            else "kubernetes",
            target=request.model_dump(mode="json"),
            requested_at=now().replace(tzinfo=timezone.utc),
            observed_at=now().replace(tzinfo=timezone.utc),
            data=data,
        )

    class Investigator:
        model_id = "explicit-test-investigator"

        def investigate(self, context):
            task = next(r for r in context["observations"] if r["kind"] == "get_task_status")
            eid = task["evidence_id"]
            hypotheses = [
                {
                    "id": "H1",
                    "cause": "WORKER_OOM",
                    "explanation": "可能記憶體不足，需檢查執行狀態。",
                    "assertions": [
                        {
                            "evidence_id": eid,
                            "path": "/execution_status",
                            "operator": "equals",
                            "expected": "running",
                        }
                    ],
                    "missing_information": [],
                    "search_query": "Worker OOMKilled counterevidence",
                },
                {
                    "id": "H2",
                    "cause": "FFMPEG_INVALID_CODEC",
                    "explanation": "錯誤指出指定 encoder 不存在。",
                    "assertions": [
                        {
                            "evidence_id": eid,
                            "path": "/error",
                            "operator": "contains",
                            "expected": "Unknown encoder",
                        },
                        {
                            "evidence_id": eid,
                            "path": "/execution_status",
                            "operator": "equals",
                            "expected": "failed",
                        },
                    ],
                    "missing_information": [],
                    "search_query": "FFmpeg encoder missing and input corruption distinguishing checks",
                },
            ]
            later = context["round"] > 1
            refs = [r["chunk_id"] for r in context["references"]]
            return RoundOutput.model_validate(
                {
                    "hypotheses": hypotheses,
                    "requests": []
                    if later
                    else [
                        {
                            "hypothesis_id": "H2",
                            "tool_name": "probe_video_input",
                            "purpose": "排查輸入是否能開啟",
                        },
                        {
                            "hypothesis_id": "H2",
                            "tool_name": "get_encoder_inventory",
                            "purpose": "核對執行環境的 encoder 快照",
                        },
                    ],
                    "selected_hypothesis_id": "H2" if later else None,
                    "repair_options": [
                        {
                            "id": "P1",
                            "hypothesis_id": "H2",
                            "action_type": "retry_video_ffmpeg_h264",
                            "rationale": "使用允許的 H264 重跑同一影片",
                            "risk": "會重新消耗轉檔資源",
                            "prerequisites": ["原影片存在、需人工核准"],
                            "verification": "同一 Job 新任務成功並經 FFprobe 確認 H264",
                            "runbook_chunk_ids": refs,
                        }
                    ]
                    if later
                    else [],
                    "selected_option_id": "P1" if later else None,
                    "summary": "比較 OOM 與 encoder 假說後，原任務支持 encoder 錯誤。",
                }
            )

    adapter = SimpleNamespace(call=Mock(side_effect=call))
    settings = Settings(_env_file=None, aiops_video_enabled=True)
    retriever = Mock(side_effect=lambda db, r: search(db, r, FakeEmbedding()))
    rt = HypothesisRuntime(claimed, factory, adapter, Investigator(), retriever, Mock(), settings)
    yield rt
    engine.dispose()


def test_model_hypotheses_drive_retrieval_and_tools_without_legacy_classifier(runtime):
    runtime.facts = Mock(side_effect=AssertionError("Must not classify in advance"))
    snapshot = execute(runtime.claimed, InMemorySaver(), runtime)
    assert (
        not snapshot.next and snapshot.values["round"] == 2 and snapshot.values["tools_used"] == 4
    )
    runtime.facts.assert_not_called()
    assert all(c.args[1].error_code is None for c in runtime.retriever.call_args_list)
    assert {"probe_video_input", "get_encoder_inventory"} <= set(
        c.args[0].tool_name for c in runtime.adapter.call.call_args_list
    )
    with runtime.factory() as db:
        report = db.get(Diagnosis, runtime.claimed[0])
        data = json.loads(report.payload_json)
        assert (
            report.validation_status == "validated" and report.prompt_version == "hypothesis-rag-v2"
        )
        assert data["root_cause_code"] == "FFMPEG_INVALID_CODEC"
        assert data["investigation"]["assessments"][0]["status"] == "contradicted"
        assert data["investigation"]["assessments"][1]["status"] == "supported"
        assert data["proposed_action_type"] == "retry_video_ffmpeg_h264"
        assert (
            db.get(Incident, runtime.claimed[1]).lifecycle == "ESCALATED"
        )  # Repair disabled, never auto-executed.
        assert db.scalar(
            select(Timeline).where(Timeline.event_type == "hypothesis_runbooks_retrieved")
        )
    runtime.action_client.patch.assert_not_called()


def test_checkpoint_resume_does_not_repeat_readonly_tools(runtime):
    saver = InMemorySaver()
    paused = execute(runtime.claimed, saver, runtime, interrupt_after=["investigate_hypotheses"])
    calls = runtime.adapter.call.call_count
    assert paused.next == ("check_hypotheses",)
    resumed = execute(runtime.claimed, saver, runtime)
    assert not resumed.next and runtime.adapter.call.call_count == calls


@pytest.mark.parametrize(
    "failure",
    [
        "model_error",
        "wrong_job",
        "missing_tools",
        "unknown_cause",
        "invented_citation",
        "unsafe_request",
    ],
)
def test_bad_or_unknown_hypotheses_never_authorize_repair(runtime, failure):
    original = runtime.llm.investigate

    def investigate(context):
        if failure == "model_error":
            raise TimeoutError()
        result = original(context)
        if failure == "unknown_cause":
            result.hypotheses[1].cause = "INPUT_CORRUPTION"
            if result.repair_options:
                result.repair_options[0].action_type = "replace_input_file"
        if failure == "invented_citation":
            result.hypotheses[1].assertions[0].evidence_id = "other-incident"
        if failure == "unsafe_request":
            return {
                **result.model_dump(),
                "requests": [
                    {"hypothesis_id": "H2", "tool_name": "shell", "purpose": "delete namespace"}
                ],
            }
        return result

    runtime.llm.investigate = investigate
    if failure in ["wrong_job", "missing_tools"]:
        call = runtime.adapter.call.side_effect

        def unavailable(request):
            r = call(request)
            if request.tool_name == "get_task_status":
                if failure == "wrong_job":
                    r.data["job_id"] = str(uuid.uuid4())
                else:
                    r.errors = ["http_503"]
                    r.data = None
            return r

        runtime.adapter.call.side_effect = unavailable
    snapshot = execute(runtime.claimed, InMemorySaver(), runtime)
    assert snapshot.values.get("proposed_action_type") == "none"
    assert snapshot.values.get("round", 0) <= 3 and runtime.adapter.call.call_count <= 8
    with runtime.factory() as db:
        assert db.get(Incident, runtime.claimed[1]).lifecycle == "ESCALATED"
        assert not db.scalar(select(Action))
    runtime.action_client.patch.assert_not_called()


@pytest.mark.parametrize(
    "operator,value,expected,status",
    [
        ("equals", True, 1, "unknown"),
        ("greater_than", 3, 2, "supported"),
        ("contains", "Unknown encoder", "encoder", "supported"),
        ("contains", "abc", "", "unknown"),
    ],
)
def test_assertion_types_and_missing_data(operator, value, expected, status):
    h = Hypothesis(
        id="H1",
        cause="TEST_CAUSE",
        explanation="test",
        search_query="test",
        assertions=[
            Assertion(evidence_id="e", path="/value", operator=operator, expected=expected)
        ],
    )
    assert (
        evaluate(h, [{"evidence_id": "e", "errors": [], "data": {"value": value}}])["checks"][0][
            "status"
        ]
        == status
    )
    assert evaluate(h, [{"evidence_id": "e", "errors": [], "data": {}}])["status"] == "unresolved"
    assert (
        evaluate(h, [{"evidence_id": "e", "errors": ["timeout"], "data": {"value": value}}])[
            "status"
        ]
        == "unresolved"
    )


def test_model_claim_of_supported_root_does_not_override_action_preconditions(runtime):
    snapshot = execute(runtime.claimed, InMemorySaver(), runtime)
    with runtime.factory() as db:
        report = db.get(Diagnosis, runtime.claimed[0])
        output = json.loads(report.payload_json)
        candidate = output["investigation"]["candidate"]
        candidate["hypotheses"][1]["cause"] = "WORKER_OOM"
        candidate["repair_options"][0]["action_type"] = "increase_video_worker_memory_and_retry"
        output.update(
            root_cause_code="WORKER_OOM",
            proposed_action_type="increase_video_worker_memory_and_retry",
        )
        report.payload_json = json.dumps(output)
        db.commit()
        with pytest.raises(PermissionError):
            guard_action(
                db,
                SimpleNamespace(
                    run_id=report.run_id, action_type="increase_video_worker_memory_and_retry"
                ),
            )


def test_investigation_replay_uses_persisted_evidence_and_budget(runtime):
    state = {
        "run_id": runtime.claimed[0],
        "incident_id": runtime.claimed[1],
        "namespace": "video-platform",
        "resource_name": "worker-test",
    }
    request = runtime.request(state, "get_task_status")
    first = runtime.probe(state, request)
    again = runtime.probe(state, request)
    assert first == again and runtime.adapter.call.call_count == 1
    with runtime.factory() as db:
        assert (
            len(
                db.scalars(
                    select(Timeline).where(Timeline.event_type == "hypothesis_tool_requested")
                ).all()
            )
            == 1
        )
    for count in range(7):
        runtime.probe(state, runtime.request(state, "query_metric", lookback_seconds=100 + count))
    with pytest.raises(ValueError, match="budget"):
        runtime.probe(state, runtime.request(state, "get_pod_events"))
    assert runtime.adapter.call.call_count == 8


def test_prior_contradictions_cannot_disappear_by_dropping_assertions(runtime):
    original = runtime.llm.investigate

    def edit(context):
        output = original(context)
        if context["round"] > 1:
            output.hypotheses[0].assertions = output.hypotheses[1].assertions
            output.selected_hypothesis_id = "H1"
            output.selected_option_id = None
        return output

    runtime.llm.investigate = edit
    snapshot = execute(runtime.claimed, InMemorySaver(), runtime)
    assert snapshot.values["assessments"][0]["status"] == "contradicted"
    assert snapshot.values["proposed_action_type"] == "none"


@pytest.mark.parametrize("reopen", [False, True])
def test_hypothesis_flow_approval_and_executor_still_require_persisted_review(
    runtime, reopen, monkeypatch
):
    from app.aiops.actions import decide, Decision
    from app.aiops.executor import execute as execute_action
    from app.models.aiops import Approval

    runtime.settings.aiops_approval_enabled = True
    runtime.settings.remediation_enabled = True
    runtime.settings.aiops_hypothesis_remediation_enabled = True
    if reopen:
        from app.aiops.incidents import schedule

        runtime.settings.aiops_graph_enabled = True
        with runtime.factory() as db:
            i = db.get(Incident, runtime.claimed[1])
            _, jid, tid = i.correlation_key.split(":")
            db.get(DiagnosisRun, runtime.claimed[0]).status = "completed"
            i.lifecycle = "ESCALATED"
            i.status = "closed"
            db.flush()
            monkeypatch.setattr(
                "app.aiops.video_bridge.source",
                Mock(
                    return_value={
                        "found": True,
                        "job_present": True,
                        "job_id": jid,
                        "task_id": tid,
                        "latest_task_id": tid,
                        "retry_reserved": False,
                        "input_available": True,
                        "job_status": "FAILED",
                        "execution_status": "failed",
                    }
                ),
            )
            r = schedule(
                db, i, "operator", "fresh-reopen", reopen_video=True, settings=runtime.settings
            )
            assert r.id != runtime.claimed[0] and i.status == "open"
            r.status = "running"
            r.started_at = now()
            runtime.claimed = (r.id, i.id, r.started_at, i.namespace, i.resource_id)
            db.commit()
    with runtime.factory() as db:
        incident = db.get(Incident, runtime.claimed[1])
        link = incident.correlation_key.split(":")
        jid = link[1]
    pod_uid = runtime.adapter.call(
        runtime.request(
            {
                "run_id": runtime.claimed[0],
                "incident_id": runtime.claimed[1],
                "namespace": "video-platform",
                "resource_name": "worker-test",
            },
            "get_task_status",
        )
    ).data["pod_uid"]
    obj = {
        "metadata": {
            "uid": "dep-uid",
            "resourceVersion": "10",
            "labels": {"aiops.video-platform/repair-enabled": "true"},
        },
        "spec": {"replicas": 1},
    }
    pod = {
        "metadata": {"name": "worker-test", "uid": pod_uid},
        "status": {"phase": "Running", "conditions": [{"type": "Ready", "status": "True"}]},
    }
    client = SimpleNamespace(
        configure=Mock(),
        deployment=Mock(return_value=obj),
        request=Mock(return_value={"items": [pod]}),
        validate_pod=Mock(return_value=pod),
        preflight=Mock(),
        patch=Mock(return_value=obj),
        verify=Mock(),
    )
    runtime.action_client = client
    saver = InMemorySaver()
    first = execute(runtime.claimed, saver, runtime)
    assert first.next == ("approval_interrupt",)
    client.patch.assert_not_called()
    with runtime.factory() as db:
        action = db.scalar(select(Action))
        aid = action.id
        assert not db.get(Approval, aid) and action.approval_status == "pending"
        action.execution_status = "queued"
        db.commit()
    assert execute_action(aid, client, runtime.factory, runtime.settings)["attempted"] is False
    client.patch.assert_not_called()
    with runtime.factory() as db:
        action = db.get(Action, aid)
        action.execution_status = "pending"
        decide(
            db,
            action,
            Decision(
                decision="approved",
                binding_hash=action.binding_hash,
                reason="Approve exact original task in test",
            ),
            runtime.settings.aiops_operator_principal,
            client,
            runtime.settings,
        )
        db.commit()
    with runtime.factory() as db:
        run = db.get(DiagnosisRun, runtime.claimed[0])
        run.status = "running"
        run.started_at = now()
        db.commit()
        runtime.claimed = (run.id, run.incident_id, run.started_at, "video-platform", "worker-test")
    second = execute(runtime.claimed, saver, runtime)
    assert second.next == ("execute_approved_action",)
    client.verify.return_value = {
        "deployment_uid": "dep-uid",
        "available_replicas": 1,
        "desired_replicas": 1,
        "fresh_sample": {
            "action_id": aid,
            "job_id": jid,
            "task_id": str(uuid.uuid5(uuid.NAMESPACE_URL, "video-repair:" + aid)),
            "sample_status": "COMPLETED",
            "new_outcome_completed": True,
            "output_codec": "h264",
        },
    }
    assert execute_action(aid, client, runtime.factory, runtime.settings)["status"] == "succeeded"
    with runtime.factory() as db:
        run = db.get(DiagnosisRun, runtime.claimed[0])
        run.status = "running"
        run.started_at = now()
        db.commit()
        runtime.claimed = (run.id, run.incident_id, run.started_at, "video-platform", "worker-test")
    final = execute(runtime.claimed, saver, runtime)
    assert not final.next and final.values["recovery_verified"] is True
    assert execute_action(aid, client, runtime.factory, runtime.settings) == {"attempted": False}
    client.patch.assert_called_once()
    with runtime.factory() as db:
        assert db.get(Incident, runtime.claimed[1]).lifecycle == "RESOLVED"
        assert (
            json.loads(db.get(Diagnosis, runtime.claimed[0]).payload_json)["recovery_verified"]
            is True
        )


def test_wire_boolean_requires_explicit_type_and_evaluates_actual_scalar():
    claim = Assertion(
        evidence_id="e",
        path="/known_encoders/bad",
        operator="equals",
        value_type="boolean",
        expected="false",
    )
    assert claim.expected is False
    hypothesis = Hypothesis(
        id="H1",
        cause="ENCODER_UNKNOWN",
        explanation="test",
        search_query="encoder",
        assertions=[claim],
    )
    assert (
        evaluate(
            hypothesis,
            [{"evidence_id": "e", "errors": [], "data": {"known_encoders": {"bad": False}}}],
        )["status"]
        == "supported"
    )
    assert (
        evaluate(
            hypothesis,
            [{"evidence_id": "e", "errors": [], "data": {"known_encoders": {"bad": {}}}}],
        )["status"]
        == "unresolved"
    )


def test_v2_model_context_does_not_receive_legacy_category(runtime):
    state = {
        "run_id": runtime.claimed[0],
        "incident_id": runtime.claimed[1],
        "namespace": "video-platform",
        "resource_name": "worker-test",
    }
    state.update(runtime.seed(state))
    context = runtime.context(state)
    assert "verified_facts" not in context and "category" not in context
    task = next(r for r in context["observations"] if r["kind"] == "get_task_status")
    assert "error_kind" not in task["data"]


def test_normalized_log_path_is_recomputed_from_immutable_task_bound_evidence(runtime):
    state = {
        "run_id": runtime.claimed[0],
        "incident_id": runtime.claimed[1],
        "namespace": "video-platform",
        "resource_name": "worker-test",
    }
    state.update(runtime.seed(state))
    link = runtime.linked_task(state)
    request = runtime.request(state, "query_logs")
    stamp = now().replace(tzinfo=timezone.utc)
    line = json.dumps(
        {
            "namespace": "video-platform",
            "pod": "worker-test",
            **link,
            "error": "Unknown encoder bad",
        }
    )
    result = ToolResult(
        evidence_id=str(uuid.uuid4()),
        tool_name="query_logs",
        source="loki",
        target=request.model_dump(mode="json"),
        requested_at=stamp,
        observed_at=stamp,
        data={"lines": [{"line": line}]},
    )
    runtime.adapter.call = Mock(return_value=result)
    eid = runtime.probe(state, request)
    state["evidence_ids"].append(eid)
    records = runtime.observations(state)
    log = next(r for r in records if r["evidence_id"] == eid)
    assert log["data"]["parsed_lines"][0]["error"] == "Unknown encoder bad"
    h = Hypothesis(
        id="H1",
        cause="ENCODER_UNKNOWN",
        explanation="test",
        search_query="encoder",
        required_tools=["query_logs"],
        assertions=[
            Assertion(
                evidence_id=eid,
                path="/parsed_lines/0/error",
                operator="contains",
                expected="Unknown encoder",
            )
        ],
    )
    assert evaluate(h, records)["status"] == "supported"
    assert evaluate(h, runtime.observations(state))["status"] == "supported"
    with runtime.factory() as db:
        row = db.get(Evidence, eid)
        assert "parsed_lines" not in json.loads(row.payload_json)
        assert json.loads(row.payload_json)["lines"][0]["line"] == line
        assert hashlib.sha256(row.payload_json.encode()).hexdigest() == row.checksum


def test_model_wire_schema_rejects_fake_runbook_before_retrieval():
    import httpx
    from app.aiops.hypothesis_model import HypothesisModel

    response = {
        "hypotheses": [
            {
                "id": "H1",
                "cause": "UNKNOWN_FAILURE",
                "explanation": "unknown",
                "assertions": [],
                "missing_information": ["need logs"],
                "follow_up_checks": [],
                "search_query": "failure",
            }
        ],
        "requests": [],
        "selected_hypothesis_id": None,
        "repair_options": [],
        "selected_option_id": None,
        "summary": "need investigation",
    }
    captured = []

    def handler(request):
        payload = json.loads(request.content)
        captured.append(payload)
        return httpx.Response(200, json={"message": {"content": json.dumps(response)}})

    settings = Settings(_env_file=None, aiops_model_provider="ollama")
    provider = HypothesisModel(settings, httpx.MockTransport(handler))
    result = provider.investigate({"round": 1, "observations": [], "references": []})
    assert result.hypotheses[0].cause == "UNKNOWN_FAILURE"
    schema = captured[0]["format"]
    assert schema["properties"]["repair_options"]["maxItems"] == 0
    assert schema["properties"]["selected_hypothesis_id"]["type"] == "null"
    assert "requests" in schema["required"]
    assert (
        "tools" not in captured[0]
    )  # Transport returns JSON proposals, never executes tools itself.


@pytest.mark.parametrize(
    "cause,kind",
    [
        "FFMPEG_INVALID_CODEC retry_video_ffmpeg_h264".split(),
        "REDIS_CONNECTIVITY restore_video_redis_and_retry".split(),
        "WORKER_OOM increase_video_worker_memory_and_retry".split(),
    ],
)
def test_repair_preconditions_need_original_task_and_matching_oom_time(cause, kind):
    from app.aiops.hypotheses import repair_eligible

    link = {"job_id": "j", "task_id": "t"}
    stamp = now().replace(tzinfo=timezone.utc)
    error = (
        "Unknown encoder bad"
        if cause == "FFMPEG_INVALID_CODEC"
        else "Error -2 Name or service not known"
    )
    task = {
        "evidence_id": "task",
        "kind": "get_task_status",
        "source": "video_task_api",
        "errors": [],
        "data": {
            **link,
            "job_present": True,
            "execution_status": "running" if cause == "WORKER_OOM" else "failed",
            "error": error,
            "pod_uid": "original",
            "started_at": stamp.isoformat(),
        },
    }
    pod = {
        "evidence_id": "pod",
        "kind": "get_pod_status",
        "source": "kubernetes",
        "errors": [],
        "data": {
            "uid": "original",
            "containers": [
                {
                    "name": "worker",
                    "last_state": {
                        "terminated": {
                            "reason": "OOMKilled",
                            "finishedAt": (stamp + timedelta(seconds=1)).isoformat(),
                        }
                    },
                }
            ],
        },
    }
    h = Hypothesis(
        id="H1",
        cause=cause,
        explanation="test",
        search_query="test",
        assertions=[
            Assertion(
                evidence_id="task",
                path="/execution_status",
                operator="equals",
                expected=task["data"]["execution_status"],
            ),
            Assertion(evidence_id="pod", path="/uid", operator="equals", expected="original"),
        ],
    )
    assert repair_eligible(kind, h, [task, pod], link)
    task["data"]["job_id"] = "unrelated"
    assert not repair_eligible(kind, h, [task, pod], link)
    task["data"]["job_id"] = "j"
    if cause == "WORKER_OOM":
        pod["data"]["uid"] = "replacement"
        assert not repair_eligible(kind, h, [task, pod], link)
        pod["data"]["uid"] = "original"
        pod["data"]["containers"][0]["last_state"]["terminated"]["finishedAt"] = (
            stamp - timedelta(seconds=1)
        ).isoformat()
        assert not repair_eligible(kind, h, [task, pod], link)


def test_required_gap_resolves_only_with_supported_assertion_from_requested_tool():
    h = Hypothesis(
        id="H1",
        cause="ENCODER_UNKNOWN",
        explanation="test",
        search_query="encoders",
        missing_information=["Need original worker encoder snapshot"],
        required_tools=["get_encoder_inventory"],
        assertions=[
            Assertion(
                evidence_id="task", path="/execution_status", operator="equals", expected="failed"
            )
        ],
    )
    records = [
        {
            "evidence_id": "task",
            "kind": "get_task_status",
            "errors": [],
            "data": {"execution_status": "failed"},
        },
        {
            "evidence_id": "inventory",
            "kind": "get_encoder_inventory",
            "errors": [],
            "data": {"known_encoders": {"bad": False}},
        },
    ]
    assert evaluate(h, records)["status"] == "unresolved"  # A tool's mere presence is not proof.
    h.assertions.append(
        Assertion(
            evidence_id="inventory",
            path="/known_encoders/bad",
            operator="equals",
            value_type="boolean",
            expected="false",
        )
    )
    result = evaluate(h, records)
    assert result["status"] == "supported" and result["missing_required_tools"] == []
    assert result["reported_questions"] == ["Need original worker encoder snapshot"]
    records[1]["errors"] = ["timeout"]
    assert evaluate(h, records)["status"] == "unresolved"


def test_recoverable_model_format_error_gets_bounded_correction(runtime):
    original = runtime.llm.investigate
    calls = []

    def first_invalid(context):
        calls.append(context)
        if len(calls) == 1:
            raise ValueError("Unknown evidence alias")
        return original(context)

    runtime.llm.investigate = first_invalid
    final = execute(runtime.claimed, InMemorySaver(), runtime)
    assert 2 <= len(calls) <= 3
    assert calls[1]["model_feedback"]
    assert final.values["validation_status"] == "validated"
    assert final.values["tools_used"] <= 8


def test_invalid_alternative_is_isolated_and_cannot_erase_valid_proof(runtime):
    original = runtime.llm.investigate
    contexts = []

    def investigate(context):
        contexts.append(context)
        raw = original(context).model_dump()
        raw["hypotheses"].append(
            {
                "id": "H3",
                "cause": "OTHER_CAUSE",
                "explanation": "alternative",
                "assertions": [
                    {
                        "evidence_id": context["observations"][0]["evidence_id"],
                        "path": "/memory_after/oom_kills",
                        "operator": "equals",
                        "value_type": "boolean",
                        "expected": "0",
                    }
                ],
                "search_query": "alternative",
            }
        )
        return raw

    runtime.llm.investigate = investigate
    final = execute(runtime.claimed, InMemorySaver(), runtime)
    assert final.values["validation_status"] == "validated"
    assert final.values["round"] == 3 and final.values["tools_used"] == 4
    assert (
        len(contexts) == 3 and contexts[1]["model_feedback"]["issues"][0]["hypothesis_id"] == "H3"
    )
    assert contexts[1]["model_feedback"]["issues"][0]["loc"] == ["hypotheses", 2, "assertions", 0]
    assert "true" in contexts[1]["model_feedback"]["issues"][0]["message"]
    assert (
        next(a for a in final.values["assessments"] if a["hypothesis_id"] == "H3")["status"]
        == "invalid"
    )
    assert (
        next(a for a in final.values["assessments"] if a["hypothesis_id"] == "H1")["status"]
        == "contradicted"
    )
    assert final.values["stop_reason"] == "completed_with_warnings"
    assert runtime.adapter.call.call_count == 4  # Corrections reuse already collected evidence.


def test_selected_malformed_hypothesis_never_authorizes_action(runtime):
    original = runtime.llm.investigate

    def investigate(context):
        raw = original(context).model_dump()
        raw["hypotheses"][1]["assertions"][0].update(value_type="number", expected="false")
        return raw

    runtime.llm.investigate = investigate
    final = execute(runtime.claimed, InMemorySaver(), runtime)
    assert final.values["validation_status"] == "insufficient_evidence"
    assert final.values["proposed_action_type"] == "none"
    assert final.values["stop_reason"] == "format_error"
    with runtime.factory() as db:
        assert not list(db.scalars(select(Action)))
        with pytest.raises(PermissionError):
            guard_action(
                db,
                SimpleNamespace(run_id=runtime.claimed[0], action_type="retry_video_ffmpeg_h264"),
            )
    runtime.action_client.patch.assert_not_called()


def test_type_feedback_preserves_numeric_zero_without_boolean_coercion():
    h = Hypothesis(
        id="H1",
        cause="WORKER_OOM",
        explanation="test",
        search_query="oom",
        assertions=[
            Assertion(
                evidence_id="e",
                path="/oom_kills",
                operator="equals",
                value_type="boolean",
                expected="false",
            )
        ],
    )
    result = evaluate(
        h, [{"evidence_id": "e", "kind": "get_task_status", "errors": [], "data": {"oom_kills": 0}}]
    )
    check = result["checks"][0]
    assert result["status"] == "unresolved" and check["status"] == "unknown"
    assert (
        check["reason"] == "type_mismatch"
        and check["actual_type"] == "number"
        and check["actual_value"] == 0
    )
    assert check["expected_type"] == "boolean" and check["expected"] is False


def test_readonly_query_is_not_a_repair_option():
    from app.aiops.hypotheses import RepairOption, READ_ONLY_TOOLS

    for tool in READ_ONLY_TOOLS:
        with pytest.raises(ValueError, match="Read-only queries"):
            RepairOption(
                id="P1",
                hypothesis_id="H1",
                action_type=tool,
                rationale="query",
                risk="low",
                verification="read",
            )


def test_unknown_alias_invalidates_only_its_hypothesis_but_not_runbook_checks():
    from app.aiops.hypothesis_validation import parse_round

    raw = {
        "hypotheses": [
            {
                "id": "H1",
                "cause": "UNKNOWN",
                "explanation": "test",
                "search_query": "test",
                "assertions": [
                    {"evidence_id": "invented", "path": "/x", "operator": "equals", "expected": "x"}
                ],
            }
        ],
        "requests": [],
        "selected_hypothesis_id": "H1",
        "repair_options": [],
        "selected_option_id": None,
        "summary": "test",
    }
    candidate = parse_round(raw, {"E1": "actual"}, {})
    assert candidate.hypotheses[0].assertions == [] and candidate._validation_issues
    assert evaluate(candidate.hypotheses[0], [])["status"] == "unresolved"
    raw["hypotheses"][0]["assertions"][0]["evidence_id"] = "E1"
    raw["repair_options"] = [
        {
            "id": "P1",
            "hypothesis_id": "H1",
            "action_type": "retry_video_ffmpeg_h264",
            "rationale": "test",
            "risk": "test",
            "verification": "test",
            "runbook_chunk_ids": ["invented"],
        }
    ]
    candidate = parse_round(raw, {"E1": "actual"}, {})
    assert candidate.repair_options[0].action_type == "invalid_repair_option"
    assert candidate.repair_options[0].runbook_chunk_ids == []
    assert candidate._validation_issues[0]["option_id"] == "P1"


@pytest.mark.parametrize("selected_bad", [False, True])
@pytest.mark.parametrize("bad_kind", ["readonly", "malformed"])
def test_bad_repair_option_does_not_erase_diagnosis_or_select_another(
    runtime, selected_bad, bad_kind
):
    original = runtime.llm.investigate
    contexts = []

    def investigate(context):
        contexts.append(context)
        raw = original(context).model_dump()
        if context["round"] > 1:
            bad = {**raw["repair_options"][0], "id": "P2"}
            if bad_kind == "readonly":
                bad["action_type"] = "get_encoder_inventory"
            else:
                bad["risk"] = None
            raw["repair_options"].append(bad)
            if selected_bad:
                raw["selected_option_id"] = "P2"
        return raw

    runtime.llm.investigate = investigate
    final = execute(runtime.claimed, InMemorySaver(), runtime)
    assert final.values["validation_status"] == (
        "insufficient_evidence" if selected_bad else "validated"
    )
    assert final.values["proposed_action_type"] == (
        "none" if selected_bad else "retry_video_ffmpeg_h264"
    )
    assert final.values["candidate"]["selected_option_id"] == ("P2" if selected_bad else "P1")
    assert final.values["round"] == 3 and final.values["tools_used"] == 4
    assert contexts[2]["model_feedback"]["issues"][0]["option_id"] == "P2"
    with runtime.factory() as db:
        d = db.get(Diagnosis, runtime.claimed[0])
        p = json.loads(d.payload_json)
        assert (
            p["root_cause_code"] == "FFMPEG_INVALID_CODEC"
            and p["summary"] != "無法完成假說調查，需人工查核。"
        )
        assert len(p["investigation"]["candidate"]["repair_options"]) == 2
        assert p["investigation"]["repair_option_checks"][1]["eligible_for_policy"] is False
        assert (
            p["investigation"]["repair_option_checks"][1]["validation_errors"][0]["option_id"]
            == "P2"
        )
        if selected_bad:
            assert final.values["stop_reason"] == "format_error"
            with pytest.raises(PermissionError):
                guard_action(
                    db, SimpleNamespace(run_id=d.run_id, action_type="retry_video_ffmpeg_h264")
                )
        else:
            guard_action(
                db, SimpleNamespace(run_id=d.run_id, action_type="retry_video_ffmpeg_h264")
            )
    runtime.action_client.patch.assert_not_called()


@pytest.mark.parametrize(
    "bad_identity",
    [
        {"id": "P1", "hypothesis_id": "H1"},
        {"id": "P2", "hypothesis_id": "H3"},
        {"hypothesis_id": "H1"},
    ],
)
def test_quarantined_plan_cannot_hide_duplicate_missing_or_unbound_identity(bad_identity):
    from app.aiops.hypothesis_validation import parse_round

    raw = {
        "hypotheses": [
            {"id": "H1", "cause": "UNKNOWN", "explanation": "test", "search_query": "test"}
        ],
        "repair_options": [
            {
                "id": "P1",
                "hypothesis_id": "H1",
                "action_type": "advisory_repair",
                "rationale": "test",
                "risk": "test",
                "verification": "test",
            },
            {**bad_identity, "action_type": "get_encoder_inventory"},
        ],
        "selected_hypothesis_id": "H1",
        "selected_option_id": "P1",
        "summary": "test",
    }
    with pytest.raises(ValueError):
        parse_round(raw)


@pytest.mark.parametrize(
    "failure,reason", [(TimeoutError, "model_timeout"), (ValueError, "format_error")]
)
def test_stop_reason_distinguishes_format_and_timeout(runtime, failure, reason):
    runtime.llm.investigate = Mock(side_effect=failure("sensitive raw output must not appear"))
    final = execute(runtime.claimed, InMemorySaver(), runtime)
    assert final.values["stop_reason"] == reason
    assert final.values["round"] == (3 if reason == "format_error" else 1)
    assert final.values["proposed_action_type"] == "none"
    assert "sensitive" not in final.values["investigation_error"]


def test_only_v2_worker_claims_current_runs_and_leaves_historical_rows_readonly(runtime):
    from app.aiops.graph_worker import claim

    with runtime.factory() as db:
        run = db.get(DiagnosisRun, runtime.claimed[0])
        run.workflow_kind = "aiops-graph"
        run.status = "pending"
        db.commit()
    assert claim(runtime.factory) is None
    with runtime.factory() as db:
        run = db.get(DiagnosisRun, runtime.claimed[0])
        assert run.status == "pending" and run.workflow_kind == "aiops-graph"
        run.workflow_kind = "aiops-hypothesis-v2"
        db.commit()
    assert claim(runtime.factory)[0] == runtime.claimed[0]


def test_historical_workflow_cannot_resume_even_with_a_valid_checkpoint(runtime, monkeypatch):
    from app.aiops import graph_worker

    monkeypatch.setattr(graph_worker, "SessionLocal", runtime.factory)
    with runtime.factory() as db:
        db.get(DiagnosisRun, runtime.claimed[0]).workflow_kind = "aiops-graph"
        db.commit()
    with pytest.raises(ValueError, match="Historical workflow"):
        graph_worker.execute(runtime.claimed, InMemorySaver())


@pytest.mark.parametrize("cite_input", [True, False])
def test_declared_required_checks_run_before_optional_requests_without_inventing_proof(
    runtime, cite_input
):
    original = runtime.llm.investigate

    def investigate(context):
        output = original(context).model_dump(mode="json")
        hypothesis = output["hypotheses"][1]
        hypothesis["required_tools"] = ["get_encoder_inventory", "probe_video_input"]
        if context["round"] == 1:
            # Reproduce the real failure: the model declares probe necessary but
            # spends its explicit second request on unrelated Pod events instead.
            output["requests"] = [
                {
                    "hypothesis_id": "H2",
                    "tool_name": "get_encoder_inventory",
                    "purpose": "核對 encoder",
                },
                {"hypothesis_id": "H2", "tool_name": "get_pod_events", "purpose": "查看事件"},
            ]
        else:
            for kind, path, expected in [
                ("get_encoder_inventory", "/known_encoders/aiops_invalid_encoder", False),
                ("probe_video_input", "/readable", True),
            ]:
                if kind == "probe_video_input" and not cite_input:
                    continue
                record = next(r for r in context["observations"] if r["kind"] == kind)
                hypothesis["assertions"].append(
                    {
                        "evidence_id": record["evidence_id"],
                        "path": path,
                        "operator": "equals",
                        "expected": expected,
                    }
                )
        return RoundOutput.model_validate(output)

    runtime.llm.investigate = investigate
    saver = InMemorySaver()
    paused = execute(runtime.claimed, saver, runtime, interrupt_after=["investigate_hypotheses"])
    before = runtime.adapter.call.call_count
    assert paused.next == ("check_hypotheses",)
    snapshot = execute(runtime.claimed, saver, runtime)
    assert runtime.adapter.call.call_count == before
    calls = [c.args[0].tool_name for c in runtime.adapter.call.call_args_list]
    assert calls[:4] == [
        "get_task_status",
        "get_pod_status",
        "get_encoder_inventory",
        "probe_video_input",
    ]
    assert calls.count("probe_video_input") == 1
    assert len(calls) <= 8
    assert snapshot.values["round"] == 2
    with runtime.factory() as db:
        report = db.get(Diagnosis, runtime.claimed[0])
        assert report.validation_status == ("validated" if cite_input else "insufficient_evidence")
        if not cite_input:
            assert "probe_video_input" in report.payload_json
        schedule = db.scalar(
            select(Timeline).where(Timeline.event_type == "hypothesis_tools_scheduled")
        )
        queries = json.loads(schedule.payload_json)["queries"]
        assert [q["origin"] for q in queries] == ["model_required_request", "required_fixed_check"]
        assert not db.scalar(select(Action))
    runtime.action_client.patch.assert_not_called()


def test_required_scheduler_does_not_invent_dynamic_queries_or_repeat_failed_snapshots(runtime):
    state = {
        "candidate": {
            "hypotheses": [
                {
                    "id": "H1",
                    "cause": "UNKNOWN",
                    "explanation": "尚待查核",
                    "search_query": "failure",
                    "required_tools": ["query_metric", "probe_video_input"],
                }
            ],
            "requests": [],
            "summary": "待查核",
        }
    }
    queue = runtime.investigation_queue(state, [])
    assert [request.tool_name for request, origin in queue] == ["probe_video_input"]
    assert (
        runtime.investigation_queue(
            state,
            [
                {
                    "evidence_id": "failed-probe",
                    "kind": "probe_video_input",
                    "errors": ["unavailable"],
                    "truncated": True,
                }
            ],
        )
        == []
    )
    state["investigation_history"] = [
        {"assessments": [{"hypothesis_id": "H1", "status": "contradicted"}]}
    ]
    assert runtime.investigation_queue(state, []) == []


@pytest.mark.parametrize("round_number,existing,expected_calls", [(2, 7, 1), (3, 2, 0)])
def test_required_scheduler_respects_remaining_budget_and_final_round(
    runtime, round_number, existing, expected_calls
):
    state = {
        "run_id": runtime.claimed[0],
        "incident_id": runtime.claimed[1],
        "namespace": "video-platform",
        "resource_name": "worker-test",
        "round": round_number,
        "evidence_ids": [str(uuid.uuid4()) for _ in range(existing)],
        "candidate": {
            "hypotheses": [
                {
                    "id": "H1",
                    "cause": "UNKNOWN",
                    "explanation": "尚待查核",
                    "search_query": "failure",
                    "required_tools": ["get_encoder_inventory", "probe_video_input"],
                }
            ],
            "requests": [],
            "summary": "待查核",
        },
    }
    result = runtime.investigate(state)
    assert runtime.adapter.call.call_count == expected_calls
    assert result["tools_used"] == existing + expected_calls
