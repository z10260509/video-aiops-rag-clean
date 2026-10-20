from sqlalchemy import func, select
from prometheus_client import Counter, Gauge, Histogram

from app.models.job import Job, JobStatus

HTTP_REQUESTS = Counter(
    "video_api_http_requests_total",
    "HTTP requests handled by the video API.",
    ("method", "path", "status"),
)
HTTP_REQUEST_DURATION = Histogram(
    "video_api_http_request_duration_seconds",
    "HTTP request duration for the video API.",
    ("method", "path"),
)
JOB_CREATIONS = Counter(
    "video_jobs_created_total",
    "Video jobs accepted by the API.",
    ("codec", "compression_preset", "resolution"),
)
JOB_COUNT = Gauge("video_jobs_by_status", "Current number of video jobs by status.", ("status",))
MEDIA_BYTES = Gauge("video_media_bytes", "Total recorded input and output video bytes.", ("kind",))


def update_job_metrics(db) -> None:
    counts = dict(db.execute(select(Job.status, func.count()).group_by(Job.status)).all())
    for status in JobStatus:
        JOB_COUNT.labels(status=status.value).set(counts.get(status.value, 0))
    input_bytes, output_bytes = db.execute(
        select(
            func.coalesce(func.sum(Job.input_size_bytes), 0),
            func.coalesce(func.sum(Job.output_size_bytes), 0),
        )
    ).one()
    MEDIA_BYTES.labels(kind="input").set(input_bytes or 0)
    MEDIA_BYTES.labels(kind="output").set(output_bytes or 0)
