"""Actual task observations, including interrupted attempts; never infer OOM from exit 137 alone."""

import json
import os
from pathlib import Path
from app.models.video_integration import VideoExecution, VideoRepair
from sqlalchemy import select


def memory_observation():
    try:
        limit = (Path("/sys/fs/cgroup") / "memory.max").read_text().strip()
        events = dict(
            line.split()
            for line in (Path("/sys/fs/cgroup") / "memory.events").read_text().splitlines()
        )
        return {
            "memory_limit_bytes": int(limit) if limit != "max" else None,
            "oom_kills": int(events["oom_kill"]),
        }
    except (OSError, ValueError, KeyError):
        return {}


def encoder_observation():
    """Capture the executing worker's own FFmpeg, not the backend's installation."""
    import subprocess
    from datetime import datetime, timezone

    try:
        result = subprocess.run(
            ["ffmpeg", "-hide_banner", "-encoders"],
            capture_output=True,
            text=True,
            check=True,
            timeout=3,
        )
        names = []
        for line in result.stdout.splitlines():
            parts = line.split()
            if len(parts) >= 2 and len(parts[0]) == 6 and parts[0][0] in "VAS" and parts[1] != "=":
                names.append(parts[1])
        if not names or len(names) > 512:
            return {"available": False}
        return {
            "available": True,
            "encoders": names,
            "observed_at": datetime.now(timezone.utc).isoformat(),
        }
    except (OSError, subprocess.SubprocessError):
        return {"available": False}


def begin_execution(db, job, task_id):
    repair = db.scalar(
        select(VideoRepair)
        .where(VideoRepair.job_id == job.id)
        .order_by(VideoRepair.created_at.desc())
        .limit(1)
    )
    if repair and task_id != repair.task_id:
        return False
    if db.get(VideoExecution, task_id):
        return False
    db.add(
        VideoExecution(
            task_id=task_id,
            job_id=job.id,
            status="running",
            started_at=job.started_at,
            pod=os.getenv("POD_NAME", os.getenv("HOSTNAME", "")),
            pod_uid=os.getenv("POD_UID", ""),
            details_json=json.dumps({"memory_before": memory_observation()}),
        )
    )
    return True


def finish_execution(db, task_id, job, error):
    row = db.get(VideoExecution, task_id)
    details = json.loads(row.details_json)
    details["memory_after"] = memory_observation()
    if error:
        details["encoder_inventory"] = encoder_observation()
    from app.services.logging import redact

    details["error"] = redact(job.error_message, 1200) if error else None
    row.status = "failed" if error else "completed"
    row.finished_at = job.completed_at
    if error and (
        "Unknown encoder" in job.error_message or "Encoder not found" in job.error_message
    ):
        details["error_kind"] = "FFMPEG_INVALID_CODEC"
    elif error and "Name or service not known" in job.error_message:
        details["error_kind"] = "REDIS_CONNECTIVITY"
    else:
        details["error_kind"] = "UNKNOWN" if error else None
    row.details_json = json.dumps(details)
