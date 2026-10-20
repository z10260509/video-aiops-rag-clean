"""Fixed video-platform mutations followed by verification of the original uploaded video."""

import json
import time
import uuid
from app.aiops.video_bridge import source, oom_termination_matches


def payload(client):
    return {k: client.params[k] for k in ["job_id", "failed_task_id", "video_codec"]} | {
        "action_id": client.action_id,
        "root_cause": client.spec["root"],
    }


def preflight(client):
    data = source(
        "/jobs/" + client.params["job_id"] + "?task_id=" + client.params["failed_task_id"],
        client.settings,
    )
    if (
        not data.get("job_present")
        or not data.get("input_available")
        or data.get("latest_task_id") != client.params["failed_task_id"]
        or data.get("retry_reserved")
    ):
        raise PermissionError("Original video input or failed task changed")
    root = client.spec["root"]
    if root == "WORKER_OOM":
        if data["execution_status"] != "running" or data["job_status"] != "PROCESSING":
            raise PermissionError("Original interrupted video changed")
        pod = client.request("GET", "/api/v1/namespaces/video-platform/pods/" + data["pod"])
        if pod["metadata"]["uid"] != data["pod_uid"] or not any(
            c["name"] == "worker"
            and oom_termination_matches(
                c.get("lastState", {}).get("terminated", {}), data["started_at"]
            )
            for c in pod.get("status", {}).get("containerStatuses", [])
        ):
            raise PermissionError("Original worker OOM no longer verifiable")
        worker = next(
            c
            for c in client.deployment()["spec"]["template"]["spec"]["containers"]
            if c["name"] == "worker"
        )
        if worker["resources"]["limits"]["memory"] != "1Gi":
            raise PermissionError("Video memory baseline changed; new policy required")
    elif (
        data["execution_status"] != "failed"
        or data["job_status"] != "FAILED"
        or data.get("error_kind") != root
    ):
        raise PermissionError("Original video failure changed")
    if root == "REDIS_CONNECTIVITY":
        obj = client.request("GET", "/apis/apps/v1/namespaces/video-platform/deployments/backend")
        bound = client.target.get("backend", {})
        if (
            obj["metadata"]["uid"] != bound.get("uid")
            or obj["metadata"]["resourceVersion"] != bound.get("resource_version")
            or obj["metadata"].get("labels", {}).get("aiops.video-platform/repair-enabled")
            != "true"
        ):
            raise PermissionError("Bound video backend changed")


def patch_env(client, obj, path, container_name):
    containers = obj["spec"]["template"]["spec"]["containers"]
    i = next(i for i, c in enumerate(containers) if c["name"] == container_name)
    env = containers[i]["env"]
    j = next(i for i, e in enumerate(env) if e["name"] == "REDIS_URL")
    return client.request(
        "PATCH",
        path,
        [
            {"op": "test", "path": "/metadata/uid", "value": obj["metadata"]["uid"]},
            {
                "op": "test",
                "path": "/metadata/resourceVersion",
                "value": obj["metadata"]["resourceVersion"],
            },
            {
                "op": "replace",
                "path": f"/spec/template/spec/containers/{i}/env/{j}",
                "value": {"name": "REDIS_URL", "value": "redis://redis:6379/0"},
            },
        ],
    )


def ready(obj):
    replicas = obj["spec"]["replicas"]
    status = obj.get("status", {})
    return (
        status.get("observedGeneration", 0) >= obj["metadata"].get("generation", 0)
        and status.get("updatedReplicas", 0) == replicas
        and status.get("availableReplicas", 0) == replicas
    )


def patch(client, before):
    after = before
    root = client.spec["root"]
    if root == "REDIS_CONNECTIVITY":
        path = "/apis/apps/v1/namespaces/video-platform/deployments/backend"
        backend = client.request("GET", path)
        # Recheck both versions immediately before either patch.
        preflight(client)
        patch_env(client, backend, path, "backend")
        after = patch_env(client, before, client.path, "worker")
    elif root == "WORKER_OOM":
        i = next(
            i
            for i, c in enumerate(before["spec"]["template"]["spec"]["containers"])
            if c["name"] == "worker"
        )
        path = f"/spec/template/spec/containers/{i}/resources/limits/memory"
        after = client.request(
            "PATCH",
            client.path,
            [
                {"op": "test", "path": "/metadata/uid", "value": before["metadata"]["uid"]},
                {
                    "op": "test",
                    "path": "/metadata/resourceVersion",
                    "value": before["metadata"]["resourceVersion"],
                },
                {"op": "test", "path": path, "value": "1Gi"},
                {"op": "replace", "path": path, "value": "2Gi"},
            ],
        )
    if root != "FFMPEG_INVALID_CODEC":
        deadline = time.monotonic() + 75
        while True:
            healthy = ready(client.deployment())
            if root == "REDIS_CONNECTIVITY":
                healthy = healthy and ready(
                    client.request(
                        "GET", "/apis/apps/v1/namespaces/video-platform/deployments/backend"
                    )
                )
            if healthy:
                break
            if time.monotonic() > deadline:
                raise ValueError("Video rollout has not recovered")
            time.sleep(2)
    result = source("/repair", client.settings, method="POST", body=payload(client), repair=True)
    if (
        result.get("accepted") is not True
        or result.get("action_id") != client.action_id
        or result.get("task_id")
        != str(uuid.uuid5(uuid.NAMESPACE_URL, "video-repair:" + client.action_id))
    ):
        raise ValueError("Original video retry receipt mismatch")
    return after


def verify(client, read_only=False):
    obj = client.deployment()
    if obj["metadata"]["uid"] != client.target["uid"] or not ready(obj):
        raise ValueError("Video worker rollout not ready")
    sample = source("/verify/" + client.action_id, client.settings, repair=not read_only)
    pod = client.validate_pod(sample["pod_name"], obj["metadata"]["uid"])
    if pod["metadata"]["uid"] != sample["pod_uid"] or not any(
        c["name"] == "worker" and c.get("ready") and c.get("state", {}).get("running")
        for c in pod.get("status", {}).get("containerStatuses", [])
    ):
        raise ValueError("Original video retry worker mismatch")
    result = {
        "fresh_sample": sample,
        "deployment_uid": obj["metadata"]["uid"],
        "available_replicas": obj["status"]["availableReplicas"],
        "desired_replicas": obj["spec"]["replicas"],
    }
    if client.spec["root"] == "WORKER_OOM":
        worker = next(
            c for c in obj["spec"]["template"]["spec"]["containers"] if c["name"] == "worker"
        )
        result["memory_limit"] = worker["resources"]["limits"]["memory"]
    if client.spec["root"] == "REDIS_CONNECTIVITY":
        backend = client.request(
            "GET", "/apis/apps/v1/namespaces/video-platform/deployments/backend"
        )
        result["backend_uid"] = backend["metadata"]["uid"]
        result["redis_restored"] = all(
            next(
                e
                for e in next(
                    c for c in d["spec"]["template"]["spec"]["containers"] if c["name"] == name
                )["env"]
                if e["name"] == "REDIS_URL"
            ).get("value")
            == "redis://redis:6379/0"
            for d, name in [(obj, "worker"), (backend, "backend")]
        ) and ready(backend)
    return result


def valid(kind, params, action_id, result):
    sample = result.get("fresh_sample", {})
    if (
        sample.get("sample_status") != "COMPLETED"
        or sample.get("new_outcome_completed") is not True
        or sample.get("action_id") != action_id
        or sample.get("job_id") != params["job_id"]
        or sample.get("task_id") != str(uuid.uuid5(uuid.NAMESPACE_URL, "video-repair:" + action_id))
        or sample.get("output_codec") != {"h264": "h264", "h265": "hevc"}[params["video_codec"]]
    ):
        return False
    desired = result.get("desired_replicas", 1)
    if type(desired) is not int or desired < 1 or result.get("available_replicas") != desired:
        return False
    if kind == "restore_video_redis_and_retry":
        return result.get("redis_restored") is True
    if kind == "increase_video_worker_memory_and_retry":
        details = sample.get("details", {})
        before = details.get("memory_before", {})
        after = details.get("memory_after", {})
        return (
            result.get("memory_limit") == "2Gi"
            and before.get("memory_limit_bytes") == 2 * 1024**3
            and after.get("memory_limit_bytes") == 2 * 1024**3
            and isinstance(before.get("oom_kills"), int)
            and before["oom_kills"] == after.get("oom_kills")
        )
    return kind == "retry_video_ffmpeg_h264"
