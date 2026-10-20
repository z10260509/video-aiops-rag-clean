#!/usr/bin/env python3
"""Upload an original video and check playback or a v2 review draft. Never approve repairs."""

import argparse
import base64
import json
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit
import httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.deploy_common import command, validate


class VideoClient:
    def __init__(self, base, token, transport=None):
        url = urlsplit(base)
        if (
            url.scheme != "http"
            or url.hostname not in {"localhost", "127.0.0.1", "::1"}
            or url.username
            or url.password
            or url.query
            or url.fragment
            or url.path not in {"", "/"}
        ):
            raise ValueError("Use the local dashboard HTTP port-forward URL")
        self.client = httpx.Client(
            base_url=base.rstrip("/"),
            headers={"Authorization": "Bearer " + token},
            timeout=30,
            follow_redirects=False,
            transport=transport,
        )

    def get(self, path):
        r = self.client.get(path)
        r.raise_for_status()
        return r.json()

    def upload(self, source, fault):
        with source.open("rb") as stream:
            r = self.client.post(
                "/video/api/jobs",
                files={"file": (source.name, stream, "video/mp4")},
                data={
                    "operation": "transcode",
                    "target_resolution": "480p",
                    "video_codec": "h264",
                    "compression_preset": "balanced",
                    "demonstrate_failure": str(fault).lower(),
                },
            )
        r.raise_for_status()
        return r.json()

    def playback(self, jid):
        r = self.client.get("/video/api/jobs/" + jid + "/video", headers={"Range": "bytes=0-31"})
        return r.status_code, len(r.content)

    def close(self):
        self.client.close()


def operator_token():
    validate()
    data = json.loads(
        command(
            "kubectl", "-n", "aiops", "get", "secret", "aiops-secrets", "-o", "json", capture=True
        )
    )
    return base64.b64decode(data["data"]["operator-token"]).decode()


def run(source, output, fault=False, api=None, base="http://127.0.0.1:13010", timeout=300):
    source, output = Path(source), Path(output)
    if not source.is_file() or source.suffix.lower() != ".mp4":
        raise ValueError("Readable MP4 source required")
    if not 1 <= timeout <= 600:
        raise ValueError("Timeout must be 1..600 seconds")
    if output.exists():
        raise ValueError("Output exists; preserve previous result and choose a new path")
    owned = api is None
    if owned:
        api = VideoClient(base, operator_token())
    result = {
        "workflow": "hypothesis-rag-v2",
        "scenario": "ffmpeg-invalid-codec" if fault else "healthy",
        "status": "running",
        "operator_approval_submitted": False,
        "repair_executed": False,
    }
    deadline = time.monotonic() + timeout
    try:
        job = api.upload(source, fault)
        result["job_id"] = job["id"]
        while time.monotonic() < deadline:
            job = api.get("/video/api/jobs/" + result["job_id"])
            result["job"] = job
            if not fault:
                if job["status"] == "FAILED":
                    raise RuntimeError("Healthy transcode failed")
                if job["status"] == "COMPLETED":
                    status, count = api.playback(job["id"])
                    result.update(playback_http=status, playback_bytes=count)
                    if status != 206 or count != 32:
                        raise RuntimeError("Playback range verification failed")
                    result["status"] = "passed"
                    return result
            else:
                linked = api.get("/aiops/api/video-jobs/" + result["job_id"])
                if linked["incidents"]:
                    iid = linked["incidents"][0]["id"]
                    result["incident_id"] = iid
                    incident = api.get("/aiops/api/incidents/" + iid)
                    result["incident"] = incident
                    try:
                        report = api.get("/aiops/api/incidents/" + iid + "/report")
                    except httpx.HTTPStatusError as exc:
                        if exc.response.status_code != 404:
                            raise
                        report = None
                    if report:
                        result["report"] = report
                        phase = next(
                            (r["status"] for r in incident["runs"] if r["id"] == report["run_id"]),
                            None,
                        )
                        if phase in {"waiting", "completed", "failed"}:
                            actions = api.get("/aiops/api/incidents/" + iid + "/actions")
                            result["actions"] = actions
                            result["model_id"] = report["model_id"]
                            workflow = next(
                                r["workflow_kind"]
                                for r in incident["runs"]
                                if r["id"] == report["run_id"]
                            )
                            if (
                                workflow != "aiops-hypothesis-v2"
                                or report["validation_status"] != "validated"
                                or report["diagnosis"]["root_cause_code"] != "FFMPEG_INVALID_CODEC"
                                or not report["model_id"]
                                or report["model_id"].startswith("fake")
                                or incident["lifecycle"] != "WAITING_APPROVAL"
                            ):
                                raise RuntimeError(
                                    "V2 diagnosis did not validate; inspect retained report"
                                )
                            pending = [
                                a
                                for a in actions["items"]
                                if a["run_id"] == report["run_id"]
                                and a["action_type"] == "retry_video_ffmpeg_h264"
                                and a["approval_status"] == "pending"
                                and a["execution_status"] == "pending"
                                and a["target"]["namespace"] == "video-platform"
                                and a["params"]["job_id"] == result["job_id"]
                            ]
                            if len(pending) != 1:
                                raise RuntimeError(
                                    "Expected one pending original-video repair draft"
                                )
                            if pending[0].get("result") is not None:
                                raise RuntimeError("Draft unexpectedly has execution result")
                            result["status"] = "passed"
                            return result
            time.sleep(1)
        raise TimeoutError("Video scenario timed out; result retained, no automatic retry")
    except Exception as exc:
        result.update(status="failed", error_type=type(exc).__name__)
        raise
    finally:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        if owned:
            api.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("--fault", action="store_true")
    parser.add_argument("--base", default="http://127.0.0.1:13010")
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = run(args.source, args.output, args.fault, base=args.base, timeout=args.timeout)
    print(
        json.dumps(
            {key: result[key] for key in ["scenario", "status", "job_id"]}, ensure_ascii=False
        )
    )
