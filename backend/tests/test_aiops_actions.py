"""Security and lease regression through persisted hypothesis-v2 video proofs."""

import json
from datetime import timedelta
import pytest
from app.aiops.actions import Decision, decide, policy
from app.aiops.executor import execute
from app.aiops.incidents import now
from app.models.aiops import Action, Approval
from app.models.diagnostics import DiagnosisRun


def approve(case):
    factory, settings, client, aid, *_ = case
    with factory() as db:
        a = db.get(Action, aid)
        decide(
            db,
            a,
            Decision(
                decision="approved",
                reason="Reviewed exact v2 original-video repair",
                binding_hash=a.binding_hash,
            ),
            settings.aiops_operator_principal,
            client,
            settings,
        )
        a.execution_status = "queued"
        db.commit()


def test_uncertain_mutation_is_never_replayed(platform):
    factory, settings, client, aid, *_ = platform
    approve(platform)
    client.patch.side_effect = TimeoutError("Unknown mutation outcome")
    assert execute(aid, client, factory, settings)["status"] == "uncertain"
    assert execute(aid, client, factory, settings) == {"attempted": False}
    client.patch.assert_called_once()


def test_rejected_and_conflicting_decision_cannot_authorize_execution(platform):
    factory, settings, client, aid, *_ = platform
    with factory() as db:
        a = db.get(Action, aid)
        decision = Decision(
            decision="rejected",
            reason="Operator rejected proposed repair",
            binding_hash=a.binding_hash,
        )
        decide(db, a, decision, settings.aiops_operator_principal, client, settings)
        decide(db, a, decision, settings.aiops_operator_principal, client, settings)
        with pytest.raises(ValueError):
            decide(
                db,
                a,
                decision.model_copy(update={"decision": "approved"}),
                settings.aiops_operator_principal,
                client,
                settings,
            )
        a.execution_status = "queued"
        db.commit()
    assert not execute(aid, client, factory, settings)["attempted"]
    client.patch.assert_not_called()


@pytest.mark.parametrize("phase", ["queued", "running"])
def test_executor_lease_expiry_wakes_only_v2_without_replay(platform, phase):
    from app.aiops.graph_worker import claim

    factory, settings, client, aid, *_ = platform
    approve(platform)
    with factory() as db:
        a = db.get(Action, aid)
        a.execution_status = phase
        a.started_at = now() - timedelta(minutes=6) if phase == "running" else None
        a.expires_at = now() - timedelta(seconds=1)
        db.get(DiagnosisRun, a.run_id).status = "waiting"
        db.commit()
    assert claim(factory)
    with factory() as db:
        assert db.get(Action, aid).execution_status == (
            "denied" if phase == "queued" else "uncertain"
        )
    client.patch.assert_not_called()


def test_legacy_report_cannot_be_used_to_authorize_v2_repair(platform):
    factory, settings, client, aid, *_ = platform
    with factory() as db:
        a = db.get(Action, aid)
        db.get(DiagnosisRun, a.run_id).workflow_kind = "aiops-graph"
        db.commit()
        with pytest.raises(PermissionError, match="read-only"):
            policy(db, a, client, settings)
        a.execution_status = "queued"
        db.commit()
    assert not execute(aid, client, factory, settings)["attempted"]
    client.patch.assert_not_called()


def test_authenticated_decision_binds_server_principal_without_mutating_worker(
    platform, monkeypatch
):
    from fastapi.testclient import TestClient
    from pydantic import SecretStr
    from app.aiops.main import app
    from app.core.config import get_settings
    from app.database.session import get_db

    factory, settings, worker, aid, *_ = platform
    settings.aiops_enabled = True
    settings.aiops_allowed_namespaces = "video-platform"
    settings.aiops_operator_token = SecretStr("o" * 40)
    settings.aiops_webhook_token = SecretStr("w" * 40)

    def database():
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = database
    app.dependency_overrides[get_settings] = lambda: settings
    monkeypatch.setattr("app.aiops.executor.KubernetesActionClient", lambda: worker)
    try:
        with factory() as db:
            a = db.get(Action, aid)
            path = f"/api/incidents/{a.incident_id}/actions/{aid}/decision"
            body = {
                "decision": "approved",
                "reason": "Reviewed original video password=private",
                "binding_hash": a.binding_hash,
            }
        with TestClient(app) as client:
            for token in [None, "w" * 40, "wrong"]:
                assert (
                    client.post(
                        path,
                        json=body,
                        headers={"Authorization": "Bearer " + token} if token else {},
                    ).status_code
                    == 401
                )
            headers = {"Authorization": "Bearer " + "o" * 40}
            assert client.post(path, json=body, headers=headers).status_code == 200
            assert client.post(path, json=body, headers=headers).status_code == 200
        with factory() as db:
            approval = db.get(Approval, aid)
            assert (
                approval.principal == settings.aiops_operator_principal
                and "private" not in approval.reason
            )
        worker.patch.assert_not_called()
    finally:
        app.dependency_overrides.clear()


@pytest.mark.parametrize(
    "condition,expected",
    [
        ("ready", ""),
        ("expired", "過期"),
        ("disabled", "尚未啟用"),
        ("insufficient", "證據驗證"),
        ("legacy", "歷史"),
        ("new_run", "較新的診斷"),
    ],
)
def test_action_review_hints_are_read_only_and_preserve_authority(
    platform, condition, expected, monkeypatch
):
    from fastapi.testclient import TestClient
    from pydantic import SecretStr
    from app.aiops.main import app
    from app.core.config import get_settings
    from app.database.session import get_db
    from app.models.aiops import Diagnosis

    factory, settings, worker, aid, *_ = platform
    settings.aiops_enabled = True
    settings.aiops_operator_token = SecretStr("o" * 40)
    settings.aiops_webhook_token = SecretStr("w" * 40)
    with factory() as db:
        action = db.get(Action, aid)
        iid = action.incident_id
        run = db.get(DiagnosisRun, action.run_id)
        if condition == "expired":
            action.expires_at = now() - timedelta(seconds=1)
        if condition == "disabled":
            settings.aiops_hypothesis_remediation_enabled = False
        if condition == "insufficient":
            db.get(Diagnosis, run.id).validation_status = "insufficient_evidence"
        if condition == "legacy":
            run.workflow_kind = "aiops-graph"
        if condition == "new_run":
            run.status = "completed"
            db.flush()
            db.add(
                DiagnosisRun(incident_id=iid, status="pending", workflow_kind="aiops-hypothesis-v2")
            )
        db.commit()

    def database():
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = database
    app.dependency_overrides[get_settings] = lambda: settings

    def no_remote_client(*args, **kwargs):
        raise AssertionError("Polling review hints must not execute policy tools or mutations")

    monkeypatch.setattr("app.aiops.executor.KubernetesActionClient", no_remote_client)
    try:
        client = TestClient(app)
        path = f"/api/incidents/{iid}/actions"
        assert client.get(path).status_code == 401
        response = client.get(path, headers={"Authorization": "Bearer " + "o" * 40})
        assert response.status_code == 200
        hint = response.json()["items"][0]["review"]
        assert hint["can_approve"] is (condition == "ready")
        assert hint["policy_recheck_required"] is True
        assert expected in hint["reason"]
        if condition == "disabled":
            assert response.json()["remediation_enabled"] is False
        with factory() as db:
            assert db.get(Action, aid).approval_status == "pending"
            assert db.get(Approval, aid) is None
        worker.patch.assert_not_called()
    finally:
        app.dependency_overrides.clear()


def test_latest_reviewable_action_survives_more_than_100_historical_actions(platform):
    from uuid import UUID, uuid4
    from app.aiops.main import list_actions

    factory, settings, worker, aid, *_ = platform
    with factory() as db:
        action = db.get(Action, aid)
        iid = action.incident_id
        baseline = {
            column.name: getattr(action, column.name) for column in Action.__table__.columns
        }
        for index in range(105):
            run = DiagnosisRun(
                incident_id=iid, status="completed", workflow_kind="aiops-hypothesis-v2"
            )
            db.add(run)
            db.flush()
            values = {
                **baseline,
                "id": str(uuid4()),
                "run_id": run.id,
                "idempotency_key": str(uuid4()),
                "created_at": action.created_at - timedelta(minutes=index + 1),
            }
            db.add(Action(**values))
        db.commit()
        result = list_actions(UUID(iid), principal="fixture-operator", db=db, settings=settings)
        assert len(result["items"]) == 100
        assert result["items"][0]["id"] == aid
        assert result["items"][0]["review"]["can_approve"] is True
        assert all(not row["review"]["can_approve"] for row in result["items"][1:])
        assert db.get(Approval, aid) is None
    worker.patch.assert_not_called()
