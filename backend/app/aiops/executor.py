"""Bound v2 video executor; uncertain mutations are never automatically replayed."""

import json
import time
from datetime import timedelta
import httpx
from sqlalchemy import select
from app.aiops.actions import SPECS, spec_for, policy, pod_allowed
from app.aiops.kubernetes import kubernetes_connection
from app.core.config import get_settings
from app.database.session import SessionLocal
from app.aiops.incidents import now
from app.models.aiops import Action


class KubernetesActionClient:
    def __init__(self, settings=None, transport=None):
        settings = settings or get_settings()
        connection = kubernetes_connection(settings)
        if not connection:
            raise PermissionError("In-cluster identity required")
        self.base, self.token, verify = connection
        self.client = httpx.Client(
            timeout=3, verify=verify, follow_redirects=False, transport=transport
        )
        self.settings = settings
        self.configure("retry_video_ffmpeg_h264")

    def configure(self, action_type, action=None):
        self.kind = action_type
        self.spec = spec_for(action_type)
        self.deployment_name = self.spec["deployment"]
        self.namespace = self.spec["namespace"]
        self.path = f"/apis/apps/v1/namespaces/{self.namespace}/deployments/{self.deployment_name}"
        self.action_id = action.id if action else None
        self.params = json.loads(action.params_json) if action else {}
        self.target = json.loads(action.target_json) if action else {}

    def request(self, method, path, body=None):
        headers = {"Authorization": "Bearer " + self.token}
        if method == "PATCH":
            headers["Content-Type"] = "application/json-patch+json"
        with self.client.stream(method, self.base + path, json=body, headers=headers) as r:
            if r.status_code != 200:
                raise ValueError("Kubernetes request rejected: " + str(r.status_code))
            data = bytearray()
            for part in r.iter_bytes(chunk_size=4096):
                data.extend(part)
                if len(data) > 65536:
                    raise ValueError("Kubernetes response too large")
        return json.loads(data)

    def deployment(self):
        return self.request("GET", self.path)

    def validate_pod(self, pod, uid):
        if not pod_allowed(pod, self.deployment_name):
            raise PermissionError("Pod outside target")
        obj = self.request("GET", f"/api/v1/namespaces/{self.namespace}/pods/{pod}")
        label = "aiops.video-platform/repair-enabled"
        if obj["metadata"].get("labels", {}).get(label) != "true":
            raise PermissionError("Pod repair label missing")
        owner = next(
            (o for o in obj["metadata"].get("ownerReferences", []) if o.get("controller")), None
        )
        if (
            not owner
            or owner["kind"] != "ReplicaSet"
            or not owner["name"].startswith(self.deployment_name + "-")
        ):
            raise PermissionError("Pod owner mismatch")
        rs = self.request(
            "GET", f"/apis/apps/v1/namespaces/{self.namespace}/replicasets/" + owner["name"]
        )
        if rs["metadata"]["uid"] != owner["uid"] or not any(
            o.get("controller")
            and o["kind"] == "Deployment"
            and o["uid"] == uid
            and o["name"] == self.deployment_name
            for o in rs["metadata"].get("ownerReferences", [])
        ):
            raise PermissionError("Deployment owner mismatch")
        return obj

    def preflight(self):
        from app.aiops.video_executor import preflight

        return preflight(self)

    def patch(self, before):
        from app.aiops.video_executor import patch

        return patch(self, before)

    def verify(self, read_only=False):
        from app.aiops.video_executor import verify

        return verify(self, read_only=read_only)


def recovery_valid_values(kind, params, action_id, result):
    if kind not in SPECS:
        return False
    from app.aiops.video_executor import valid

    return valid(kind, params, action_id, result)


def recovery_valid(action, result):
    if action.action_type == "restore_video_redis_and_retry" and result.get(
        "backend_uid"
    ) != json.loads(action.target_json).get("backend", {}).get("uid"):
        return False
    return result.get("deployment_uid") == json.loads(action.target_json)[
        "uid"
    ] and recovery_valid_values(
        action.action_type, json.loads(action.params_json), action.id, result
    )


def execute(action_id, client, factory=SessionLocal, settings=None):
    settings = settings or get_settings()
    with factory() as db:
        action = db.get(Action, action_id, with_for_update=True)
        if not action or action.execution_status != "queued":
            return {"attempted": False}
        try:
            before = policy(db, action, client, settings, require_approval=True)
        except (PermissionError, ValueError, httpx.HTTPError) as exc:
            action.execution_status = "denied"
            action.finished_at = now()
            action.result_json = json.dumps(
                {
                    "reason": str(exc)
                    if isinstance(exc, PermissionError)
                    else "Policy source unavailable",
                    "mutation_attempted": False,
                }
            )
            db.commit()
            return {"attempted": False, "status": "denied"}
        from types import SimpleNamespace

        proof = SimpleNamespace(
            id=action.id,
            action_type=action.action_type,
            params_json=action.params_json,
            target_json=action.target_json,
        )
        action.execution_status = "running"
        action.started_at = now()
        db.commit()
    result = {
        "mutation_attempted": True,
        "before": {
            "uid": before["metadata"]["uid"],
            "resource_version": before["metadata"]["resourceVersion"],
        },
    }
    try:
        after = client.patch(before)
        result["after"] = {
            "uid": after["metadata"]["uid"],
            "resource_version": after["metadata"]["resourceVersion"],
        }
        deadline = time.monotonic() + 180
        while True:
            try:
                result["verification"] = client.verify()
                if not recovery_valid(proof, result["verification"]):
                    raise ValueError("Recovery proof does not match action")
                break
            except (ValueError, httpx.HTTPError):
                if time.monotonic() >= deadline:
                    raise ValueError("Recovery not verified within deadline")
                time.sleep(2)
        status = "succeeded"
    except Exception as exc:
        status = "uncertain"
        result["error_type"] = type(exc).__name__
        result["manual_fallback"] = (
            "Inspect the bound target and recorded task/result; request a new reviewed action if needed. No automatic retry or rollback."
        )
    with factory() as db:
        action = db.get(Action, action_id, with_for_update=True)
        action.execution_status = status
        action.result_json = json.dumps(result)
        action.finished_at = now()
        db.commit()
    return {"attempted": True, "status": status}


def run():
    if not get_settings().remediation_enabled:
        raise RuntimeError("Executor disabled; no mutation")
    client = KubernetesActionClient()
    while True:
        with SessionLocal() as db:
            stale = [
                row
                for row in db.scalars(
                    select(Action).where(
                        Action.execution_status == "running", Action.action_type.in_(SPECS)
                    )
                )
                if row.started_at < now() - timedelta(minutes=5)
            ]
            for row in stale:
                row.execution_status = "uncertain"
                row.finished_at = now()
                row.result_json = json.dumps(
                    {"error": "Interrupted execution; do not retry mutation automatically"}
                )
            db.commit()
            row = db.scalar(
                select(Action)
                .where(
                    Action.approval_status == "approved",
                    Action.execution_status == "queued",
                    Action.action_type.in_(SPECS),
                )
                .order_by(Action.created_at)
                .limit(1)
            )
            aid = row.id if row else None
        if aid:
            execute(aid, client)
        else:
            time.sleep(2)


if __name__ == "__main__":
    run()
