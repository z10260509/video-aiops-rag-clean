from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.aiops.main import app
from app.aiops.incidents import receive, transition
from app.aiops.schemas import Webhook
from app.core.config import Settings, get_settings
from app.database.session import Base, get_db
from app.models.aiops import Alert, Timeline
from app.models.diagnostics import DiagnosisRun, Incident


@pytest.fixture
def api(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'aiops.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    settings = Settings(
        _env_file=None,
        aiops_enabled=True,
        aiops_operator_token="o" * 40,
        aiops_webhook_token="w" * 40,
    )

    def database():
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = database
    app.dependency_overrides[get_settings] = lambda: settings
    with TestClient(app) as client:
        yield client, factory, settings
    app.dependency_overrides.clear()
    engine.dispose()


def payload(status="firing", start=None, fingerprint="0" * 16, **labels):
    start = start or datetime(2026, 10, 10, tzinfo=timezone.utc)
    return {
        "version": "4",
        "status": status,
        "alerts": [
            {
                "status": status,
                "fingerprint": fingerprint,
                "startsAt": start.isoformat(),
                "endsAt": (start + timedelta(seconds=60)).isoformat(),
                "labels": {
                    "namespace": "video-platform",
                    "pod": "worker-test",
                    "alertname": "VideoLabFFmpegFailure",
                    **labels,
                },
                "annotations": {"summary": "password=hide user@example.com", "token": "hide-too"},
            }
        ],
    }


def send(client, body):
    return client.post(
        "/api/alerts/webhook", json=body, headers={"Authorization": "Bearer " + "w" * 40}
    )


def test_firing_dedupes_incident_dispatch_and_timeline(api):
    client, factory, _ = api
    first = send(client, payload())
    second = send(client, payload())
    assert first.status_code == second.status_code == 202
    assert second.json()["accepted"][0]["duplicate"]
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Incident)) == 1
        assert db.scalar(select(func.count()).select_from(Alert)) == 1
        assert db.scalar(select(func.count()).select_from(DiagnosisRun)) == 1
        assert db.scalar(select(func.count()).select_from(Timeline)) == 3
        alert = db.scalar(select(Alert))
        assert (
            "hide" not in alert.annotations_json
            and "user@example.com" not in alert.annotations_json
        )


def test_resolved_duplicates_and_late_firing_cannot_reopen(api):
    client, factory, _ = api
    send(client, payload())
    with factory() as db:
        db.scalar(select(DiagnosisRun)).status = "completed"
        db.commit()
    send(client, payload("resolved"))
    send(client, payload("resolved"))
    send(client, payload())
    with factory() as db:
        row = db.scalar(select(Incident))
        assert row.lifecycle == "RESOLVED" and row.status == "closed"
        assert db.scalar(select(DiagnosisRun)).status == "completed"
        assert db.scalar(select(func.count()).select_from(Incident)) == 1


def test_resolved_before_firing_is_tombstone_but_new_episode_queues(api):
    client, factory, _ = api
    send(client, payload("resolved"))
    send(client, payload())
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(DiagnosisRun)) == 0
    send(client, payload(start=datetime(2026, 10, 10, 0, 2, tzinfo=timezone.utc)))
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Incident)) == 2
        assert db.scalar(select(func.count()).select_from(DiagnosisRun)) == 1


def test_old_resolved_does_not_close_new_episode(api):
    client, factory, _ = api
    send(client, payload())
    send(client, payload("resolved"))
    send(client, payload(start=datetime(2026, 10, 10, 0, 2, tzinfo=timezone.utc)))
    send(client, payload("resolved"))
    with factory() as db:
        assert (
            db.scalar(select(func.count()).select_from(Incident).where(Incident.status == "open"))
            == 1
        )


def test_group_remains_open_until_all_fingerprints_resolve(api):
    client, factory, _ = api
    send(client, payload())
    send(client, payload(fingerprint="1" * 16))
    with factory() as db:
        db.scalar(select(DiagnosisRun)).status = "completed"
        db.commit()
    send(client, payload("resolved"))
    with factory() as db:
        assert db.scalar(select(Incident)).status == "open"
    send(client, payload("resolved", fingerprint="1" * 16))
    with factory() as db:
        assert db.scalar(select(Incident)).status == "closed"


def test_auth_bounds_namespace_and_malformed_payload(api):
    client, _, _ = api
    assert client.post("/api/alerts/webhook", json=payload()).status_code == 401
    assert (
        client.post(
            "/api/alerts/webhook", json=payload(), headers={"Authorization": "Bearer " + "o" * 40}
        ).status_code
        == 401
    )
    assert client.get("/api/incidents").status_code == 401
    assert send(client, payload(namespace="kube-system")).status_code == 403
    assert send(client, payload(namespace="../../")).status_code == 422
    bad = payload()
    bad["alerts"][0]["status"] = "invalid"
    assert send(client, bad).status_code == 422
    assert client.post("/api/alerts/webhook", content=b"x" * 131073).status_code == 413


def test_authenticated_list_detail_timeline_and_idempotent_schedule(api):
    client, factory, _ = api
    incident_id = send(client, payload()).json()["accepted"][0]["incident_id"]
    auth = {"Authorization": "Bearer " + "o" * 40, "Idempotency-Key": "request-1"}
    assert len(client.get("/api/incidents", headers=auth).json()["items"]) == 1
    assert (
        client.get("/api/incidents/" + incident_id, headers=auth).json()["namespace"]
        == "video-platform"
    )
    assert (
        len(client.get("/api/incidents/" + incident_id + "/timeline", headers=auth).json()["items"])
        == 3
    )
    first = client.post("/api/incidents/" + incident_id + "/analyze", headers=auth)
    second = client.post("/api/incidents/" + incident_id + "/analyze", headers=auth)
    assert first.status_code == 202 and first.json()["run_id"] == second.json()["run_id"]
    assert client.get("/api/incidents?namespace=kube-system", headers=auth).status_code == 403


def test_state_machine_rejects_skips_and_stale_version(api):
    client, factory, _ = api
    incident_id = send(client, payload()).json()["accepted"][0]["incident_id"]
    with factory() as db:
        row = db.get(Incident, incident_id)
        with pytest.raises(ValueError):
            transition(db, row, "REMEDIATING", "operator", "unsafe")
        with pytest.raises(ValueError):
            transition(db, row, "INVESTIGATING", "operator", "inspect", 99)
        transition(db, row, "INVESTIGATING", "operator", "inspect", 1)
        db.commit()
        assert row.version == 2


def test_each_coalesced_request_key_survives_completed_run(api):
    client, factory, _ = api
    incident_id = send(client, payload()).json()["accepted"][0]["incident_id"]
    auth = {"Authorization": "Bearer " + "o" * 40}
    runs = []
    for key in ["one", "two"]:
        runs.append(
            client.post(
                "/api/incidents/" + incident_id + "/analyze",
                headers={**auth, "Idempotency-Key": key},
            ).json()["run_id"]
        )
    assert runs[0] == runs[1]
    with factory() as db:
        db.get(DiagnosisRun, runs[0]).status = "completed"
        db.commit()
    for key in ["one", "two"]:
        assert (
            client.post(
                "/api/incidents/" + incident_id + "/analyze",
                headers={**auth, "Idempotency-Key": key},
            ).json()["run_id"]
            == runs[0]
        )


def test_resolved_delivery_after_manual_cancel_preserves_terminal_state(api):
    client, factory, _ = api
    incident_id = send(client, payload()).json()["accepted"][0]["incident_id"]
    with factory() as db:
        transition(db, db.get(Incident, incident_id), "CANCELLED", "operator", "cancel", 1)
        db.commit()
    assert send(client, payload("resolved")).status_code == 202
    with factory() as db:
        assert db.get(Incident, incident_id).lifecycle == "CANCELLED"
        assert db.scalar(select(Alert)).status == "resolved"


@pytest.mark.parametrize("phase", ["pending", "running", "waiting"])
def test_cleared_alert_does_not_close_active_graph_or_skip_human_repair(api, phase):
    client, factory, settings = api
    incident_id = send(client, payload()).json()["accepted"][0]["incident_id"]
    with factory() as db:
        run = db.scalar(select(DiagnosisRun))
        run.workflow_kind = "aiops-hypothesis-v2"
        run.status = phase
        row = db.get(Incident, incident_id)
        row.lifecycle = "WAITING_APPROVAL" if phase == "waiting" else "INVESTIGATING"
        db.commit()
    send(client, payload("resolved"))
    with factory() as db:
        row = db.get(Incident, incident_id)
        assert row.status == "open" and row.lifecycle != "RESOLVED"
        assert db.scalar(select(DiagnosisRun)).status == phase
        assert db.scalar(
            select(Timeline.id).where(Timeline.event_type == "alert_cleared_workflow_active")
        )


def test_delete_history_requires_operator_and_retains_tombstone(api):
    client, factory, _ = api
    response = send(client, payload("resolved"))
    iid = response.json()["accepted"][0]["incident_id"]
    path = "/api/incidents/" + iid
    headers = {"Authorization": "Bearer " + "o" * 40}
    assert client.delete(path).status_code == 401
    assert client.delete(path, headers={"Authorization": "Bearer " + "w" * 40}).status_code == 401
    assert client.delete(path, headers=headers).json()["audit_retained"] is True
    assert client.get("/api/incidents", headers=headers).json()["items"] == []
    assert client.get(path, headers=headers).status_code == 404
    assert (
        client.post(
            path + "/analyze", headers={**headers, "Idempotency-Key": "deleted"}
        ).status_code
        == 404
    )
    assert client.delete(path, headers=headers).status_code == 404
    send(client, payload())
    assert client.get("/api/incidents", headers=headers).json()["items"] == []
    with factory() as db:
        assert db.get(Incident, iid)
        assert db.scalar(select(Alert).where(Alert.incident_id == iid))
        assert (
            db.scalar(
                select(Timeline).where(
                    Timeline.incident_id == iid, Timeline.event_type == "incident_deleted"
                )
            ).actor
            == "local-operator"
        )


@pytest.mark.parametrize("blocked", ["open", "running", "waiting", "queued_action"])
def test_cannot_delete_active_history(api, blocked):
    from app.models.aiops import Action

    client, factory, _ = api
    iid = send(client, payload()).json()["accepted"][0]["incident_id"]
    with factory() as db:
        incident = db.get(Incident, iid)
        run = db.scalar(select(DiagnosisRun).where(DiagnosisRun.incident_id == iid))
        if blocked != "open":
            incident.status = "closed"
            incident.lifecycle = "ESCALATED"
        run.status = blocked if blocked in ["running", "waiting"] else "completed"
        if blocked == "queued_action":
            db.add(
                Action(
                    incident_id=iid,
                    run_id=run.id,
                    action_type="retry_video_ffmpeg_h264",
                    target_json="{}",
                    params_json="{}",
                    binding_hash="x",
                    policy_version="v",
                    idempotency_key="x",
                    approval_status="approved",
                    execution_status="queued",
                    created_at=datetime.utcnow(),
                    expires_at=datetime.utcnow() + timedelta(minutes=5),
                )
            )
        db.commit()
    assert (
        client.delete(
            "/api/incidents/" + iid, headers={"Authorization": "Bearer " + "o" * 40}
        ).status_code
        == 409
    )
    with factory() as db:
        assert not db.scalar(select(Timeline).where(Timeline.event_type == "incident_deleted"))


def test_bulk_delete_all_pages_auth_scope_and_active_guards(api):
    from app.models.aiops import Action
    from app.aiops.incidents import now

    client, factory, settings = api
    headers = {"Authorization": "Bearer " + "o" * 40}
    with factory() as db:
        ids = []
        for i in range(25):
            r = Incident(
                correlation_key="bulk:" + str(i),
                namespace="video-platform",
                resource_id="worker-test",
                service="transcoder",
                symptom="test",
                status="closed",
                lifecycle="RESOLVED",
                first_seen_at=now(),
                last_seen_at=now(),
            )
            db.add(r)
            db.flush()
            ids.append(r.id)
        for blocked in [
            "open",
            "pending",
            "running",
            "waiting",
            "queued_action",
            "running_action",
            "outside",
        ]:
            r = Incident(
                correlation_key="bulk:" + blocked,
                namespace="outside" if blocked == "outside" else "video-platform",
                resource_id="worker-test",
                service="transcoder",
                symptom=blocked,
                status="open" if blocked == "open" else "closed",
                lifecycle="ESCALATED",
                first_seen_at=now(),
                last_seen_at=now(),
            )
            db.add(r)
            db.flush()
            if blocked in ["pending", "running", "waiting", "queued_action", "running_action"]:
                run = DiagnosisRun(
                    incident_id=r.id,
                    status=blocked if blocked in ["pending", "running", "waiting"] else "completed",
                )
                db.add(run)
                db.flush()
                if blocked.endswith("_action"):
                    db.add(
                        Action(
                            incident_id=r.id,
                            run_id=run.id,
                            action_type="retry_video_ffmpeg_h264",
                            target_json="{}",
                            params_json="{}",
                            binding_hash="x",
                            policy_version="v",
                            idempotency_key=blocked,
                            approval_status="approved",
                            execution_status=blocked.split("_")[0],
                            created_at=now(),
                            expires_at=now() + timedelta(minutes=5),
                        )
                    )
        db.commit()
    assert client.delete("/api/incidents").status_code == 401
    assert (
        client.delete("/api/incidents", headers={"Authorization": "Bearer " + "w" * 40}).status_code
        == 401
    )
    result = client.delete("/api/incidents", headers=headers)
    assert result.status_code == 200
    assert result.json() == {"deleted_count": 25, "retained_count": 6, "audit_retained": True}
    visible = client.get("/api/incidents?limit=100", headers=headers).json()["items"]
    assert len(visible) == 6 and not any(r["id"] in ids for r in visible)
    assert client.delete("/api/incidents", headers=headers).json()["deleted_count"] == 0
    with factory() as db:
        assert db.scalar(select(func.count()).select_from(Incident)) == 32
        tombstones = db.scalars(
            select(Timeline).where(Timeline.event_type == "incident_deleted")
        ).all()
        assert len(tombstones) == 25 and all(t.actor == "local-operator" for t in tombstones)
        assert not db.scalar(
            select(Timeline)
            .join(Incident, Incident.id == Timeline.incident_id)
            .where(Incident.namespace == "outside")
        )


def test_bulk_delete_empty_returns_zero(api):
    client, factory, _ = api
    assert client.delete(
        "/api/incidents", headers={"Authorization": "Bearer " + "o" * 40}
    ).json() == {"deleted_count": 0, "retained_count": 0, "audit_retained": True}


def test_new_runs_always_use_v2_without_rewriting_historical_reports(api, monkeypatch):
    import app.aiops.incidents as incidents

    client, factory, settings = api
    settings.aiops_graph_enabled = True
    monkeypatch.setattr("app.core.config.get_settings", lambda: settings)
    first = send(client, payload()).json()["accepted"][0]["incident_id"]
    with factory() as db:
        run = db.scalar(select(DiagnosisRun).where(DiagnosisRun.incident_id == first))
        assert run.workflow_kind == "aiops-hypothesis-v2"
        run.workflow_kind = "aiops-graph"
        run.status = "completed"
        db.commit()
    result = client.post(
        "/api/incidents/" + first + "/analyze",
        headers={"Authorization": "Bearer " + "o" * 40, "Idempotency-Key": "v2-new-run"},
    )
    with factory() as db:
        assert db.get(DiagnosisRun, result.json()["run_id"]).workflow_kind == "aiops-hypothesis-v2"
        old = db.scalar(
            select(DiagnosisRun).where(
                DiagnosisRun.incident_id == first, DiagnosisRun.status == "completed"
            )
        )
        assert old.workflow_kind == "aiops-graph"
