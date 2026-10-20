"""Original video repairs: fixed resources and parameters, exact failed task, persisted approval."""

import hashlib
import json
import uuid
from datetime import timedelta
from app.aiops.actions import binding, configure, pod_allowed, spec_for
from app.aiops.incidents import now, timeline
from app.aiops.video_bridge import linked_job
from app.models.aiops import Approval, Action, Diagnosis, Evidence
from app.models.diagnostics import Incident


def params_for(db, run_id, kind):
    report = db.get(Diagnosis, run_id)
    if not report or report.validation_status != "validated":
        raise PermissionError("Validated video diagnosis required")
    incident = db.get(Incident, report.incident_id)
    link = linked_job(incident)
    if not link:
        raise PermissionError("Original video link required")
    output = json.loads(report.payload_json)
    for eid in output.get("evidence_ids", []):
        row = db.get(Evidence, eid)
        if (
            not row
            or row.run_id != run_id
            or row.incident_id != incident.id
            or row.kind != "get_task_status"
            or row.source != "video_task_api"
            or json.loads(row.errors_json)
            or hashlib.sha256(row.payload_json.encode()).hexdigest() != row.checksum
        ):
            continue
        data = json.loads(row.payload_json)
        if (
            data.get("job_id") != link["job_id"]
            or data.get("task_id") != link["task_id"]
            or not data.get("job_present")
        ):
            continue
        codec = "h264" if kind == "retry_video_ffmpeg_h264" else data.get("video_codec")
        if codec not in {"h264", "h265"}:
            raise PermissionError("Original video codec unavailable")
        params = {"job_id": link["job_id"], "failed_task_id": link["task_id"], "video_codec": codec}
        if kind == "restore_video_redis_and_retry":
            params["redis_url"] = "redis://redis:6379/0"
        if kind == "increase_video_worker_memory_and_retry":
            params["memory_limit"] = "2Gi"
        return params
    raise PermissionError("Cited original video task evidence required")


def worker_spec_hash(obj):
    spec = dict(obj["spec"])
    spec.pop("replicas", None)
    return hashlib.sha256(
        json.dumps(spec, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def worker_pool(client, obj):
    replicas = obj["spec"].get("replicas", 1)
    if type(replicas) is not int or replicas < 1:
        raise PermissionError("Video worker requires at least one desired replica")
    pods = client.request(
        "GET", "/api/v1/namespaces/video-platform/pods?labelSelector=app%3Dworker"
    )["items"]
    available = []
    for item in pods:
        if item["metadata"].get("deletionTimestamp"):
            continue
        pod = client.validate_pod(item["metadata"]["name"], obj["metadata"]["uid"])
        if pod["metadata"].get("deletionTimestamp"):
            continue
        status = pod.get("status", {})
        if status.get("phase") == "Running" and any(
            c.get("type") == "Ready" and c.get("status") == "True"
            for c in status.get("conditions", [])
        ):
            available.append(pod)
    if not available:
        raise PermissionError("No ready video worker in the bound Deployment")
    return sorted(available, key=lambda p: p["metadata"]["name"])


def video_policy(db, action, client, settings, require_approval):
    spec = spec_for(action.action_type)
    target = json.loads(action.target_json)
    if (
        not settings.aiops_video_enabled
        or not settings.aiops_approval_enabled
        or not settings.remediation_enabled
    ):
        raise PermissionError("Video repair disabled")
    if (
        action.policy_version != spec["policy"]
        or action.expires_at <= now()
        or action.binding_hash != binding(action)
    ):
        raise PermissionError("Video action binding or expiry invalid")
    if (
        target.get("namespace") != "video-platform"
        or target.get("deployment") != "worker"
        or not pod_allowed(target.get("pod"), "worker")
        or json.loads(action.params_json) != params_for(db, action.run_id, action.action_type)
    ):
        raise PermissionError("Video target or parameters changed")
    incident = db.get(Incident, action.incident_id)
    report = db.get(Diagnosis, action.run_id)
    if (
        not incident
        or incident.status != "open"
        or not linked_job(incident)
        or not report
        or report.incident_id != incident.id
    ):
        raise PermissionError("Video incident no longer active")
    output = json.loads(report.payload_json)
    if (
        output.get("root_cause_code") != spec["root"]
        or output.get("proposed_action_type") != action.action_type
    ):
        raise PermissionError("Diagnosis does not support video repair")
    if require_approval:
        approval = db.get(Approval, action.id)
        if (
            not approval
            or approval.decision != "approved"
            or approval.binding_hash != action.binding_hash
            or approval.principal != settings.aiops_operator_principal
            or approval.decided_at > action.expires_at
            or action.approval_status != "approved"
        ):
            raise PermissionError("Authenticated bound approval required")
    configure(client, action.action_type, action)
    obj = client.deployment()
    meta = obj["metadata"]
    if (
        meta.get("labels", {}).get("aiops.video-platform/repair-enabled") != "true"
        or meta["uid"] != target["uid"]
    ):
        raise PermissionError("Bound video worker changed")
    if target.get("worker_binding_version") == 2:
        if worker_spec_hash(obj) != target.get("deployment_spec_hash"):
            raise PermissionError("Bound video worker configuration changed; new approval required")
        worker_pool(client, obj)
    else:
        # Historical approvals retain their original single-Pod contract.
        if (
            target.get("worker_binding_version") is not None
            or meta["resourceVersion"] != target["resource_version"]
            or obj["spec"]["replicas"] != 1
        ):
            raise PermissionError("Bound video worker changed")
        pod = client.validate_pod(target["pod"], meta["uid"])
        if pod["metadata"]["uid"] != target["pod_uid"]:
            raise PermissionError("Bound video worker Pod changed")
    client.preflight()
    return obj


def video_propose(db, incident, run, client, ttl):
    kind = json.loads(db.get(Diagnosis, run.id).payload_json)["proposed_action_type"]
    spec = spec_for(kind)
    params = params_for(db, run.id, kind)
    configure(client, kind)
    obj = client.deployment()
    meta = obj["metadata"]
    if meta.get("labels", {}).get("aiops.video-platform/repair-enabled") != "true":
        raise PermissionError("Video worker repair label missing")
    pod = worker_pool(client, obj)[0]
    target = {
        "namespace": "video-platform",
        "deployment": "worker",
        "uid": meta["uid"],
        "resource_version": meta["resourceVersion"],
        "pod": pod["metadata"]["name"],
        "pod_uid": pod["metadata"]["uid"],
        "worker_binding_version": 2,
        "deployment_spec_hash": worker_spec_hash(obj),
        "replicas_at_proposal": obj["spec"].get("replicas", 1),
    }
    if kind == "restore_video_redis_and_retry":
        backend = client.request(
            "GET", "/apis/apps/v1/namespaces/video-platform/deployments/backend"
        )
        if (
            backend["metadata"].get("labels", {}).get("aiops.video-platform/repair-enabled")
            != "true"
        ):
            raise PermissionError("Video backend repair not enabled")
        target["backend"] = {
            "uid": backend["metadata"]["uid"],
            "resource_version": backend["metadata"]["resourceVersion"],
        }
    row = Action(
        id=str(uuid.uuid5(uuid.NAMESPACE_URL, "action:" + run.id)),
        run_id=run.id,
        incident_id=incident.id,
        action_type=kind,
        target_json=json.dumps(target, sort_keys=True),
        params_json=json.dumps(params, sort_keys=True),
        policy_version=spec["policy"],
        idempotency_key=hashlib.sha256(("execute:" + run.id).encode()).hexdigest(),
        created_at=now(),
        expires_at=now() + timedelta(seconds=min(3600, max(5, ttl))),
        approval_status="pending",
        execution_status="pending",
    )
    row.binding_hash = binding(row)
    db.add(row)
    db.flush()
    timeline(
        db,
        incident,
        "action_proposed",
        "video-policy",
        "proposal:" + row.id,
        {"action_id": row.id, "job_id": params["job_id"], "binding_hash": row.binding_hash},
    )
    return row
