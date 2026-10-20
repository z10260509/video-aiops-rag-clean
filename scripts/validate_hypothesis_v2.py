#!/usr/bin/env python3
"""Real local model/embedding, synthetic tools, temporary DB; never touch a live incident."""

import argparse
import json
import sys
import tempfile
import time
import uuid
from pathlib import Path
from datetime import timezone
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from langgraph.checkpoint.memory import InMemorySaver
from app.core.config import Settings
from app.database.session import Base
from app.aiops.hypotheses import WORKFLOW
from app.aiops.hypothesis_graph import HypothesisRuntime
from app.aiops.hypothesis_model import HypothesisModel
from app.aiops.embeddings import RemoteEmbedding
from app.aiops.knowledge import DocumentInput, ingest, search
from app.aiops.gateway import ToolResult
from app.aiops.graph_worker import execute
from app.aiops.incidents import now
from app.models.diagnostics import Incident, DiagnosisRun
from app.models.aiops import Diagnosis


def validate(url, output):
    settings = Settings(
        _env_file=None,
        aiops_model_provider="ollama",
        ollama_base_url=url,
        aiops_model_context_tokens=16384,
        aiops_model_timeout_seconds=180,
        knowledge_embedding_provider="ollama",
        knowledge_timeout_seconds=60,
        aiops_video_enabled=True,
        aiops_hypothesis_remediation_enabled=False,
        remediation_enabled=False,
    )
    with tempfile.TemporaryDirectory(prefix="hypothesis-validation-") as directory:
        engine = create_engine("sqlite:///" + directory + "/validation.db")
        Base.metadata.create_all(engine)
        factory = sessionmaker(engine)
        jid, tid, puid = [str(uuid.uuid4()) for _ in range(3)]
        model = HypothesisModel(settings)
        embedding = RemoteEmbedding(settings)
        with factory() as db:
            incident = Incident(
                correlation_key=f"video-job:{jid}:{tid}",
                symptom="VideoJobFailure",
                namespace="video-platform",
                resource_id="worker-synthetic",
                service="transcoder",
                lifecycle="INVESTIGATING",
                first_seen_at=now(),
                last_seen_at=now(),
            )
            db.add(incident)
            db.flush()
            run = DiagnosisRun(
                incident_id=incident.id, status="running", workflow_kind=WORKFLOW, started_at=now()
            )
            db.add(run)
            db.flush()
            claimed = (
                run.id,
                incident.id,
                run.started_at,
                incident.namespace,
                incident.resource_id,
            )
            for name in [
                "ffmpeg-invalid-codec.md",
                "worker-oom.md",
                "redis-connectivity.md",
                "ffmpeg-hypothesis-investigation.md",
            ]:
                ingest(
                    db,
                    DocumentInput(
                        filename=name,
                        content=(
                            Path(__file__).resolve().parents[1] / "runbooks" / name
                        ).read_text(),
                    ),
                    embedding,
                )
            db.commit()

        def synthetic(request):
            data = {"job_id": jid, "task_id": tid}
            if request.tool_name == "get_task_status":
                data.update(
                    job_present=True,
                    execution_status="failed",
                    error="Unknown encoder 'aiops_invalid_encoder'",
                    pod="worker-synthetic",
                    pod_uid=puid,
                    started_at=now().isoformat() + "Z",
                )
            elif request.tool_name == "get_pod_status":
                data = {
                    "uid": puid,
                    "phase": "Running",
                    "containers": [
                        {
                            "name": "worker",
                            "ready": True,
                            "state": {"running": {}},
                            "last_state": {},
                        }
                    ],
                }
            elif request.tool_name == "probe_video_input":
                data.update(readable=True, streams=[{"codec_name": "h264"}])
            elif request.tool_name == "get_encoder_inventory":
                data.update(
                    available=True,
                    known_encoders={
                        "libx264": True,
                        "libx265": True,
                        "aiops_invalid_encoder": False,
                    },
                    requested_encoder="aiops_invalid_encoder",
                    encoder_count=2,
                    scope="worker_execution_snapshot",
                )
            elif request.tool_name == "query_logs":
                data = {"lines": []}
            elif request.tool_name == "get_pod_events":
                data = {"events": []}
            else:
                data = {"series": []}
            source = (
                "video_task_api"
                if request.tool_name
                in ["get_task_status", "probe_video_input", "get_encoder_inventory"]
                else "loki"
                if request.tool_name == "query_logs"
                else "prometheus"
                if request.tool_name in ["query_metric", "get_queue_depth"]
                else "kubernetes"
            )
            stamp = now().replace(tzinfo=timezone.utc)
            return ToolResult(
                evidence_id=str(uuid.uuid4()),
                tool_name=request.tool_name,
                source=source,
                target=request.model_dump(mode="json"),
                requested_at=stamp,
                observed_at=stamp,
                data=data,
            )

        runtime = HypothesisRuntime(
            claimed,
            factory,
            SimpleNamespace(call=synthetic),
            model,
            lambda db, query: search(db, query, embedding),
            settings=settings,
        )
        started = time.monotonic()
        snapshot = execute(runtime.claimed, InMemorySaver(), runtime)
        with factory() as db:
            report = json.loads(db.get(Diagnosis, claimed[0]).payload_json)
        result = {
            "real_chat_model": model.model_id,
            "real_embedding_model": embedding.model_id,
            "synthetic_tool_observations": True,
            "retrieval_backend": "temporary SQLite cosine; production uses pgvector",
            "elapsed_seconds": round(time.monotonic() - started, 2),
            "validation_status": snapshot.values["validation_status"],
            "root_cause": report["root_cause_code"],
            "rounds": snapshot.values["round"],
            "tools_used": snapshot.values["tools_used"],
            "report": report,
            "mutation_executed": False,
            "actual_positive_approvals": 0,
            "live_incident_modified": False,
        }
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
        print(json.dumps({k: v for k, v in result.items() if k != "report"}, ensure_ascii=False))
        engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://localhost:11434")
    parser.add_argument(
        "--output", type=Path, default=Path("/tmp/aiops-hypothesis-v2-real-model.json")
    )
    args = parser.parse_args()
    validate(args.url, args.output)
