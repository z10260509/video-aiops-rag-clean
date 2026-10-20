from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import get_settings, Settings
from app.database.session import Base


@pytest.fixture()
def db_and_settings(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine, autocommit=False, autoflush=False)
    settings = get_settings().model_copy(update={"storage_root": Path(tmp_path / "storage")})

    yield TestingSession, settings


# Shared fixture uses persisted v2 assertions and real policy checks for all three repairs.
import hashlib, json, uuid
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock
from sqlalchemy.orm import sessionmaker
from app.aiops.actions import propose
from app.aiops.incidents import now
from app.models.aiops import Diagnosis, Evidence
from app.models.diagnostics import Incident, DiagnosisRun

KINDS = {
    "retry_video_ffmpeg_h264": "FFMPEG_INVALID_CODEC",
    "restore_video_redis_and_retry": "REDIS_CONNECTIVITY",
    "increase_video_worker_memory_and_retry": "WORKER_OOM",
}


def install_v2_report(db, incident, run, kind, jid, tid, puid):
    from app.aiops.hypotheses import RoundOutput
    from sqlalchemy import select

    start = now() - timedelta(seconds=2)
    error = (
        "Unknown encoder 'broken'"
        if kind == "retry_video_ffmpeg_h264"
        else "Error -2 Name or service not known"
        if kind == "restore_video_redis_and_retry"
        else "OOMKilled"
    )
    data = {
        "job_id": jid,
        "task_id": tid,
        "job_present": True,
        "video_codec": "h265",
        "pod_uid": puid,
        "started_at": start.isoformat() + "Z",
        "execution_status": "running"
        if kind == "increase_video_worker_memory_and_retry"
        else "failed",
        "error": error,
    }
    eid = str(uuid.uuid4())
    raw = json.dumps(data)
    target = {
        "namespace": incident.namespace,
        "pod": incident.resource_id,
        "job_id": jid,
        "task_id": tid,
    }

    def evidence(eid, tool, source, data):
        raw = json.dumps(data)
        db.add(
            Evidence(
                id=eid,
                incident_id=incident.id,
                run_id=run.id,
                kind=tool,
                source=source,
                requested_at=now(),
                observed_at=now(),
                query_json=json.dumps(target),
                payload_json=raw,
                checksum=hashlib.sha256(raw.encode()).hexdigest(),
                errors_json="[]",
                truncated=False,
            )
        )

    evidence(eid, "get_task_status", "video_task_api", data)
    claims = [
        {
            "evidence_id": eid,
            "path": "/error",
            "operator": "contains",
            "value_type": "string",
            "expected": error,
        },
        {
            "evidence_id": eid,
            "path": "/execution_status",
            "operator": "equals",
            "value_type": "string",
            "expected": data["execution_status"],
        },
    ]
    ids = [eid]
    required = ["get_task_status"]
    if kind == "increase_video_worker_memory_and_retry":
        pid = str(uuid.uuid4())
        evidence(
            pid,
            "get_pod_status",
            "kubernetes",
            {
                "uid": puid,
                "containers": [
                    {
                        "name": "worker",
                        "last_state": {
                            "terminated": {
                                "reason": "OOMKilled",
                                "finishedAt": now().isoformat() + "Z",
                            }
                        },
                    }
                ],
            },
        )
        claims.append(
            {
                "evidence_id": pid,
                "path": "/containers/0/last_state/terminated/reason",
                "operator": "equals",
                "value_type": "string",
                "expected": "OOMKilled",
            }
        )
        ids.append(pid)
        required.append("get_pod_status")
    candidate = RoundOutput.model_validate(
        {
            "hypotheses": [
                {
                    "id": "H1",
                    "cause": KINDS[kind],
                    "explanation": "Actual failed task and scoped tool observations",
                    "assertions": claims,
                    "required_tools": required,
                    "search_query": "fixture",
                }
            ],
            "selected_hypothesis_id": "H1",
            "repair_options": [
                {
                    "id": "P1",
                    "hypothesis_id": "H1",
                    "action_type": kind,
                    "rationale": "Bound original task",
                    "risk": "Reviewed test action",
                    "prerequisites": [],
                    "verification": "Same video fresh output",
                    "runbook_chunk_ids": ["fixture-runbook"],
                }
            ],
            "selected_option_id": "P1",
            "summary": "Verified test hypothesis",
        }
    )
    report = db.get(Diagnosis, run.id)
    report.payload_json = json.dumps(
        {
            "root_cause_code": KINDS[kind],
            "proposed_action_type": kind,
            "evidence_ids": ids,
            "investigation": {
                "candidate": candidate.model_dump(),
                "evidence_ids": ids,
                "hypothesis_references": {"H1": ["fixture-runbook"]},
            },
        }
    )
    db.flush()


@pytest.fixture(params=list(KINDS))
def platform(request, tmp_path):
    kind = request.param
    cause = KINDS[kind]
    engine = create_engine("sqlite:///" + str(tmp_path / "control.db"))
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine)
    settings = Settings(
        _env_file=None,
        aiops_video_enabled=True,
        aiops_approval_enabled=True,
        remediation_enabled=True,
        aiops_hypothesis_remediation_enabled=True,
    )
    jid, tid, puid = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
    obj = {
        "metadata": {
            "uid": "worker-uid",
            "resourceVersion": "12",
            "labels": {"aiops.video-platform/repair-enabled": "true"},
        },
        "spec": {"replicas": 1},
    }
    pod = {
        "metadata": {"name": "worker-abc", "uid": puid},
        "status": {"phase": "Running", "conditions": [{"type": "Ready", "status": "True"}]},
    }
    backend = {
        "metadata": {
            "uid": "backend-uid",
            "resourceVersion": "42",
            "labels": {"aiops.video-platform/repair-enabled": "true"},
        }
    }
    client = SimpleNamespace(
        configure=Mock(),
        deployment=Mock(return_value=obj),
        validate_pod=Mock(return_value=pod),
        request=Mock(
            side_effect=lambda method, path: {"items": [pod]} if "/pods?" in path else backend
        ),
        preflight=Mock(),
        patch=Mock(return_value=obj),
        verify=Mock(),
    )
    with factory() as db:
        incident = Incident(
            correlation_key=f"video-job:{jid}:{tid}",
            namespace="video-platform",
            resource_id="worker-old",
            service="transcoder",
            symptom="VideoJobFailure",
            lifecycle="WAITING_APPROVAL",
            first_seen_at=now(),
            last_seen_at=now(),
        )
        db.add(incident)
        db.flush()
        run = DiagnosisRun(
            incident_id=incident.id, status="waiting", workflow_kind="aiops-hypothesis-v2"
        )
        db.add(run)
        db.flush()
        raw = json.dumps(
            {"job_id": jid, "task_id": tid, "job_present": True, "video_codec": "h265"}
        )
        eid = str(uuid.uuid4())
        db.add(
            Evidence(
                id=eid,
                incident_id=incident.id,
                run_id=run.id,
                kind="get_task_status",
                source="video_task_api",
                requested_at=now(),
                observed_at=now(),
                query_json="{}",
                payload_json=raw,
                checksum=hashlib.sha256(raw.encode()).hexdigest(),
                errors_json="[]",
                truncated=False,
            )
        )
        db.add(
            Diagnosis(
                run_id=run.id,
                incident_id=incident.id,
                thread_id="video-test",
                model_id="fake",
                prompt_version="v1",
                validation_status="validated",
                payload_json=json.dumps(
                    {"root_cause_code": cause, "proposed_action_type": kind, "evidence_ids": [eid]}
                ),
                created_at=now(),
            )
        )
        db.flush()
        install_v2_report(db, incident, run, kind, jid, tid, puid)
        action = propose(db, incident, run, client)
        aid = action.id
        params = json.loads(action.params_json)
        proof = {
            "deployment_uid": "worker-uid",
            "available_replicas": 1,
            "fresh_sample": {
                "action_id": aid,
                "job_id": jid,
                "task_id": str(uuid.uuid5(uuid.NAMESPACE_URL, "video-repair:" + aid)),
                "sample_status": "COMPLETED",
                "new_outcome_completed": True,
                "output_codec": "h264" if kind == "retry_video_ffmpeg_h264" else "hevc",
                "details": {
                    "memory_before": {"memory_limit_bytes": 2 * 1024**3, "oom_kills": 0},
                    "memory_after": {"memory_limit_bytes": 2 * 1024**3, "oom_kills": 0},
                },
            },
            "memory_limit": "2Gi",
            "redis_restored": True,
            "backend_uid": "backend-uid",
        }
        client.verify.return_value = proof
        db.commit()
    return factory, settings, client, aid, kind, params
