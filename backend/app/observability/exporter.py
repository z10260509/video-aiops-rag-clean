"""One publisher aggregates committed SQL outcomes and a fixed broker list."""

from datetime import timezone
from functools import lru_cache

import redis
from fastapi import FastAPI, Response
from prometheus_client import CollectorRegistry, CONTENT_TYPE_LATEST, generate_latest
from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily, HistogramMetricFamily
from sqlalchemy import case, func, select

from app.core.config import get_settings
from app.database.session import SessionLocal
from app.models.observability import VideoTaskOutcome as Outcome

BUCKETS = (0.1, 0.5, 1, 5, 10, 30, 60, 120, 300, 600)


class TaskCollector:
    def __init__(self, session_factory, queue_client, queue_name: str):
        if queue_name not in {"celery"}:
            raise ValueError("Queue is not allowlisted")
        self.sessions, self.redis, self.queue = session_factory, queue_client, queue_name

    def describe(self):
        return []  # No DB queries at registration/startup.

    def collect(self):
        health = GaugeMetricFamily(
            "video_exporter_source_up", "Source query health.", labels=["source"]
        )
        try:
            with self.sessions() as db:
                aggregates = db.execute(
                    select(
                        func.count(),
                        func.coalesce(func.sum(Outcome.duration_seconds), 0),
                        *[
                            func.coalesce(
                                func.sum(case((Outcome.duration_seconds <= b, 1), else_=0)), 0
                            )
                            for b in BUCKETS
                        ],
                        func.coalesce(
                            func.sum(case((Outcome.status == "completed", 1), else_=0)), 0
                        ),
                        func.coalesce(func.sum(case((Outcome.status == "failed", 1), else_=0)), 0),
                        func.max(
                            case((Outcome.status == "failed", Outcome.finished_at), else_=None)
                        ),
                    ).select_from(Outcome)
                ).one()
                counts = dict(zip(("completed", "failed"), aggregates[-3:-1]))
                failure_time = aggregates[-1]
            health.add_metric(["database"], 1)
            counter = CounterMetricFamily(
                "video_tasks",
                "Committed terminal executions, deduped by Celery task ID.",
                labels=["status"],
            )
            for status in ("completed", "failed"):
                counter.add_metric([status], counts.get(status, 0))
            yield counter
            histogram = HistogramMetricFamily(
                "video_task_duration_seconds", "Transcode plus thumbnail execution time."
            )
            histogram.add_metric(
                [],
                [(str(b), int(n)) for b, n in zip(BUCKETS, aggregates[2:])]
                + [("+Inf", aggregates[0])],
                aggregates[1],
            )
            yield histogram
            yield GaugeMetricFamily(
                "video_task_last_failure_timestamp_seconds",
                "UTC timestamp of latest committed failure.",
                value=failure_time.replace(tzinfo=timezone.utc).timestamp() if failure_time else 0,
            )
        except Exception:
            # No fake zero outcomes on a missing schema/DB; source health carries failure.
            health.add_metric(["database"], 0)
        try:
            depth = self.redis.llen(self.queue)
            health.add_metric(["redis"], 1)
            yield GaugeMetricFamily(
                "video_queue_depth", "Unclaimed tasks in the configured broker list.", value=depth
            )
        except Exception:
            health.add_metric(["redis"], 0)
        yield health


@lru_cache
def collector():
    settings = get_settings()
    client = redis.Redis.from_url(settings.redis_url, socket_connect_timeout=2, socket_timeout=2)
    return TaskCollector(SessionLocal, client, settings.video_metrics_queue)


app = FastAPI(title="Video task metrics exporter", docs_url=None, redoc_url=None)


@app.get("/healthz")
def health():
    return {"status": "ok"}


@app.get("/readyz")
def ready():
    health_samples = [
        s for m in collector().collect() if m.name == "video_exporter_source_up" for s in m.samples
    ]
    healthy = len(health_samples) == 2 and all(s.value == 1 for s in health_samples)
    return Response(
        "ready" if healthy else "sources unavailable", status_code=200 if healthy else 503
    )


@app.get("/metrics")
def metrics():
    registry = CollectorRegistry()
    registry.register(collector())
    return Response(generate_latest(registry), media_type=CONTENT_TYPE_LATEST)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=get_settings().video_metrics_port)
