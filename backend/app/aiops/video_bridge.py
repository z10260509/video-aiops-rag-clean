"""Join original video jobs to control incidents without sharing database ownership."""

import json
import uuid
from datetime import datetime
import httpx
from sqlalchemy import select
from app.aiops.incidents import lock_key, now, schedule, timeline
from app.core.config import get_settings
from app.models.diagnostics import Incident

BASE = "http://backend.video-platform.svc.cluster.local/internal/aiops"


def oom_termination_matches(termination, started_at):
    if termination.get("reason") != "OOMKilled":
        return False
    try:
        return datetime.fromisoformat(
            termination["finishedAt"].replace("Z", "+00:00")
        ) >= datetime.fromisoformat(started_at.replace("Z", "+00:00"))
    except (KeyError, ValueError, TypeError):
        return False


def source(path, settings=None, method="GET", body=None, repair=False):
    settings = settings or get_settings()
    if not settings.aiops_video_enabled:
        raise PermissionError("Video integration disabled")
    token = (
        settings.aiops_video_repair_token if repair else settings.aiops_video_reader_token
    ).get_secret_value()
    if len(token) < 32:
        raise PermissionError("Video source identity unavailable")
    with httpx.Client(timeout=8, follow_redirects=False) as client:
        with client.stream(
            method, BASE + path, json=body, headers={"Authorization": "Bearer " + token}
        ) as response:
            if response.status_code != 200:
                raise ValueError("Video source unavailable: " + str(response.status_code))
            data = bytearray()
            for part in response.iter_bytes(chunk_size=4096):
                data.extend(part)
                if len(data) > 65536:
                    raise ValueError("Video source response too large")
    return json.loads(data)


def linked_job(incident):
    if incident.namespace != "video-platform" or not incident.correlation_key.startswith(
        "video-job:"
    ):
        return None
    _, job, task = incident.correlation_key.split(":")
    return {"job_id": str(uuid.UUID(job)), "task_id": str(uuid.UUID(task))}


def verify_reopen(incident, settings):
    """Read-only checks for an operator-requested reopen of the original attempt."""
    link = linked_job(incident)
    if not link or not settings.aiops_video_enabled:
        raise ValueError("僅能重新開啟仍待修復的原影片事件。")
    try:
        data = source("/jobs/" + link["job_id"] + "?task_id=" + link["task_id"], settings)
    except (ValueError, PermissionError, httpx.HTTPError):
        raise ValueError("無法確認原影片狀態；事件未重新開啟，請稍後再試。") from None
    if not isinstance(data, dict):
        raise ValueError("原影片服務回應無效；事件未重新開啟。")
    if (
        data.get("found") is not True
        or data.get("job_present") is not True
        or any(data.get(k) != v for k, v in link.items())
        or data.get("latest_task_id") != link["task_id"]
        or data.get("retry_reserved") is not False
    ):
        raise ValueError("原影片任務已變更、已有重跑紀錄或不存在；不能沿用這筆事件重新修復。")
    if data.get("input_available") is not True:
        raise ValueError("原上傳檔已不存在，無法重新建立修復草案。")
    if data.get("job_status") == "FAILED" and data.get("execution_status") == "failed":
        return link
    if data.get("job_status") == "PROCESSING" and data.get("execution_status") == "running":
        from app.aiops.gateway import gateway, ToolRequest

        try:
            result = gateway().call(
                ToolRequest(
                    tool_name="get_pod_status",
                    namespace=incident.namespace,
                    pod=incident.resource_id,
                )
            )
        except (ValueError, PermissionError, httpx.HTTPError):
            raise ValueError("無法查核原 worker 的 OOM；事件未重新開啟。") from None
        pod = result.data or {}
        if (
            not result.errors
            and not result.truncated
            and data.get("pod") == incident.resource_id
            and pod.get("uid") == data.get("pod_uid")
            and any(
                c.get("name") == "worker"
                and oom_termination_matches(
                    c.get("last_state", {}).get("terminated", {}), data.get("started_at", "")
                )
                for c in pod.get("containers", [])
            )
        ):
            return link
    raise ValueError("影片已完成、正在正常處理，或未能確認原 worker 的 OOM；不會重新開啟修復事件。")


def register(db, record, adapter, settings):
    job = str(uuid.UUID(record["job_id"]))
    task = str(uuid.UUID(record["task_id"]))
    key = "video-job:" + job + ":" + task
    lock_key(db, key)
    old = db.scalar(select(Incident).where(Incident.correlation_key == key))
    if old:
        return old
    pod = record.get("pod", "")
    if not pod.startswith("worker-"):
        from app.aiops.gateway import ToolRequest

        result = adapter.call(ToolRequest(tool_name="list_pods", namespace="video-platform"))
        pods = (result.data or {}).get("pods", [])
        if result.errors or len(pods) != 1:
            return None
        pod = pods[0]["name"]
    at = now()
    row = Incident(
        id=str(uuid.uuid5(uuid.NAMESPACE_URL, key)),
        correlation_key=key,
        namespace="video-platform",
        resource_kind="Pod",
        resource_id=pod,
        service="transcoder",
        symptom="VideoJobFailure",
        first_seen_at=at,
        last_seen_at=at,
    )
    db.add(row)
    db.flush()
    timeline(
        db,
        row,
        "video_job_linked",
        "video-bridge",
        key,
        {
            "job_id": job,
            "failed_task_id": task,
            "original_filename": record.get("original_filename"),
        },
    )
    schedule(db, row, "video-bridge")
    return row


def discover(factory, adapter, settings=None):
    settings = settings or get_settings()
    if not settings.aiops_video_enabled:
        return
    records = source("/failures", settings)["items"]
    for record in records:
        if (
            settings.aiops_video_scan_since
            and record["started_at"] < settings.aiops_video_scan_since
        ):
            continue
        if record["execution_status"] == "running":
            # A running attempt is diagnosed only after the original worker has a real OOM termination.
            from app.aiops.gateway import ToolRequest

            result = adapter.call(
                ToolRequest(
                    tool_name="get_pod_status", namespace="video-platform", pod=record["pod"]
                )
            )
            data = result.data or {}
            if (
                result.errors
                or data.get("uid") != record["pod_uid"]
                or not any(
                    c.get("name") == "worker"
                    and oom_termination_matches(
                        c.get("last_state", {}).get("terminated", {}), record["started_at"]
                    )
                    for c in data.get("containers", [])
                )
            ):
                continue
        with factory() as db:
            register(db, record, adapter, settings)
            db.commit()
