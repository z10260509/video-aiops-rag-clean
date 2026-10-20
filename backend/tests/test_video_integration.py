import hashlib
import json
import uuid
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.api import video_integration as api
from app.aiops.actions import Decision, decide, propose
from app.aiops.executor import execute
from app.aiops.incidents import now
from app.aiops.video_executor import valid
from app.core.config import Settings
from app.database.session import Base, get_db
from app.models.aiops import Action, Diagnosis, Evidence
from app.models.diagnostics import DiagnosisRun, Incident
from app.models.job import Job
from app.models.observability import VideoTaskOutcome
from app.models.video_integration import VideoExecution, VideoRepair

KINDS = {
    "retry_video_ffmpeg_h264": "FFMPEG_INVALID_CODEC",
    "restore_video_redis_and_retry": "REDIS_CONNECTIVITY",
    "increase_video_worker_memory_and_retry": "WORKER_OOM",
}


@pytest.fixture
def endpoint(tmp_path, monkeypatch):
    engine = create_engine(
        "sqlite:///" + str(tmp_path / "video.db"), connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine)
    settings = Settings(
        _env_file=None,
        storage_root=tmp_path,
        aiops_video_enabled=True,
        aiops_video_reader_token="r" * 32,
        aiops_video_repair_token="e" * 32,
    )
    monkeypatch.setattr(api, "get_settings", lambda: settings)
    app = FastAPI()
    app.include_router(api.router)

    def db():
        with factory() as s:
            yield s

    app.dependency_overrides[get_db] = db
    (tmp_path / "input").mkdir()
    (tmp_path / "input" / "mine.mp4").write_bytes(b"my video")
    jid, tid = str(uuid.uuid4()), str(uuid.uuid4())
    with factory() as db:
        db.add(
            Job(
                id=jid,
                original_filename="my-upload.mp4",
                operation="transcode",
                input_path=str(tmp_path / "input" / "mine.mp4"),
                video_codec="h265",
                status="FAILED",
            )
        )
        db.add(
            VideoExecution(
                task_id=tid,
                job_id=jid,
                status="failed",
                pod="worker-abc",
                pod_uid=str(uuid.uuid4()),
                started_at=now() - timedelta(seconds=5),
                finished_at=now(),
                details_json=json.dumps(
                    {"error_kind": "FFMPEG_INVALID_CODEC", "error": "Unknown encoder 'broken'"}
                ),
            )
        )
        db.commit()
    body = {
        "action_id": str(uuid.uuid4()),
        "job_id": jid,
        "failed_task_id": tid,
        "root_cause": "FFMPEG_INVALID_CODEC",
        "video_codec": "h264",
    }
    send = Mock()
    monkeypatch.setattr(api.celery_app, "send_task", send)
    return (
        TestClient(app, raise_server_exceptions=False),
        factory,
        body,
        {"Authorization": "Bearer " + "e" * 32},
        {"Authorization": "Bearer " + "r" * 32},
        send,
        tmp_path,
    )


def test_reader_cannot_mutate_executor_cannot_read_failures(endpoint):
    client, factory, body, executor, reader, send, root = endpoint
    assert client.get("/internal/aiops/failures").status_code == 401
    assert client.get("/internal/aiops/failures", headers=executor).status_code == 401
    assert (
        client.get("/internal/aiops/failures", headers=reader).json()["items"][0]["job_id"]
        == body["job_id"]
    )
    assert client.post("/internal/aiops/repair", json=body, headers=reader).status_code == 401
    send.assert_not_called()


@pytest.mark.parametrize(
    "field,value",
    [
        ("job_id", str(uuid.uuid4())),
        ("failed_task_id", str(uuid.uuid4())),
        ("video_codec", "h265"),
        ("root_cause", "REDIS_CONNECTIVITY"),
    ],
)
def test_wrong_job_task_codec_or_cause_rejected(endpoint, field, value):
    client, factory, body, executor, reader, send, root = endpoint
    assert (
        client.post(
            "/internal/aiops/repair", json={**body, field: value}, headers=executor
        ).status_code
        == 409
    )
    send.assert_not_called()


def test_same_uploaded_job_retry_once_and_fresh_output_verification(endpoint, monkeypatch):
    client, factory, body, executor, reader, send, root = endpoint
    response = client.post("/internal/aiops/repair", json=body, headers=executor)
    assert response.status_code == 200
    task = str(uuid.uuid5(uuid.NAMESPACE_URL, "video-repair:" + body["action_id"]))
    send.assert_called_once_with("worker.process_transcode", args=[body["job_id"]], task_id=task)
    assert client.post("/internal/aiops/repair", json=body, headers=executor).status_code == 409
    assert (
        client.get("/internal/aiops/verify/" + body["action_id"], headers=executor).status_code
        == 409
    )
    (root / "output").mkdir()
    out = root / "output" / "same-job.mp4"
    out.write_bytes(b"repaired video")
    with factory() as db:
        job = db.get(Job, body["job_id"])
        assert job.original_filename == "my-upload.mp4"
        job.status = "COMPLETED"
        job.output_path = str(out)
        at = now()
        db.add(
            VideoExecution(
                task_id=task,
                job_id=job.id,
                status="completed",
                pod="worker-abc",
                pod_uid=str(uuid.uuid4()),
                started_at=at,
                finished_at=at,
                details_json="{}",
            )
        )
        db.add(
            VideoTaskOutcome(
                task_id=task,
                job_id=job.id,
                status="completed",
                started_at=at,
                finished_at=at,
                duration_seconds=0,
            )
        )
        db.commit()
    monkeypatch.setattr(
        api.subprocess,
        "run",
        Mock(return_value=SimpleNamespace(stdout='{"streams":[{"codec_name":"h264"}]}')),
    )
    result = client.get("/internal/aiops/verify/" + body["action_id"], headers=executor).json()
    assert (
        result["job_id"] == body["job_id"]
        and result["output_codec"] == "h264"
        and result["new_outcome_completed"]
    )
    assert (
        client.get("/internal/aiops/verify/" + body["action_id"], headers=reader).status_code == 200
    )
    assert client.get("/internal/aiops/verify/" + body["action_id"]).status_code == 401
    assert body["job_id"] in result["play_url"]
    assert send.call_count == 1


def test_missing_input_and_changed_latest_attempt_reject(endpoint):
    client, factory, body, executor, reader, send, root = endpoint
    (root / "input" / "mine.mp4").unlink()
    assert client.post("/internal/aiops/repair", json=body, headers=executor).status_code == 409
    (root / "input" / "mine.mp4").write_bytes(b"input")
    with factory() as db:
        db.add(
            VideoExecution(
                task_id=str(uuid.uuid4()),
                job_id=body["job_id"],
                status="failed",
                started_at=now(),
                finished_at=now(),
                details_json="{}",
            )
        )
        db.commit()
    assert client.post("/internal/aiops/repair", json=body, headers=executor).status_code == 409
    send.assert_not_called()


def test_unknown_publish_keeps_reservation_and_never_republishes(endpoint):
    client, factory, body, executor, reader, send, root = endpoint
    send.side_effect = TimeoutError("Publish acknowledgement lost")
    assert client.post("/internal/aiops/repair", json=body, headers=executor).status_code == 500
    assert client.post("/internal/aiops/repair", json=body, headers=executor).status_code == 409
    assert send.call_count == 1
    with factory() as db:
        assert db.get(VideoRepair, body["action_id"])


def test_three_video_actions_require_human_approval_and_execute_once(platform):
    factory, settings, client, aid, kind, params = platform
    with factory() as db:
        action = db.get(Action, aid)
        action.execution_status = "queued"
        db.commit()
    assert execute(aid, client, factory, settings)["attempted"] is False
    client.patch.assert_not_called()
    with factory() as db:
        action = db.get(Action, aid)
        action.execution_status = "pending"
        decide(
            db,
            action,
            Decision(
                decision="approved",
                reason="Reviewed original uploaded video",
                binding_hash=action.binding_hash,
            ),
            settings.aiops_operator_principal,
            client,
            settings,
        )
        action.execution_status = "queued"
        db.commit()
    assert execute(aid, client, factory, settings)["status"] == "succeeded"
    assert execute(aid, client, factory, settings) == {"attempted": False}
    client.patch.assert_called_once()


@pytest.mark.parametrize(
    "cause",
    [
        "wrong_job",
        "changed_configuration",
        "disabled",
        "expired",
        "deployment_replaced",
        "reader_unavailable",
    ],
)
def test_video_scope_changes_deny_before_mutation(platform, cause):
    factory, settings, client, aid, kind, params = platform
    with factory() as db:
        action = db.get(Action, aid)
        decide(
            db,
            action,
            Decision(
                decision="approved",
                reason="Reviewed uploaded video scope",
                binding_hash=action.binding_hash,
            ),
            settings.aiops_operator_principal,
            client,
            settings,
        )
        action.execution_status = "queued"
        if cause == "wrong_job":
            action.params_json = json.dumps({**params, "job_id": str(uuid.uuid4())})
        if cause == "changed_configuration":
            client.deployment.return_value["spec"]["template"] = {"changed": True}
        if cause == "disabled":
            settings.aiops_video_enabled = False
        if cause == "expired":
            action.expires_at = now() - timedelta(seconds=1)
        if cause == "deployment_replaced":
            client.deployment.return_value["metadata"]["uid"] = str(uuid.uuid4())
        if cause == "reader_unavailable":
            client.preflight.side_effect = ValueError("Original video evidence unavailable")
        db.commit()
    assert execute(aid, client, factory, settings)["attempted"] is False
    client.patch.assert_not_called()


def test_recovery_is_bound_to_original_video_and_actual_memory(platform):
    factory, settings, client, aid, kind, params = platform
    proof = client.verify.return_value
    assert valid(kind, params, aid, proof)
    proof["fresh_sample"]["job_id"] = str(uuid.uuid4())
    assert not valid(kind, params, aid, proof)
    proof["fresh_sample"]["job_id"] = params["job_id"]
    if kind == "increase_video_worker_memory_and_retry":
        proof["fresh_sample"]["details"]["memory_after"]["oom_kills"] = 1
        assert not valid(kind, params, aid, proof)


def test_bridge_registers_original_job_once_including_closed_incident(platform):
    from app.aiops.video_bridge import register, linked_job

    factory, settings, client, aid, kind, params = platform
    with factory() as db:
        action = db.get(Action, aid)
        incident = db.get(Incident, action.incident_id)
        incident.status = "closed"
        db.commit()
        row = register(
            db,
            {"job_id": params["job_id"], "task_id": params["failed_task_id"], "pod": "worker-abc"},
            None,
            settings,
        )
        assert row.id == incident.id and row.status == "closed"
        assert linked_job(row) == {"job_id": params["job_id"], "task_id": params["failed_task_id"]}


def test_demo_upload_requires_operator_authorization_before_creating_file(endpoint):
    from app.api.jobs import create_job
    from app.models.job import Operation, VideoCodec, CompressionPreset
    from fastapi import HTTPException, UploadFile
    from io import BytesIO

    client, factory, body, executor, reader, send, root = endpoint
    settings = Settings(
        _env_file=None,
        aiops_video_enabled=True,
        aiops_video_demo_enabled=True,
        storage_root=root,
        aiops_operator_token="o" * 32,
    )
    with factory() as db, pytest.raises(HTTPException) as caught:
        create_job(
            UploadFile(file=BytesIO(b"input"), filename="my.mp4"),
            Operation.TRANSCODE,
            "480p",
            db,
            settings,
            VideoCodec.H264,
            CompressionPreset.BALANCED,
            True,
            None,
        )
    assert caught.value.status_code == 403
    send.assert_not_called()


def test_oom_requires_real_termination_after_attempt_started():
    from app.aiops.video_bridge import oom_termination_matches

    start = "2026-10-11T06:00:00.500000Z"
    assert not oom_termination_matches(
        {"reason": "Error", "exitCode": 137, "finishedAt": "2026-10-11T06:00:02Z"}, start
    )
    assert not oom_termination_matches(
        {"reason": "OOMKilled", "finishedAt": "2026-10-11T06:00:00Z"}, start
    )
    assert oom_termination_matches(
        {"reason": "OOMKilled", "finishedAt": "2026-10-11T06:00:02Z"}, start
    )
    assert not oom_termination_matches({"reason": "OOMKilled", "finishedAt": "invalid"}, start)


def test_integrated_readiness_keeps_evidence_available_when_broker_down(monkeypatch):
    from app import main

    settings = Settings(_env_file=None, aiops_video_enabled=True)
    monkeypatch.setattr(main, "get_settings", lambda: settings)
    session = Mock()
    session.__enter__ = Mock(return_value=session)
    session.__exit__ = Mock(return_value=False)
    monkeypatch.setattr(main, "SessionLocal", lambda: session)
    broker = Mock()
    broker.connection_for_read.side_effect = RuntimeError("Redis unavailable")
    monkeypatch.setattr(main, "celery_app", broker)
    assert main.ready() == {"status": "ready"}
    broker.connection_for_read.assert_not_called()


def test_all_video_recovery_requires_all_desired_replicas_ready(platform):
    factory, settings, client, aid, kind, params = platform
    proof = client.verify.return_value
    proof.update(available_replicas=3, desired_replicas=3)
    assert valid(kind, params, aid, proof)
    proof["available_replicas"] = 2
    assert not valid(kind, params, aid, proof)


@pytest.mark.parametrize(
    "outcome", ["pass", "wrong_video", "unavailable", "not_approved", "active_run"]
)
def test_recheck_reads_existing_proof_without_any_repair(platform, monkeypatch, outcome):
    from app.aiops.main import app
    from app.aiops.executor import KubernetesActionClient
    from app.models.aiops import Approval, Timeline
    from app.core.config import get_settings

    factory, settings, worker, aid, kind, params = platform
    from pydantic import SecretStr

    settings.aiops_enabled = True
    settings.aiops_operator_token = SecretStr("o" * 40)
    settings.aiops_webhook_token = SecretStr("w" * 40)
    settings.aiops_allowed_namespaces = "video-platform"
    with factory() as db:
        action = db.get(Action, aid)
        iid = action.incident_id
        decide(
            db,
            action,
            Decision(
                decision="approved",
                reason="Actual operator approved original video",
                binding_hash=action.binding_hash,
            ),
            settings.aiops_operator_principal,
            worker,
            settings,
        )
        action.execution_status = "uncertain"
        action.result_json = json.dumps({"error_type": "ValueError"})
        incident = db.get(Incident, iid)
        incident.status = "closed"
        incident.lifecycle = "ESCALATED"
        run = db.get(DiagnosisRun, action.run_id)
        run.status = "running" if outcome == "active_run" else "completed"
        if outcome == "not_approved":
            db.get(Approval, aid).decision = "rejected"
        db.commit()
    if outcome == "wrong_video":
        worker.verify.return_value["fresh_sample"]["job_id"] = str(uuid.uuid4())
    if outcome == "unavailable":
        worker.verify.side_effect = ValueError("Output not ready")
    monkeypatch.setattr("app.aiops.executor.KubernetesActionClient", lambda **kwargs: worker)

    def database():
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = database
    app.dependency_overrides[get_settings] = lambda: settings
    try:
        with TestClient(app) as client:
            path = "/api/incidents/" + iid + "/actions/" + aid + "/verify"
            assert client.post(path, json={}).status_code == 401
            response = client.post(path, json={}, headers={"Authorization": "Bearer " + "o" * 40})
            if outcome in ["not_approved", "active_run"]:
                assert response.status_code == 409
                worker.verify.assert_not_called()
            else:
                assert response.status_code == 200
                assert response.json()["verified"] == (outcome == "pass")
                worker.verify.assert_called_once_with(read_only=True)
            if outcome == "pass":
                assert (
                    client.post(
                        path, json={}, headers={"Authorization": "Bearer " + "o" * 40}
                    ).status_code
                    == 409
                )
        with factory() as db:
            action = db.get(Action, aid)
            report = db.get(Diagnosis, action.run_id)
            incident = db.get(Incident, iid)
            assert action.execution_status == ("succeeded" if outcome == "pass" else "uncertain")
            if outcome == "pass":
                assert json.loads(report.payload_json)["recovery_verified"] is True
                assert incident.lifecycle == "RESOLVED"
                assert json.loads(action.result_json)["prior_result"]["error_type"] == "ValueError"
                assert db.scalar(
                    select(Timeline).where(Timeline.event_type == "recovery_rechecked")
                )
    finally:
        app.dependency_overrides.clear()
    worker.patch.assert_not_called()


def test_private_investigation_probes_bound_to_original_attempt(endpoint, monkeypatch):
    client, factory, body, executor, reader, send, root = endpoint
    url = "/internal/aiops/jobs/" + body["job_id"] + "/probe/"
    query = "?task_id=" + body["failed_task_id"]
    assert client.get(url + "input" + query).status_code == 401
    assert client.get(url + "input" + query, headers=executor).status_code == 401
    assert client.get(url + "input?task_id=" + str(uuid.uuid4()), headers=reader).status_code == 404
    assert client.get(url + "shell" + query, headers=reader).status_code == 422
    probe = Mock(
        return_value=SimpleNamespace(
            returncode=0,
            stdout='{"streams":[{"codec_name":"h264"}],"format":{"duration":"1.0"}}',
            stderr="",
        )
    )
    monkeypatch.setattr(api.subprocess, "run", probe)
    result = client.get(url + "input" + query, headers=reader).json()
    assert result["readable"] is True and result["job_id"] == body["job_id"]
    assert probe.call_args.args[0][-1] == str(root / "input" / "mine.mp4")
    assert "-protocol_whitelist" in probe.call_args.args[0]
    assert client.get(url + "encoders" + query, headers=reader).status_code == 409
    with factory() as db:
        row = db.get(VideoExecution, body["failed_task_id"])
        row.details_json = json.dumps(
            {
                "encoder_inventory": {
                    "available": True,
                    "encoders": ["libx264"],
                    "observed_at": "2026-10-16T00:00:00Z",
                }
            }
        )
        db.commit()
    result = client.get(url + "encoders" + query, headers=reader).json()
    assert (
        result["scope"] == "worker_execution_snapshot"
        and result["known_encoders"] == {"libx264": True, "libx265": False}
        and result["encoder_count"] == 1
    )
    send.assert_not_called()


def test_real_local_ffprobe_and_worker_encoder_snapshot(endpoint):
    import shutil
    import subprocess
    from app.services.video_execution import encoder_observation

    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("Requires FFmpeg runtime image")
    client, factory, body, executor, reader, send, root = endpoint
    source = root / "input" / "mine.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=160x90:d=0.2",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(source),
        ],
        check=True,
    )
    url = (
        "/internal/aiops/jobs/" + body["job_id"] + "/probe/input?task_id=" + body["failed_task_id"]
    )
    response = client.get(url, headers=reader)
    assert response.status_code == 200 and response.json()["readable"] is True
    assert response.json()["streams"][0]["codec_name"] == "h264"
    source.write_bytes(b"broken MP4")
    response = client.get(url, headers=reader)
    assert response.status_code == 200 and response.json()["readable"] is False
    inventory = encoder_observation()
    assert inventory["available"] is True and "libx264" in inventory["encoders"]
    send.assert_not_called()


def test_scaled_pool_proposal_approval_and_execution_survive_hpa_and_pod_replacement(platform):
    from copy import deepcopy

    factory, settings, client, aid, kind, params = platform
    obj = client.deployment.return_value
    obj["spec"]["replicas"] = 2
    old = deepcopy(client.validate_pod.return_value)
    pods = [
        {**deepcopy(old), "metadata": {"name": "worker-one", "uid": "pod-one"}},
        {**deepcopy(old), "metadata": {"name": "worker-two", "uid": "pod-two"}},
    ]
    client.request.side_effect = lambda method, path: (
        {"items": pods}
        if "/pods?" in path
        else {
            "metadata": {
                "uid": "backend-uid",
                "resourceVersion": "42",
                "labels": {"aiops.video-platform/repair-enabled": "true"},
            }
        }
    )
    client.validate_pod.side_effect = lambda name, uid: next(
        p for p in pods if p["metadata"]["name"] == name
    )
    with factory() as db:
        old_action = db.get(Action, aid)
        run = DiagnosisRun(
            incident_id=old_action.incident_id,
            status="waiting",
            workflow_kind="aiops-hypothesis-v2",
        )
        db.add(run)
        db.flush()
        report = db.get(Diagnosis, old_action.run_id)
        payload = json.loads(report.payload_json)
        evidence = db.get(Evidence, payload["evidence_ids"][0])
        eid = str(uuid.uuid4())
        db.add(
            Evidence(
                id=eid,
                incident_id=evidence.incident_id,
                run_id=run.id,
                kind=evidence.kind,
                source=evidence.source,
                requested_at=now(),
                observed_at=now(),
                query_json=evidence.query_json,
                payload_json=evidence.payload_json,
                checksum=evidence.checksum,
                errors_json="[]",
                truncated=False,
            )
        )
        payload["evidence_ids"] = [eid]
        db.add(
            Diagnosis(
                run_id=run.id,
                incident_id=run.incident_id,
                thread_id="scaled-test",
                model_id="fake",
                prompt_version="v1",
                validation_status="validated",
                payload_json=json.dumps(payload),
                created_at=now(),
            )
        )
        db.flush()
        from conftest import install_v2_report

        incident = db.get(Incident, run.incident_id)
        install_v2_report(
            db,
            incident,
            run,
            kind,
            params["job_id"],
            params["failed_task_id"],
            pods[0]["metadata"]["uid"],
        )
        action = propose(db, incident, run, client)
        aid = action.id
        assert json.loads(action.target_json)["replicas_at_proposal"] == 2
        # A status update and HPA scale-in replace the illustrative Pod, while
        # Deployment UID/configuration and exact original job remain unchanged.
        obj["metadata"]["resourceVersion"] = "99"
        obj["spec"]["replicas"] = 1
        pods[:] = [pods[1]]
        decide(
            db,
            action,
            Decision(
                decision="approved",
                reason="Reviewed multi replica original job repair",
                binding_hash=action.binding_hash,
            ),
            settings.aiops_operator_principal,
            client,
            settings,
        )
        obj["spec"]["replicas"] = 3
        obj["metadata"]["resourceVersion"] = "100"
        action.execution_status = "queued"
        db.commit()
    proof = client.verify.return_value
    proof.update(available_replicas=3, desired_replicas=3)
    proof["fresh_sample"].update(
        action_id=aid, task_id=str(uuid.uuid5(uuid.NAMESPACE_URL, "video-repair:" + aid))
    )
    assert execute(aid, client, factory, settings)["status"] == "succeeded"
    client.patch.assert_called_once()


@pytest.mark.parametrize("failure", ["zero", "not_ready", "terminating", "foreign_owner"])
def test_multi_replica_pool_blocks_unavailable_or_unowned_workers(platform, failure):
    from app.aiops.actions import policy

    factory, settings, client, aid, kind, params = platform
    client.deployment.return_value["spec"]["replicas"] = 3
    if failure == "zero":
        client.deployment.return_value["spec"]["replicas"] = 0
    if failure == "not_ready":
        client.validate_pod.return_value["status"]["conditions"][0]["status"] = "False"
    if failure == "terminating":
        client.validate_pod.return_value["metadata"]["deletionTimestamp"] = "2026-10-17T00:00:00Z"
    if failure == "foreign_owner":
        client.validate_pod.side_effect = PermissionError("Deployment owner mismatch")
    with factory() as db, pytest.raises(PermissionError):
        policy(db, db.get(Action, aid), client, settings)
    client.patch.assert_not_called()


def test_old_single_pod_approval_contract_is_not_silently_relaxed(platform):
    from app.aiops.actions import policy, binding

    factory, settings, client, aid, kind, params = platform
    with factory() as db:
        action = db.get(Action, aid)
        target = json.loads(action.target_json)
        for key in ["worker_binding_version", "deployment_spec_hash", "replicas_at_proposal"]:
            target.pop(key)
        action.target_json = json.dumps(target)
        action.binding_hash = binding(action)
        policy(db, action, client, settings)
        client.deployment.return_value["spec"]["replicas"] = 2
        with pytest.raises(PermissionError):
            policy(db, action, client, settings)
