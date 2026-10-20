"""Bounded GET-only evidence adapters with server-owned targets and query templates."""

import hashlib
import json
import re
import time
import uuid
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field

from app.core.config import get_settings
from app.aiops.kubernetes import kubernetes_connection
from app.models.aiops import Evidence
from app.services.logging import redact

SELECTORS = {"video-platform": "app=worker"}
TOOLS = Literal[
    "list_pods",
    "get_pod_status",
    "get_pod_events",
    "get_pod_logs",
    "query_metric",
    "query_logs",
    "get_task_status",
    "get_queue_depth",
    "probe_video_input",
    "get_encoder_inventory",
]


class ToolRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tool_name: TOOLS
    namespace: str = Field(pattern=r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
    pod: str | None = Field(default=None, pattern=r"^[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?$")
    lookback_seconds: int = Field(default=300, ge=1, le=900)
    tail_lines: int = Field(default=50, ge=1, le=100)
    metric_key: Literal[
        "failure_total", "queue_depth", "memory", "worker_available", "oom_kills"
    ] = "failure_total"
    query_key: Literal["worker_errors", "worker_tasks"] = "worker_errors"
    job_id: uuid.UUID | None = None
    task_id: uuid.UUID | None = None


class ToolResult(BaseModel):
    evidence_id: str
    tool_name: str
    requested_at: datetime
    observed_at: datetime
    target: dict
    source: str
    data: dict | list | None
    truncated: bool = False
    errors: list[str] = Field(default_factory=list)


class ToolFailure(Exception):
    def __init__(self, code, truncated=False):
        self.code, self.truncated = code, truncated


def sanitize(value):
    if isinstance(value, str):
        return redact(value, 4096)
    if isinstance(value, list):
        return [sanitize(x) for x in value[:100]]
    if isinstance(value, dict):
        return {
            str(k)[:128]: "[REDACTED]"
            if re.search(r"(?i)password|passwd|token|authorization|secret|api.?key", str(k))
            else sanitize(v)
            for k, v in list(value.items())[:100]
        }
    return value


def clipped(value):
    if isinstance(value, str):
        return len(value.encode()) > 4096
    if isinstance(value, list):
        return len(value) > 100 or any(clipped(x) for x in value)
    if isinstance(value, dict):
        return len(value) > 100 or any(clipped(v) for v in value.values())
    return False


class Gateway:
    def __init__(self, settings):
        self.settings = settings
        self.failures = {}

    def validate(self, request):
        allowed = {x.strip() for x in self.settings.aiops_allowed_namespaces.split(",")}
        if (
            not self.settings.aiops_tools_enabled
            or request.namespace not in allowed
            or request.namespace not in SELECTORS
        ):
            raise PermissionError("Namespace or tools outside allowlist")
        if request.pod and not request.pod.startswith("worker-"):
            raise PermissionError("Pod target outside allowlist")
        if (
            request.tool_name not in {"list_pods", "get_queue_depth", "get_task_status"}
            and not request.pod
        ):
            raise ValueError("Pod target required")
        if request.tool_name == "get_task_status" and not (request.task_id or request.job_id):
            raise ValueError("Task or job ID required")
        if request.tool_name in {"probe_video_input", "get_encoder_inventory"} and (
            request.namespace != "video-platform" or not request.job_id or not request.task_id
        ):
            raise ValueError("Original video job and task required for probe")
        if (
            request.namespace == "video-platform"
            and request.tool_name == "get_task_status"
            and not request.job_id
        ):
            raise ValueError("Original video job ID required")

    def fetch(self, source, path, params=None, json_body=True):
        connection = kubernetes_connection(self.settings) if source == "kubernetes" else None
        if source == "kubernetes":
            if not connection:
                raise ToolFailure("unconfigured_kubernetes")
            base, token, verify = connection
        else:
            base = (
                self.settings.prometheus_url
                if source == "prometheus"
                else self.settings.aiops_loki_url
            )
            token = self.settings.prometheus_bearer_token if source == "prometheus" else None
            verify = True
        if not base:
            raise ToolFailure("unconfigured_" + source)
        count, until = self.failures.get(source, (0, 0))
        if until > time.monotonic():
            raise ToolFailure("circuit_open")
        headers = {"Authorization": "Bearer " + token} if token else {}
        for attempt in range(2):
            try:
                with httpx.Client(
                    timeout=self.settings.aiops_tool_timeout_seconds,
                    verify=verify,
                    follow_redirects=False,
                ) as client:
                    with client.stream(
                        "GET", base.rstrip("/") + path, params=params, headers=headers
                    ) as response:
                        if response.status_code != 200:
                            raise ToolFailure("http_" + str(response.status_code))
                        body = bytearray()
                        for part in response.iter_bytes(chunk_size=4096):
                            body.extend(part)
                            if len(body) > 65536:
                                raise ToolFailure("response_limit", True)
                self.failures[source] = (0, 0)
                return json.loads(body) if json_body else body.decode("utf-8", "replace")
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                count += 1
                self.failures[source] = (count, time.monotonic() + 30 if count >= 3 else 0)
                if attempt == 0 and count < 3:
                    time.sleep(0.1)
                    continue
                raise ToolFailure(
                    "timeout" if isinstance(exc, httpx.TimeoutException) else "transport_error"
                ) from exc
            except (ValueError, UnicodeError) as exc:
                raise ToolFailure("invalid_response") from exc

    def pod(self, request):
        pod = self.fetch("kubernetes", f"/api/v1/namespaces/{request.namespace}/pods/{request.pod}")
        expected = "worker"
        if pod.get("metadata", {}).get("labels", {}).get("app") != expected:
            raise ToolFailure("target_label_mismatch")
        return pod

    def perform(self, r):
        if r.tool_name == "list_pods":
            obj = self.fetch(
                "kubernetes",
                f"/api/v1/namespaces/{r.namespace}/pods",
                {"labelSelector": SELECTORS[r.namespace], "limit": 20},
            )
            return {
                "pods": [
                    {
                        "name": x["metadata"]["name"],
                        "uid": x["metadata"]["uid"],
                        "phase": x.get("status", {}).get("phase"),
                    }
                    for x in obj.get("items", [])[:20]
                ]
            }, bool(obj.get("metadata", {}).get("continue"))
        if r.tool_name in {"get_pod_status", "get_pod_events", "get_pod_logs"}:
            pod = self.pod(r)
            meta, status = pod["metadata"], pod.get("status", {})
            if r.tool_name == "get_pod_status":
                return {
                    "name": meta["name"],
                    "uid": meta["uid"],
                    "phase": status.get("phase"),
                    "resources": {
                        c["name"]: c.get("resources", {})
                        for c in pod.get("spec", {}).get("containers", [])
                    },
                    "owners": meta.get("ownerReferences", []),
                    "conditions": status.get("conditions", []),
                    "containers": [
                        {
                            "name": x["name"],
                            "ready": x.get("ready"),
                            "restart_count": x.get("restartCount"),
                            "state": x.get("state"),
                            "last_state": x.get("lastState"),
                        }
                        for x in status.get("containerStatuses", [])
                    ],
                }, False
            if r.tool_name == "get_pod_events":
                obj = self.fetch(
                    "kubernetes",
                    f"/api/v1/namespaces/{r.namespace}/events",
                    {"fieldSelector": "involvedObject.uid=" + meta["uid"], "limit": 100},
                )
                start = datetime.now(timezone.utc) - timedelta(seconds=r.lookback_seconds)
                events = []
                for item in obj.get("items", []):
                    stamp = (
                        item.get("eventTime")
                        or item.get("lastTimestamp")
                        or item.get("metadata", {}).get("creationTimestamp")
                    )
                    try:
                        when = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
                    except (ValueError, AttributeError):
                        continue
                    if when >= start:
                        events.append(
                            {
                                "time": stamp,
                                "reason": item.get("reason"),
                                "message": item.get("message"),
                                "type": item.get("type"),
                            }
                        )
                events.sort(key=lambda e: e["time"])
                return {"pod_uid": meta["uid"], "events": events[-20:]}, len(events) > 20 or bool(
                    obj.get("metadata", {}).get("continue")
                )
            body = self.fetch(
                "kubernetes",
                f"/api/v1/namespaces/{r.namespace}/pods/{r.pod}/log",
                {
                    "container": "worker",
                    "sinceSeconds": r.lookback_seconds,
                    "tailLines": r.tail_lines,
                    "limitBytes": 8192,
                    "timestamps": "true",
                    "follow": "false",
                },
                False,
            )
            if "failed to create fsnotify watcher" in body:
                raise ToolFailure("log_stream_resource_unavailable")
            return {"pod_uid": meta["uid"], "lines": body.splitlines()[-r.tail_lines :]}, len(
                body.encode()
            ) >= 8192
        if r.tool_name in {"query_metric", "get_queue_depth"}:
            key = "queue_depth" if r.tool_name == "get_queue_depth" else r.metric_key
            templates = {
                "oom_kills": 'container_oom_events_total{namespace="NS",pod="POD",container="worker"}',
                "failure_total": 'video_tasks_total{namespace="NS",status="failed"}',
                "queue_depth": 'video_queue_depth{namespace="NS"}',
                "memory": 'container_memory_working_set_bytes{namespace="NS",pod="POD",container="worker"}',
                "worker_available": 'kube_deployment_status_replicas_available{namespace="NS",deployment="DEPLOYMENT"}',
            }
            query = (
                templates[key]
                .replace("NS", r.namespace)
                .replace("POD", r.pod or "")
                .replace("DEPLOYMENT", "worker")
            )
            end = datetime.now(timezone.utc).timestamp()
            obj = self.fetch(
                "prometheus",
                "/api/v1/query_range",
                {"query": query, "start": end - r.lookback_seconds, "end": end, "step": 15},
            )
            if obj.get("status") != "success":
                raise ToolFailure("prometheus_query_failed")
            rows = obj.get("data", {}).get("result", [])
            return {"metric_key": key, "query": query, "series": rows[:20]}, len(rows) > 20
        if r.tool_name == "query_logs":
            app = "worker"
            query = (
                '{namespace="'
                + r.namespace
                + '",app="'
                + app
                + '",container="worker",pod="'
                + r.pod
                + '"}'
            )
            query += (
                ' |= "processing_failed"' if r.query_key == "worker_errors" else ' |= "processing_"'
            )
            if r.job_id:
                query += ' |= "' + str(r.job_id) + '"'
            end = time.time_ns()
            obj = self.fetch(
                "loki",
                "/loki/api/v1/query_range",
                {
                    "query": query,
                    "start": str(end - r.lookback_seconds * 10**9),
                    "end": str(end),
                    "limit": r.tail_lines,
                    "direction": "backward",
                },
            )
            if obj.get("status") != "success":
                raise ToolFailure("loki_query_failed")
            streams = obj.get("data", {}).get("result", [])[:20]
            lines = [
                {"timestamp_ns": at, "line": line}
                for s in streams
                for at, line in s.get("values", [])
            ][: r.tail_lines]
            return {"query_key": r.query_key, "query": query, "lines": lines}, len(
                lines
            ) >= r.tail_lines
        if r.tool_name in {"probe_video_input", "get_encoder_inventory"}:
            from app.aiops.video_bridge import source

            probe = "input" if r.tool_name == "probe_video_input" else "encoders"
            return source(
                "/jobs/" + str(r.job_id) + "/probe/" + probe + "?task_id=" + str(r.task_id),
                self.settings,
            ), False
        if r.tool_name == "get_task_status":
            from app.aiops.video_bridge import source

            path = "/jobs/" + str(r.job_id) + ("?task_id=" + str(r.task_id) if r.task_id else "")
            return source(path, self.settings), False
        raise ValueError("Tool not implemented")

    def call(self, request):
        self.validate(request)
        at = datetime.now(timezone.utc)
        sources = {
            "query_metric": "prometheus",
            "get_queue_depth": "prometheus",
            "query_logs": "loki",
            "get_task_status": "video_task_api",
        }
        if request.namespace == "video-platform":
            for name in ["get_task_status", "probe_video_input", "get_encoder_inventory"]:
                sources[name] = "video_task_api"
        result = ToolResult(
            evidence_id=str(uuid.uuid4()),
            tool_name=request.tool_name,
            requested_at=at,
            observed_at=at,
            target=request.model_dump(mode="json"),
            source=sources.get(request.tool_name, "kubernetes"),
            data=None,
        )
        try:
            data, truncated = self.perform(request)
            truncated = truncated or clipped(data)
            data = sanitize(data)
            encoded = json.dumps(data, ensure_ascii=False).encode()
            if len(encoded) > 16384:
                data = {"excerpt": redact(encoded.decode(), 12000)}
                truncated = True
            result.data, result.truncated = data, truncated
        except ToolFailure as exc:
            result.errors = [exc.code]
            result.truncated = exc.truncated
        except Exception as exc:
            result.errors = [type(exc).__name__]
        result.observed_at = datetime.now(timezone.utc)
        from app.aiops.metrics import TOOLS, TRUNCATED

        TOOLS.labels(request.tool_name, "error" if result.errors else "ok").inc()
        if result.truncated:
            TRUNCATED.labels(request.tool_name).inc()
        return result


def save(db, incident_id, result, run_id=None):
    if run_id:
        result.evidence_id = str(
            uuid.uuid5(uuid.NAMESPACE_URL, run_id + json.dumps(result.target, sort_keys=True))
        )
    raw = json.dumps(result.data, ensure_ascii=False, sort_keys=True)
    row = db.get(Evidence, result.evidence_id)
    if row:
        # Evidence is immutable; replay refers to the previously committed observation.
        return ToolResult(
            evidence_id=row.id,
            tool_name=row.kind,
            source=row.source,
            requested_at=row.requested_at.replace(tzinfo=timezone.utc),
            observed_at=row.observed_at.replace(tzinfo=timezone.utc),
            target=json.loads(row.query_json),
            data=json.loads(row.payload_json),
            truncated=row.truncated,
            errors=json.loads(row.errors_json),
        )
    values = dict(
        incident_id=incident_id,
        run_id=run_id,
        kind=result.tool_name,
        source=result.source,
        requested_at=result.requested_at.replace(tzinfo=None),
        observed_at=result.observed_at.replace(tzinfo=None),
        query_json=json.dumps(result.target),
        payload_json=raw,
        checksum=hashlib.sha256(raw.encode()).hexdigest(),
        truncated=result.truncated,
        errors_json=json.dumps(result.errors),
    )
    db.add(Evidence(id=result.evidence_id, **values))
    db.flush()
    return result


@lru_cache
def gateway():
    return Gateway(get_settings())
