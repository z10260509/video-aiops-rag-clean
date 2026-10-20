"""Bounded JSON logs. Correlation IDs belong in log fields, never metric labels."""

import json
import logging
import os
import re
from datetime import datetime, timezone

_SECRETS = re.compile(
    r"""(?i)(["']?(?:password|passwd|token|secret|api[_-]?key|authorization)["']?\s*[:=]\s*["']?)(?:bearer\s+)?[^\s,;"']+"""
)
_BEARER = re.compile(r"(?i)\bbearer\s+[a-z0-9._~+/-]+=*")
_URL_AUTH = re.compile(r"(\w+://)[^\s/@]+:[^\s/@]+@")
_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")


def redact(value: object, limit: int = 4000) -> str:
    text = _URL_AUTH.sub(r"\1[REDACTED]@", str(value))
    text = _SECRETS.sub(r"\1[REDACTED]", text)
    text = _BEARER.sub("Bearer [REDACTED]", text)
    return _EMAIL.sub("[EMAIL]", text).encode("utf-8")[:limit].decode("utf-8", "ignore")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        data = {
            "timestamp": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "service": "video-worker",
            "logger": redact(record.name, 128),
            "message": redact(record.getMessage(), 1000),
            "namespace": redact(os.getenv("KUBERNETES_NAMESPACE", "local"), 128),
            "pod": redact(os.getenv("POD_NAME", os.getenv("HOSTNAME", "worker")), 128),
            "pod_uid": redact(os.getenv("POD_UID", ""), 128),
        }
        for key in ("event", "job_id", "task_id", "status", "error_kind", "error", "run_id"):
            if hasattr(record, key):
                data[key] = redact(getattr(record, key), 4000 if key == "error" else 128)
        # Do not append a raw traceback that could contain connection credentials.
        if record.exc_info and "error" not in data:
            data["error"] = redact(record.exc_info[1])
        output = json.dumps(data, ensure_ascii=False)
        if len(output.encode("utf-8")) > 8192:
            data = {key: value[:80] for key, value in data.items()}
            data["truncated"] = True
            output = json.dumps(data, ensure_ascii=False)
        return output


def configure_worker_logs(logger: logging.Logger) -> None:
    for handler in logger.handlers:
        handler.setFormatter(JsonFormatter())
