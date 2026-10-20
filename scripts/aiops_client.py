#!/usr/bin/env python3
"""Local operator helper (analyze schedules a new diagnosis). Credentials stay inside the AIOps Pod."""

import argparse
import json
import subprocess
from uuid import UUID


def query(mode, incident_id=None, search_query=None, kind="runbook"):
    context = subprocess.check_output(["kubectl", "config", "current-context"], text=True).strip()
    if context != "kind-video-platform":
        raise ValueError("Expected kind-video-platform context")
    if mode not in {"list", "documents", "search"}:
        incident_id = str(UUID(incident_id))
    path = (
        "/api/incidents"
        if mode in {"list", "documents", "search"}
        else "/api/incidents/" + incident_id
    )
    if mode in {"timeline", "evidence", "report"}:
        path += "/" + mode
    if mode in {"documents", "search"}:
        path = "/api/knowledge/" + ("documents" if mode == "documents" else "search")
    payload = {"query": search_query, "kind": kind, "top_k": 3} if mode == "search" else None
    if mode == "analyze":
        path += "/analyze"
        payload = {}
    code = (
        """
import json,httpx
from app.core.config import get_settings
s=get_settings()
response=httpx.request('POST' if PAYLOAD is not None else 'GET','http://127.0.0.1:8010'+PATH,json=PAYLOAD,headers={'Authorization':'Bearer '+s.aiops_operator_token.get_secret_value(),'Idempotency-Key':REQUEST_KEY},timeout=45)
response.raise_for_status()
print(json.dumps(response.json(),ensure_ascii=False,indent=2))
""".replace("PATH", repr(path))
        .replace("PAYLOAD", repr(payload))
        .replace("REQUEST_KEY", repr(__import__("uuid").uuid4().hex))
    )
    result = subprocess.run(
        [
            "kubectl",
            "--request-timeout=15s",
            "-n",
            "aiops",
            "exec",
            "deployment/aiops-api",
            "-c",
            "api",
            "-i",
            "--",
            "python",
            "-",
        ],
        input=code,
        text=True,
        capture_output=True,
        timeout=60,
    )
    if result.returncode:
        raise RuntimeError("AIOps request failed")
    return json.loads(result.stdout)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "mode",
        choices=[
            "list",
            "show",
            "timeline",
            "evidence",
            "documents",
            "search",
            "report",
            "analyze",
        ],
    )
    parser.add_argument("incident_id", nargs="?")
    parser.add_argument("--query")
    parser.add_argument("--kind", choices=["runbook", "incident_history"], default="runbook")
    args = parser.parse_args()
    if args.mode == "search" and not args.query:
        parser.error("search requires --query")
    print(
        json.dumps(
            query(args.mode, args.incident_id, args.query, args.kind), ensure_ascii=False, indent=2
        )
    )
