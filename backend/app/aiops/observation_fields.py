"""Bounded factual field catalog. No diagnosis categories or repair decisions."""

import json
import hashlib
import math
import re
from collections import deque


def normalize_logs(data, namespace, pod, link=None):
    data = dict(data or {})
    parsed = []
    for index, item in enumerate(data.get("lines", [])[:100]):
        try:
            value = json.loads(item["line"])
            if not isinstance(value, dict):
                continue
            if value.get("namespace") != namespace or value.get("pod") != pod:
                continue
            if link and any(value.get(key) != expected for key, expected in link.items()):
                continue
        except (KeyError, TypeError, ValueError):
            continue
        # Keep raw lines unchanged. Never trust an upstream 'parsed_lines' field.
        parsed.append(value)
    data["parsed_lines"] = parsed
    return data


def scalar_fields(data):
    queue = deque([("", data)])
    fields = []
    visited = 0
    while queue and len(fields) < 48 and visited < 500:
        path, value = queue.popleft()
        visited += 1
        if len(path) > 180:
            continue
        if type(value) in (str, bool, int, float) and path:
            if isinstance(value, float) and not math.isfinite(value):
                continue
            kind = (
                "boolean"
                if type(value) is bool
                else "number"
                if type(value) in (int, float)
                else "string"
            )
            truncated = isinstance(value, str) and len(value) > 400
            fields.append(
                {
                    "path": path,
                    "value_type": kind,
                    "value": value[:400] if isinstance(value, str) else value,
                    "value_truncated": truncated,
                    "operators": ["contains"]
                    if truncated
                    else ["equals", "contains"]
                    if kind == "string"
                    else ["equals", "greater_than"]
                    if kind == "number"
                    else ["equals"],
                }
            )
        elif isinstance(value, dict):
            # Structured log fields precede their raw string copies; all other
            # dictionary fields retain their original order and original paths.
            keys = sorted(value, key=lambda k: k != "parsed_lines")[:40]
            queue.extend(
                (path + "/" + key, value[key])
                for key in keys
                if isinstance(key, str) and re.fullmatch(r"[a-zA-Z0-9_-]+", key)
            )
        elif isinstance(value, list):
            queue.extend((path + "/" + str(i), item) for i, item in enumerate(value[:4]))
    return fields


def field_catalog(observations, evidence_map=None):
    rows = []
    lookup = {}
    size = 0
    candidates = [
        (
            row,
            scalar_fields(row.get("data"))
            if not row.get("errors") and not row.get("truncated")
            else [],
        )
        for row in observations
    ]
    # Round robin keeps a large Pod response from hiding later tool observations.
    for index in range(48):
        for observation, fields in candidates:
            if index >= len(fields):
                continue
            field = fields[index]
            # Round-robin position changes when tools add observations. IDs must
            # never silently rebind a previous claim to a different scalar.
            identity = (evidence_map or {}).get(
                observation["evidence_id"], observation["evidence_id"]
            )
            key = json.dumps([identity, field["path"], field["value_type"]], separators=(",", ":"))
            suffix = hashlib.sha256(key.encode()).hexdigest()[:12]
            fid = "F_" + field["path"].rsplit("/", 1)[-1][:20] + "_" + suffix
            entry = {
                "field_id": fid,
                "evidence_id": observation["evidence_id"],
                "tool_name": observation["kind"],
                **field,
            }
            length = len(json.dumps(entry, ensure_ascii=False).encode())
            if size + length > 14000 or len(rows) >= 96:
                continue
            if fid in lookup:
                raise ValueError("Duplicate or colliding observation field identity")
            rows.append(entry)
            size += length
            lookup[fid] = {
                "evidence_id": observation["evidence_id"],
                "path": field["path"],
                "value_type": field["value_type"],
                "operators": field["operators"],
            }
    return rows, lookup
