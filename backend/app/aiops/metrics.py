"""Process-local low-cardinality telemetry; scrape each API/graph instance separately."""

from prometheus_client import Counter, Histogram

REQUESTS = Counter("aiops_http_requests_total", "API requests", ["method", "status"])
TOOLS = Counter("aiops_tool_calls_total", "Bounded tool results", ["tool", "outcome"])
TRUNCATED = Counter("aiops_evidence_truncated_total", "Truncated tool results", ["tool"])
MODELS = Histogram("aiops_model_duration_seconds", "Diagnosis model duration", ["provider"])
ANALYSIS = Histogram("aiops_analysis_duration_seconds", "Graph invocation duration")


class IncidentCollector:
    def collect(self):
        from prometheus_client.core import GaugeMetricFamily
        from sqlalchemy import select, func
        from app.database.session import SessionLocal
        from app.models.diagnostics import Incident
        from app.core.config import get_settings

        metric = GaugeMetricFamily(
            "aiops_incidents", "Persisted incident counts", labels=["severity", "status"]
        )
        with SessionLocal() as db:
            namespaces = get_settings().aiops_allowed_namespaces.split(",")
            rows = db.execute(
                select(Incident.severity, Incident.lifecycle, func.count())
                .where(Incident.namespace.in_(namespaces))
                .group_by(Incident.severity, Incident.lifecycle)
            ).all()
            counts = {}
            states = {
                "DETECTED",
                "INVESTIGATING",
                "DIAGNOSED",
                "WAITING_APPROVAL",
                "REMEDIATING",
                "VERIFYING",
                "RESOLVED",
                "RESOLVED_NO_ACTION",
                "ESCALATED",
                "REJECTED",
                "CANCELLED",
                "FAILED",
            }
            for severity, status, count in rows:
                labels = (
                    severity if severity in {"info", "warning", "critical"} else "other",
                    status if status in states else "other",
                )
                counts[labels] = counts.get(labels, 0) + count
            for labels, count in counts.items():
                metric.add_metric(list(labels), count)
        yield metric
