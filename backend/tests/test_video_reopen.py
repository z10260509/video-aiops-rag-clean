import json
import uuid
from datetime import timedelta
from unittest.mock import Mock
from types import SimpleNamespace
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from app.aiops.main import app
from app.aiops.incidents import now, transition
from app.core.config import Settings, get_settings
from app.database.session import Base, get_db
from app.models.aiops import Action, Approval, Timeline
from app.models.diagnostics import Incident, DiagnosisRun


@pytest.fixture
def reopen_api(tmp_path, monkeypatch):
    engine = create_engine(
        "sqlite:///" + str(tmp_path / "reopen.db"), connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine)
    settings = Settings(
        _env_file=None,
        aiops_enabled=True,
        aiops_operator_token="o" * 40,
        aiops_webhook_token="w" * 40,
        aiops_allowed_namespaces="video-platform",
        aiops_video_enabled=True,
        aiops_graph_enabled=True,
    )
    jid, tid = str(uuid.uuid4()), str(uuid.uuid4())
    with factory() as db:
        i = Incident(
            correlation_key=f"video-job:{jid}:{tid}",
            namespace="video-platform",
            resource_id="worker-test",
            service="transcoder",
            symptom="VideoJobFailure",
            lifecycle="ESCALATED",
            status="closed",
            first_seen_at=now(),
            last_seen_at=now(),
        )
        db.add(i)
        db.flush()
        r = DiagnosisRun(incident_id=i.id, status="completed", workflow_kind="aiops-hypothesis-v2")
        db.add(r)
        db.flush()
        a = Action(
            run_id=r.id,
            incident_id=i.id,
            action_type="retry_video_ffmpeg_h264",
            target_json="{}",
            params_json="{}",
            binding_hash="a" * 64,
            policy_version="video-codec-v1",
            idempotency_key=str(uuid.uuid4()),
            created_at=now(),
            expires_at=now() + timedelta(hours=1),
            approval_status="draft",
            execution_status="pending",
        )
        db.add(a)
        db.flush()
        iid, aid, rid = i.id, a.id, r.id
        db.commit()
    record = {
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
    source = Mock(return_value=record)
    monkeypatch.setattr("app.aiops.video_bridge.source", source)

    def database():
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = database
    app.dependency_overrides[get_settings] = lambda: settings
    with TestClient(app) as client:
        yield client, factory, iid, aid, rid, source, record
    app.dependency_overrides.clear()
    engine.dispose()


def post(case, key="reopen-1", auth=True):
    client, _, iid, *_ = case
    headers = {"Idempotency-Key": key}
    if auth:
        headers["Authorization"] = "Bearer " + "o" * 40
    return client.post("/api/incidents/" + iid + "/analyze", headers=headers)


def test_reopen_rechecks_original_and_supersedes_draft_without_approval(reopen_api):
    client, factory, iid, aid, rid, source, record = reopen_api
    r = post(reopen_api)
    assert r.status_code == 202 and r.json()["reopened"]
    assert r.json()["run_id"] != rid
    assert post(reopen_api).json()["run_id"] == r.json()["run_id"]
    assert post(reopen_api, "different-click").json()["run_id"] == r.json()["run_id"]
    source.assert_called_once()
    with factory() as db:
        i = db.get(Incident, iid)
        assert i.status == "open" and i.lifecycle == "INVESTIGATING" and i.version == 2
        old = db.get(Action, aid)
        assert old.approval_status == "superseded" and old.binding_hash == "a" * 64
        assert not list(db.scalars(select(Approval)))
        assert (
            len(list(db.scalars(select(Action)))) == 1
        )  # No repair proposal before new diagnosis.
        assert db.get(DiagnosisRun, r.json()["run_id"]).workflow_kind == "aiops-hypothesis-v2"
        events = list(
            db.scalars(select(Timeline).where(Timeline.event_type == "incident_reopened"))
        )
        assert len(events) == 1 and json.loads(events[0].payload_json)["approval_reused"] is False


@pytest.mark.parametrize(
    "change",
    [
        {"found": False},
        {"input_available": False},
        {"latest_task_id": str(uuid.uuid4())},
        {"task_id": str(uuid.uuid4())},
        {"job_id": str(uuid.uuid4())},
        {"retry_reserved": True},
        {"job_status": "COMPLETED", "execution_status": "completed"},
        {"job_status": "PROCESSING", "execution_status": "running"},
    ],
)
def test_changed_or_recovered_task_cannot_reopen(reopen_api, change, monkeypatch):
    _, factory, iid, aid, _, _, record = reopen_api
    record.update(change)
    monkeypatch.setattr(
        "app.aiops.gateway.gateway",
        Mock(return_value=Mock(call=Mock(side_effect=ValueError("unavailable")))),
    )
    assert post(reopen_api).status_code == 409
    with factory() as db:
        assert db.get(Incident, iid).status == "closed"
        assert db.get(Action, aid).approval_status == "draft"
        assert len(list(db.scalars(select(DiagnosisRun)))) == 1
        assert not list(
            db.scalars(select(Timeline).where(Timeline.event_type == "incident_reopened"))
        )


@pytest.mark.parametrize("status", ["queued", "running", "uncertain"])
def test_prior_mutation_blocks_reopen(reopen_api, status):
    _, factory, _, aid, _, source, _ = reopen_api
    with factory() as db:
        db.get(Action, aid).execution_status = status
        db.commit()
    assert post(reopen_api).status_code == 409
    source.assert_not_called()


def test_source_failure_and_unauthenticated_request_do_not_reopen(reopen_api):
    *_, source, _ = reopen_api
    assert post(reopen_api, auth=False).status_code == 401
    source.assert_not_called()
    source.side_effect = ValueError("private detail")
    response = post(reopen_api)
    assert response.status_code == 409 and "private detail" not in response.text


def test_generic_transition_cannot_bypass_reopen_checks(reopen_api):
    _, factory, iid, *_ = reopen_api
    with factory() as db:
        with pytest.raises(ValueError):
            transition(db, db.get(Incident, iid), "INVESTIGATING", "operator", "bypass")


def test_completed_event_stays_closed_during_readonly_reanalysis(reopen_api):
    _, factory, iid, _, _, source, _ = reopen_api
    with factory() as db:
        db.get(Incident, iid).lifecycle = "RESOLVED"
        db.commit()
    assert post(reopen_api).status_code == 202
    source.assert_not_called()
    with factory() as db:
        assert db.get(Incident, iid).status == "closed"


def test_deleted_event_is_not_resurrected(reopen_api):
    _, factory, iid, _, _, source, _ = reopen_api
    with factory() as db:
        db.add(
            Timeline(
                incident_id=iid,
                event_key="deleted:" + iid,
                event_type="incident_deleted",
                actor="operator",
                payload_json="{}",
                created_at=now(),
            )
        )
        db.commit()
    assert post(reopen_api).status_code == 404
    source.assert_not_called()


@pytest.mark.parametrize("matching", [True, False])
def test_interrupted_oom_requires_original_pod_and_termination(reopen_api, monkeypatch, matching):
    *_, record = reopen_api
    record.update(
        job_status="PROCESSING",
        execution_status="running",
        pod="worker-test",
        pod_uid="original",
        started_at=(now() - timedelta(seconds=10)).isoformat() + "Z",
    )
    result = SimpleNamespace(
        errors=[],
        truncated=False,
        data={
            "uid": "original" if matching else "replacement",
            "containers": [
                {
                    "name": "worker",
                    "last_state": {
                        "terminated": {"reason": "OOMKilled", "finishedAt": now().isoformat() + "Z"}
                    },
                }
            ],
        },
    )
    monkeypatch.setattr(
        "app.aiops.gateway.gateway", Mock(return_value=Mock(call=Mock(return_value=result)))
    )
    assert post(reopen_api).status_code == (202 if matching else 409)


def test_approved_unexecuted_action_is_not_silently_replaced(reopen_api):
    _, factory, _, aid, _, source, _ = reopen_api
    with factory() as db:
        db.get(Action, aid).approval_status = "approved"
        db.commit()
    assert post(reopen_api).status_code == 409
    source.assert_not_called()
