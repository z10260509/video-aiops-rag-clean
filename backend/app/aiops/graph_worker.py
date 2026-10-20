"""Single-run checkpoint resume with session advisory lock and bounded graph steps."""

import logging
import json
import time
from datetime import timedelta

from sqlalchemy import select, text

from app.aiops.checkpoints import saver
from app.aiops.lifecycle import thread_id
from app.aiops.hypothesis_graph import HypothesisRuntime, build_hypothesis
from app.aiops.hypotheses import WORKFLOW
from app.aiops.incidents import now, transition
from app.core.config import get_settings
from app.database.session import SessionLocal, engine
from app.models.diagnostics import DiagnosisRun, Incident
from app.models.aiops import Action


def claim(factory=SessionLocal):
    with factory() as db:
        for action in db.scalars(
            select(Action)
            .join(DiagnosisRun, Action.run_id == DiagnosisRun.id)
            .where(
                DiagnosisRun.workflow_kind == WORKFLOW,
                (Action.approval_status == "pending") | (Action.approval_status == "approved"),
            )
        ):
            row = db.get(DiagnosisRun, action.run_id)
            if action.execution_status == "queued" and action.expires_at <= now():
                action.execution_status = "denied"
                action.finished_at = now()
                action.result_json = json.dumps(
                    {
                        "reason": "Approval expired before executor claim",
                        "mutation_attempted": False,
                    }
                )
            if (
                action.execution_status == "running"
                and action.started_at
                and action.started_at < now() - timedelta(minutes=5)
            ):
                action.execution_status = "uncertain"
                action.finished_at = now()
                action.result_json = json.dumps(
                    {
                        "reason": "Execution lease expired; manual inspection required; no automatic retry"
                    }
                )
            if row.status == "waiting" and (
                (action.expires_at <= now() and action.execution_status == "pending")
                or action.execution_status in {"succeeded", "uncertain", "denied"}
            ):
                row.status = "pending"
                if action.approval_status == "pending":
                    action.approval_status = "expired"
        db.commit()
        run = db.scalar(
            select(DiagnosisRun)
            .where(
                DiagnosisRun.workflow_kind == WORKFLOW,
                (DiagnosisRun.status == "pending")
                | (
                    (DiagnosisRun.status == "running")
                    & (DiagnosisRun.started_at < now() - timedelta(minutes=5))
                ),
            )
            .order_by(DiagnosisRun.id)
            .with_for_update(skip_locked=True)
            .limit(1)
        )
        if not run:
            return None
        incident = db.get(Incident, run.incident_id, with_for_update=True)
        if incident.lifecycle in {"CANCELLED", "REJECTED", "FAILED"}:
            run.status = "cancelled"
            run.finished_at = now()
            db.commit()
            return None
        run.status = "running"
        run.started_at = now()
        run.error = None
        if incident.lifecycle == "DETECTED":
            transition(
                db, incident, "INVESTIGATING", "graph-worker", "Checkpointed read-only diagnosis"
            )
        db.commit()
        return run.id, incident.id, run.started_at, incident.namespace, incident.resource_id


def execute(claimed, checkpointer=None, runtime=None, interrupt_after=None):
    from app.aiops.metrics import ANALYSIS

    with ANALYSIS.time():
        return _execute(claimed, checkpointer, runtime, interrupt_after)


def _execute(claimed, checkpointer=None, runtime=None, interrupt_after=None):
    if runtime is None:
        with SessionLocal() as db:
            if db.get(DiagnosisRun, claimed[0]).workflow_kind != WORKFLOW:
                raise ValueError("Historical workflow cannot resume; create a new diagnosis")
        runtime = HypothesisRuntime(claimed)
    if getattr(runtime, "workflow_kind", None) != WORKFLOW:
        raise ValueError("Only hypothesis v2 can execute")
    graph = build_hypothesis(runtime, checkpointer, interrupt_after)
    config = {
        "configurable": {"thread_id": thread_id(claimed[1], claimed[0])},
        "recursion_limit": 40,
    }
    snapshot = graph.get_state(config)
    if snapshot.values:
        if snapshot.next:
            if snapshot.next[0] in {"approval_interrupt", "execute_approved_action"}:
                from langgraph.types import Command

                graph.invoke(Command(resume="read-persisted-authorization"), config)
            else:
                graph.invoke(None, config)
        else:
            runtime.finalize(snapshot.values)
    else:
        graph.invoke(
            {
                "run_id": claimed[0],
                "incident_id": claimed[1],
                "namespace": claimed[3],
                "resource_name": claimed[4],
            },
            config,
        )
    final = graph.get_state(config)
    if final.next and final.next[0] in {"approval_interrupt", "execute_approved_action"}:
        with runtime.factory() as db:
            row = db.get(DiagnosisRun, claimed[0])
            row.status = "waiting"
            db.commit()
    return final


def run():
    if not get_settings().aiops_graph_enabled:
        raise RuntimeError("Graph disabled")
    from prometheus_client import start_http_server

    start_http_server(8011)
    import signal, threading

    stopping = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopping.set())
    signal.signal(signal.SIGINT, lambda *_: stopping.set())
    while not stopping.is_set():
        if get_settings().aiops_video_enabled:
            try:
                from app.aiops.video_bridge import discover
                from app.aiops.gateway import gateway

                discover(SessionLocal, gateway())
            except Exception as exc:
                logging.warning("Video source scan unavailable: %s", type(exc).__name__)
        claimed = claim()
        if not claimed:
            time.sleep(2)
            continue
        try:
            # Lock is independent of SQL transactions and held across all graph node commits.
            with engine.connect() as lock:
                key = int(claimed[0].replace("-", "")[:15], 16)
                if not lock.execute(
                    text("SELECT pg_try_advisory_lock(:key)"), {"key": key}
                ).scalar():
                    continue
                try:
                    with saver() as checkpointer:
                        execute(claimed, checkpointer)
                finally:
                    lock.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": key})
        except Exception as exc:
            logging.error("Graph failed: %s", type(exc).__name__)
            with SessionLocal() as db:
                row = db.get(DiagnosisRun, claimed[0], with_for_update=True)
                if row.status == "running":
                    row.status = "failed"
                    row.error = type(exc).__name__
                    row.finished_at = now()
                    db.commit()


if __name__ == "__main__":
    run()
