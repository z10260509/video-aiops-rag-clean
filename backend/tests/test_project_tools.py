"""Current deployment and original-video acceptance tools; no live commands or mutations."""

import base64
import json
from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import Mock
import httpx
import pytest
from scripts import deploy_common as common
from scripts import bootstrap_hypothesis_v2 as bootstrap
from scripts import setup_hypothesis_v2 as rollout
from scripts import benchmark_aiops as benchmark
from scripts.run_video_scenario import VideoClient, run
from scripts.project_images import CONTROL, VIDEO


def test_validation_does_not_require_old_lab_and_rejects_foreign_context(monkeypatch):
    calls = []

    def command(*args, **kwargs):
        calls.append(args)
        if args[:3] == ("kubectl", "config", "current-context"):
            return "kind-video-platform"
        if "aiops" in args:
            return json.dumps({"metadata": {"labels": {"aiops.video-platform/managed": "true"}}})
        return "namespace/video-platform"

    monkeypatch.setattr(common, "command", command)
    common.validate()
    assert not any("chaos-lab" in call for call in calls)
    monkeypatch.setattr(common, "command", lambda *a, **kw: "production")
    with pytest.raises(ValueError, match="context"):
        common.validate()


def test_update_retains_worker_replicas_and_cas_identity(monkeypatch):
    calls = []
    obj = {
        "metadata": {"uid": "owned", "resourceVersion": "42"},
        "spec": {
            "replicas": 3,
            "template": {
                "metadata": {"labels": {"app": "worker"}},
                "spec": {
                    "containers": [{"image": "prior", "env": [{"name": "KEEP", "value": "yes"}]}]
                },
            },
        },
    }

    def command(*args, **kwargs):
        calls.append(args)
        return json.dumps(obj) if "get" in args else ""

    monkeypatch.setattr(common, "command", command)
    common.update("video-platform", "worker", VIDEO, [{"name": "NEW", "value": "yes"}], True)
    patch = json.loads(calls[-1][-1])
    assert patch[:2] == [
        {"op": "test", "path": "/metadata/uid", "value": "owned"},
        {"op": "test", "path": "/metadata/resourceVersion", "value": "42"},
    ]
    assert not any(row["path"].startswith("/spec/replicas") for row in patch)
    template = patch[-1]["value"]
    assert template["spec"]["containers"][0]["image"] == VIDEO
    assert template["spec"]["containers"][0]["env"][0]["name"] == "KEEP"


def test_bootstrap_refuses_existing_api_before_any_mutation(monkeypatch):
    monkeypatch.setattr(bootstrap, "deployment_lock", nullcontext)
    monkeypatch.setattr(bootstrap, "validate", lambda: None)
    command = Mock(return_value="deployment/aiops-api")
    monkeypatch.setattr(bootstrap, "command", command)
    mutate = Mock()
    monkeypatch.setattr(bootstrap, "apply", mutate)
    with pytest.raises(ValueError, match="already exists"):
        bootstrap.bootstrap("http://ollama:11434")
    mutate.assert_not_called()
    assert command.call_count == 1


def test_bootstrap_reuses_secret_and_does_not_rotate_private_credentials(monkeypatch):
    expected = {"password": "existing", "DATABASE_URL": "existing-url"}
    monkeypatch.setattr(
        bootstrap,
        "command",
        lambda *a, **kw: json.dumps(
            {"data": {k: base64.b64encode(v.encode()).decode() for k, v in expected.items()}}
        ),
    )
    apply = Mock()
    monkeypatch.setattr(bootstrap, "apply", apply)
    assert (
        bootstrap.ensure_secret(
            "aiops", "aiops-executor", {"password": "new", "DATABASE_URL": "new-url"}
        )
        == expected
    )
    apply.assert_not_called()
    with pytest.raises(ValueError):
        bootstrap.ensure_secret("aiops", "aiops-executor", {"missing": "value"})


def test_current_rollout_guards_active_work_before_mutation(monkeypatch):
    monkeypatch.setattr(rollout, "validate", lambda: None)
    calls = []

    def command(*args, **kwargs):
        calls.append(args)
        if "python" in args:
            raise RuntimeError("Active video jobs")

    monkeypatch.setattr(rollout, "command", command)
    update = Mock()
    monkeypatch.setattr(rollout, "update", update)
    with pytest.raises(RuntimeError):
        rollout.rollout()
    update.assert_not_called()
    assert len(calls) == 1
    assert "QUEUED" in calls[0][-1] and "PROCESSING" in calls[0][-1]


def test_bootstrap_secret_patch_retains_executor_database_identity(tmp_path, monkeypatch):
    monkeypatch.setattr(bootstrap, "deployment_lock", nullcontext)
    monkeypatch.setattr(bootstrap, "validate", lambda: None)
    commands = []
    applied = []
    updates = []

    def command(*args, **kwargs):
        commands.append(args)
        if "get" in args and "secret" in args:
            return json.dumps({"data": {"operator-token": base64.b64encode(b"operator").decode()}})
        return ""

    def ensure(ns, name, values):
        if name == "aiops-executor":
            return {"password": "preserved-password", "DATABASE_URL": "preserved-url"}
        return values

    monkeypatch.setattr(bootstrap, "command", command)
    monkeypatch.setattr(bootstrap, "ensure_secret", ensure)
    monkeypatch.setattr(bootstrap, "schema_job", Mock())
    monkeypatch.setattr(bootstrap, "apply", applied.append)
    monkeypatch.setattr(
        bootstrap,
        "render",
        lambda p: [{"kind": "ConfigMap", "data": {}}] if p.endswith("/api.yaml") else [],
    )
    monkeypatch.setattr(bootstrap, "update", lambda *a: updates.append(a))
    monkeypatch.setattr(bootstrap.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=0))
    original = bootstrap.tempfile.NamedTemporaryFile
    monkeypatch.setattr(
        bootstrap.tempfile, "NamedTemporaryFile", lambda **kw: original(dir=tmp_path, **kw)
    )
    bootstrap.bootstrap("http://ollama:11434")
    secret = next(o for o in applied if o.get("metadata", {}).get("name") == "aiops-executor")
    assert (
        secret["stringData"]["DATABASE_URL"] == "preserved-url"
        and secret["stringData"]["password"] == "preserved-password"
    )
    assert secret["stringData"]["VIDEO_READER_TOKEN"]
    assert {a[2] for a in updates} == {CONTROL, VIDEO}
    assert not any("chaos-lab" in c for c in commands)
    assert (
        bootstrap.schema_job.call_args_list[0].args[2]
        == bootstrap.CONTROL_SCHEMA + " && python -m app.aiops.knowledge"
    )
    env = bootstrap.schema_job.call_args_list[0].args[3]
    assert {"name": "KNOWLEDGE_EMBEDDING_PROVIDER", "value": "ollama"} in env


@pytest.mark.parametrize(
    "mode", ["healthy", "review", "insufficient", "wrong_workflow", "no_action"]
)
def test_original_video_scenario_retains_real_outcome_and_never_approves(
    tmp_path, monkeypatch, mode
):
    source = tmp_path / "source.mp4"
    source.write_bytes(b"fixture")
    output = tmp_path / "result.json"
    calls = []
    report = {
        "run_id": "run",
        "model_id": "ollama:actual-model",
        "validation_status": "insufficient_evidence" if mode == "insufficient" else "validated",
        "diagnosis": {"root_cause_code": "FFMPEG_INVALID_CODEC"},
    }

    def handler(request):
        calls.append(request)
        path = request.url.path
        if request.method == "POST":
            assert path == "/video/api/jobs"
            assert b"demonstrate_failure" in request.content
            return httpx.Response(202, json={"id": "job"})
        if path == "/video/api/jobs/job":
            return httpx.Response(
                200, json={"id": "job", "status": "COMPLETED" if mode == "healthy" else "FAILED"}
            )
        if path.endswith("/video"):
            return httpx.Response(206, content=b"x" * 32)
        if path == "/aiops/api/video-jobs/job":
            return httpx.Response(200, json={"incidents": [{"id": "incident"}]})
        if path.endswith("/report"):
            return httpx.Response(200, json=report)
        if path.endswith("/actions"):
            return httpx.Response(
                200,
                json={
                    "items": []
                    if mode == "no_action"
                    else [
                        {
                            "run_id": "run",
                            "action_type": "retry_video_ffmpeg_h264",
                            "approval_status": "pending",
                            "execution_status": "pending",
                            "target": {"namespace": "video-platform"},
                            "params": {"job_id": "job"},
                            "result": None,
                        }
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "lifecycle": "WAITING_APPROVAL",
                "runs": [
                    {
                        "id": "run",
                        "status": "waiting",
                        "workflow_kind": "aiops-graph"
                        if mode == "wrong_workflow"
                        else "aiops-hypothesis-v2",
                    }
                ],
            },
        )

    api = VideoClient("http://127.0.0.1:13010", "private-token", httpx.MockTransport(handler))
    try:
        if mode in {"healthy", "review"}:
            result = run(source, output, fault=mode != "healthy", api=api)
            assert result["status"] == "passed"
            if mode == "review":
                assert result["model_id"] == report["model_id"]
        else:
            with pytest.raises(RuntimeError):
                run(source, output, fault=True, api=api)
            assert json.loads(output.read_text())["status"] == "failed"
        assert "private-token" not in output.read_text()
        assert not any("/decision" in str(r.url) or "/repair" in str(r.url) for r in calls)
        assert json.loads(output.read_text())["operator_approval_submitted"] is False
    finally:
        api.close()


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com",
        "http://127.0.0.1:13010?token=x",
        "http://user:password@localhost:13010",
    ],
)
def test_video_tool_does_not_send_operator_token_to_other_hosts(url):
    with pytest.raises(ValueError):
        VideoClient(url, "private")


def test_benchmark_preserves_failed_real_report_and_stops_without_reroll(tmp_path, monkeypatch):
    output = tmp_path / "benchmark.json"
    calls = []

    def run(source, path, **kwargs):
        calls.append(path)
        path.write_text(json.dumps({"status": "failed", "model_id": "ollama:actual-model"}))
        raise RuntimeError("Diagnosis rejected")

    monkeypatch.setattr(benchmark, "run", run)
    with pytest.raises(RuntimeError):
        benchmark.benchmark(tmp_path / "source.mp4", 3, output)
    result = json.loads(output.read_text())
    assert result["status"] == "failed" and len(calls) == 1
    assert result["runs"][0]["result"]["model_id"] == "ollama:actual-model"
    with pytest.raises(ValueError):
        benchmark.benchmark(tmp_path / "source.mp4", 3, output)


def test_upgrade_enables_both_policy_gates_and_starts_executor_only_when_requested(monkeypatch):
    monkeypatch.setattr(
        rollout,
        "render_all",
        lambda: [
            {
                "kind": "Deployment",
                "metadata": {"namespace": ns, "name": name},
                "spec": {"template": {"spec": {"containers": [{"image": image, "env": []}]}}},
            }
            for ns, name, image in rollout.TARGETS
        ],
    )
    monkeypatch.setattr(rollout, "apply", lambda obj: None)
    monkeypatch.setattr(rollout, "validate", lambda: None)
    calls = []
    monkeypatch.setattr(rollout, "command", lambda *a, **kw: calls.append(a) or "")
    monkeypatch.setattr(rollout, "update", lambda *a, **kw: None)
    rollout.rollout(enable_remediation=True)
    patches = [json.loads(c[-1]) for c in calls if "configmap" in c and "patch" in c]
    assert (
        patches[0]["data"]["REMEDIATION_ENABLED"]
        == patches[0]["data"]["AIOPS_HYPOTHESIS_REMEDIATION_ENABLED"]
        == "true"
    )
    assert any("--replicas=1" in c for c in calls if "scale" in c)
    calls.clear()
    rollout.rollout(enable_remediation=False)
    assert any("--replicas=0" in c for c in calls if "scale" in c)


def test_old_lab_allowed_for_history_cannot_be_used_for_new_tool_queries():
    from app.aiops.gateway import Gateway, ToolRequest
    from app.core.config import Settings

    tool = Gateway(
        Settings(
            _env_file=None,
            aiops_tools_enabled=True,
            aiops_allowed_namespaces="video-platform,chaos-lab",
        )
    )
    tool.fetch = Mock()
    with pytest.raises(PermissionError):
        tool.call(ToolRequest(tool_name="list_pods", namespace="chaos-lab"))
    tool.fetch.assert_not_called()
