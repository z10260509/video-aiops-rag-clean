import json
from uuid import UUID, uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.exceptions import RequestValidationError
from starlette.responses import JSONResponse
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.aiops.auth import operator, webhook_sender
from app.aiops.gateway import ToolRequest, Gateway, gateway, save
from app.aiops.incidents import digest, receive, schedule, transition, timeline, now
from app.aiops.schemas import TransitionInput, Webhook
from app.core.config import Settings, get_settings
from app.database.session import get_db
from app.models.aiops import Alert, Evidence, Timeline
from app.models.diagnostics import DiagnosisRun, Incident, KBDocument
from app.aiops.knowledge import DocumentInput, SearchInput, ingest, search


class BodyLimit:
    def __init__(self, app, limit=131072):
        self.app, self.limit = app, limit

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] not in {"POST", "PUT", "PATCH"}:
            return await self.app(scope, receive, send)
        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body.extend(message.get("body", b""))
            if len(body) > self.limit:
                return await JSONResponse({"detail": "Request body too large"}, status_code=413)(
                    scope, receive, send
                )
            if not message.get("more_body"):
                break
        consumed = False

        async def replay():
            nonlocal consumed
            if not consumed:
                consumed = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            return await receive()

        await self.app(scope, replay, send)


app = FastAPI(title="AIOps Incident and Evidence API")
app.add_middleware(BodyLimit)


@app.exception_handler(RequestValidationError)
async def invalid_request(request, exc):
    return JSONResponse(
        {"detail": [{"loc": e["loc"], "type": e["type"]} for e in exc.errors()]}, status_code=422
    )


def allowed(settings):
    return {x.strip() for x in settings.aiops_allowed_namespaces.split(",") if x.strip()}


def visible_incident():
    return (
        ~select(Timeline.id)
        .where(Timeline.incident_id == Incident.id, Timeline.event_type == "incident_deleted")
        .exists()
    )


def owned(db, incident_id, settings, lock=False):
    incident = db.get(Incident, str(incident_id), with_for_update=lock)
    if (
        not incident
        or incident.namespace not in allowed(settings)
        or db.scalar(
            select(Timeline.id)
            .where(
                Timeline.incident_id == str(incident_id), Timeline.event_type == "incident_deleted"
            )
            .limit(1)
        )
    ):
        raise HTTPException(404, "Incident not found")
    return incident


def incident_json(row):
    data = {
        key: getattr(row, key)
        for key in [
            "id",
            "namespace",
            "resource_kind",
            "resource_id",
            "symptom",
            "severity",
            "lifecycle",
            "status",
            "first_seen_at",
            "last_seen_at",
            "resolved_at",
            "version",
        ]
    }
    from app.aiops.video_bridge import linked_job

    data["video_job"] = linked_job(row)
    return data


@app.get("/api/video-jobs/{job_id}")
def video_job(
    job_id: UUID,
    principal=Depends(operator),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    from app.aiops.video_bridge import source

    try:
        job = source("/jobs/" + str(job_id), settings)
    except (ValueError, PermissionError):
        raise HTTPException(503, "影片服務尚未接通")
    rows = db.scalars(
        select(Incident)
        .where(
            Incident.namespace == "video-platform",
            Incident.correlation_key.startswith("video-job:" + str(job_id) + ":"),
            visible_incident(),
        )
        .order_by(Incident.first_seen_at.desc())
    ).all()
    return {"job": job, "incidents": [incident_json(r) for r in rows]}


@app.get("/healthz")
def health():
    return {"status": "ok"}


@app.get("/readyz")
def ready(settings: Settings = Depends(get_settings), db: Session = Depends(get_db)):
    if (
        not settings.aiops_enabled
        or min(
            len(settings.aiops_operator_token.get_secret_value()),
            len(settings.aiops_webhook_token.get_secret_value()),
        )
        < 32
    ):
        raise HTTPException(503, "Authentication not configured")
    db.execute(text("SELECT 1 FROM aiops_alerts LIMIT 1"))
    if settings.knowledge_vectors_enabled:
        db.execute(text("SELECT 1 FROM aiops_kb_vectors LIMIT 1"))
    return {"status": "ready"}


@app.post("/api/alerts/webhook", status_code=202)
def alerts(
    payload: Webhook,
    sender=Depends(webhook_sender),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    try:
        return {"accepted": receive(db, payload, settings.aiops_allowed_namespaces)}
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except ValueError as exc:
        db.rollback()
        raise HTTPException(409, str(exc)) from exc


@app.get("/api/incidents")
def list_incidents(
    namespace: str | None = None,
    status: str | None = None,
    severity: str | None = None,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0, le=10000),
    principal=Depends(operator),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    if namespace and namespace not in allowed(settings):
        raise HTTPException(403, "Namespace outside allowlist")
    query = select(Incident).where(Incident.namespace.in_(allowed(settings)), visible_incident())
    for column, value in [
        (Incident.namespace, namespace),
        (Incident.lifecycle, status),
        (Incident.severity, severity),
    ]:
        if value:
            query = query.where(column == value)
    return {
        "items": [
            incident_json(x)
            for x in db.scalars(
                query.order_by(Incident.last_seen_at.desc(), Incident.id)
                .offset(offset)
                .limit(limit)
            )
        ],
        "offset": offset,
        "limit": limit,
    }


@app.get("/api/incidents/{incident_id}")
def detail(
    incident_id: UUID,
    principal=Depends(operator),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    incident = owned(db, incident_id, settings)
    payload = incident_json(incident)
    payload["alerts"] = [
        {
            "id": a.id,
            "name": a.name,
            "status": a.status,
            "fingerprint": a.fingerprint,
            "labels": json.loads(a.labels_json),
            "annotations": json.loads(a.annotations_json),
            "starts_at": a.starts_at,
            "ends_at": a.ends_at,
        }
        for a in db.scalars(select(Alert).where(Alert.incident_id == incident.id))
    ]
    payload["runs"] = [
        {"id": r.id, "status": r.status, "workflow_kind": r.workflow_kind, "error": r.error}
        for r in db.scalars(select(DiagnosisRun).where(DiagnosisRun.incident_id == incident.id))
    ]
    return payload


def history_deletable(db, incident):
    from app.models.aiops import Action

    if incident.status != "closed":
        return False
    active_run = db.scalar(
        select(DiagnosisRun.id)
        .where(
            DiagnosisRun.incident_id == incident.id,
            DiagnosisRun.status.in_(["pending", "running", "waiting"]),
        )
        .limit(1)
    )
    active_action = db.scalar(
        select(Action.id)
        .where(
            Action.incident_id == incident.id, Action.execution_status.in_(["queued", "running"])
        )
        .limit(1)
    )
    return not active_run and not active_action


@app.delete("/api/incidents")
def remove_all_incidents(
    principal=Depends(operator),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    # Applies to every page/state in the allowed namespaces, not the current UI page.
    rows = db.scalars(
        select(Incident)
        .where(Incident.namespace.in_(allowed(settings)), visible_incident())
        .order_by(Incident.id)
        .with_for_update()
    ).all()
    deleted = 0
    for incident in rows:
        if not history_deletable(db, incident):
            continue
        timeline(
            db,
            incident,
            "incident_deleted",
            principal,
            "deleted:" + incident.id,
            {
                "reason": "Operator bulk removed historical incidents from the interface",
                "audit_retained": True,
            },
        )
        deleted += 1
    db.commit()
    return {"deleted_count": deleted, "retained_count": len(rows) - deleted, "audit_retained": True}


@app.delete("/api/incidents/{incident_id}")
def remove_incident(
    incident_id: UUID,
    principal=Depends(operator),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    incident = owned(db, incident_id, settings, lock=True)
    if not history_deletable(db, incident):
        raise HTTPException(409, "只能刪除已結束且沒有進行中診斷或修復的事件")
    # Keep the tombstone, evidence and approval history. Bridge/late alerts cannot resurrect it.
    timeline(
        db,
        incident,
        "incident_deleted",
        principal,
        "deleted:" + incident.id,
        {
            "reason": "Operator removed historical incident from the interface",
            "audit_retained": True,
        },
    )
    db.commit()
    return {"id": incident.id, "deleted": True, "audit_retained": True}


@app.get("/api/incidents/{incident_id}/timeline")
def events(
    incident_id: UUID,
    limit: int = Query(50, ge=1, le=100),
    offset: int = Query(0, ge=0, le=10000),
    principal=Depends(operator),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    incident = owned(db, incident_id, settings)
    return {
        "items": [
            {
                "id": r.id,
                "actor": r.actor,
                "event_type": r.event_type,
                "created_at": r.created_at,
                "payload": json.loads(r.payload_json),
            }
            for r in db.scalars(
                select(Timeline)
                .where(Timeline.incident_id == incident.id)
                .order_by(Timeline.created_at, Timeline.id)
                .offset(offset)
                .limit(limit)
            )
        ]
    }


@app.get("/api/incidents/{incident_id}/evidence")
def evidence(
    incident_id: UUID,
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0, le=10000),
    principal=Depends(operator),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    incident = owned(db, incident_id, settings)
    return {
        "items": [
            {
                "evidence_id": r.id,
                "tool_name": r.kind,
                "source": r.source,
                "requested_at": r.requested_at,
                "observed_at": r.observed_at,
                "target": json.loads(r.query_json),
                "data": json.loads(r.payload_json),
                "truncated": r.truncated,
                "errors": json.loads(r.errors_json),
                "checksum": r.checksum,
            }
            for r in db.scalars(
                select(Evidence)
                .where(Evidence.incident_id == incident.id)
                .order_by(Evidence.observed_at, Evidence.id)
                .offset(offset)
                .limit(limit)
            )
        ]
    }


@app.post("/api/incidents/{incident_id}/analyze", status_code=202)
def analyze(
    incident_id: UUID,
    idempotency_key: str = Header(min_length=1, max_length=128),
    principal=Depends(operator),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    incident = owned(db, incident_id, settings, lock=True)
    before_version = incident.version
    try:
        run = schedule(
            db,
            incident,
            principal,
            digest(incident.id + ":" + idempotency_key),
            reopen_video=True,
            settings=settings,
        )
        db.commit()
        return {
            "run_id": run.id,
            "status": run.status,
            "reopened": incident.version != before_version and incident.status == "open",
        }
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/api/incidents/{incident_id}/transition")
def change(
    incident_id: UUID,
    request: TransitionInput,
    principal=Depends(operator),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    incident = owned(db, incident_id, settings, lock=True)
    try:
        transition(db, incident, request.state, principal, request.reason, request.expected_version)
        db.commit()
        return incident_json(incident)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc


@app.post("/api/incidents/{incident_id}/tools")
def inspect(
    incident_id: UUID,
    request: ToolRequest,
    principal=Depends(operator),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    incident = owned(db, incident_id, settings)
    if request.namespace != incident.namespace or (
        request.pod and request.pod != incident.resource_id
    ):
        raise HTTPException(403, "Tool target does not belong to incident")
    if request.tool_name in {"probe_video_input", "get_encoder_inventory"}:
        from app.aiops.video_bridge import linked_job

        link = linked_job(incident)
        if (
            not link
            or str(request.job_id) != link["job_id"]
            or str(request.task_id) != link["task_id"]
        ):
            raise HTTPException(403, "Probe task does not belong to incident")
    if request.tool_name == "get_task_status":
        linked = False
        for evidence in db.scalars(
            select(Evidence).where(
                Evidence.incident_id == incident.id, Evidence.kind == "query_logs"
            )
        ):
            obj = json.loads(evidence.payload_json)
            for line in obj.get("lines", []) if isinstance(obj, dict) else []:
                try:
                    data = json.loads(line["line"])
                    if (not request.job_id or data.get("job_id") == str(request.job_id)) and (
                        not request.task_id or data.get("task_id") == str(request.task_id)
                    ):
                        linked = True
                except (ValueError, KeyError, TypeError):
                    continue
        if not linked:
            raise HTTPException(403, "Task ID is not linked to incident evidence")
    try:
        result = gateway().call(request)
        save(db, incident.id, result)
        db.commit()
        return result.model_dump(mode="json")
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post("/api/knowledge/documents", status_code=201)
def add_document(
    request: DocumentInput,
    principal=Depends(operator),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    if not settings.knowledge_vectors_enabled:
        raise HTTPException(503, "Knowledge vectors disabled")
    try:
        result = ingest(db, request)
        db.commit()
        return result
    except ValueError as exc:
        db.rollback()
        raise HTTPException(422, str(exc)) from None


@app.get("/api/knowledge/documents")
def documents(
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0, le=10000),
    principal=Depends(operator),
    db: Session = Depends(get_db),
):
    return {
        "items": [
            {
                "document_id": d.id,
                "title": d.title,
                "kind": d.kind,
                "source_path": d.source_path,
                "version": d.version,
                "checksum": d.content_hash,
                "status": d.status,
                "metadata": json.loads(d.metadata_json),
            }
            for d in db.scalars(
                select(KBDocument)
                .where(KBDocument.id.startswith("aiops-kb-"))
                .order_by(KBDocument.id)
                .limit(limit)
                .offset(offset)
            )
        ]
    }


@app.post("/api/knowledge/search")
def retrieve(
    request: SearchInput,
    principal=Depends(operator),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    if not settings.knowledge_vectors_enabled:
        raise HTTPException(503, "Knowledge vectors disabled")
    try:
        return search(db, request)
    except ValueError:
        raise HTTPException(503, "Knowledge embedding provider unavailable") from None


@app.get("/api/incidents/{incident_id}/report")
def report(
    incident_id: UUID,
    principal=Depends(operator),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    from app.models.aiops import Diagnosis

    incident = owned(db, incident_id, settings)
    row = db.scalar(
        select(Diagnosis)
        .where(Diagnosis.incident_id == incident.id)
        .order_by(Diagnosis.created_at.desc(), Diagnosis.run_id.desc())
        .limit(1)
    )
    if not row:
        raise HTTPException(404, "Diagnosis not available yet")
    return {
        "incident_id": row.incident_id,
        "run_id": row.run_id,
        "thread_id": row.thread_id,
        "model_id": row.model_id,
        "prompt_version": row.prompt_version,
        "validation_status": row.validation_status,
        "created_at": row.created_at,
        "diagnosis": json.loads(row.payload_json),
    }


@app.get("/api/incidents/{incident_id}/actions")
def list_actions(
    incident_id: UUID,
    principal=Depends(operator),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    from app.models.aiops import Action, Approval, Diagnosis
    from app.aiops.actions import action_json, SPECS

    incident = owned(db, incident_id, settings)
    enabled = (
        settings.remediation_enabled
        and settings.aiops_approval_enabled
        and settings.aiops_hypothesis_remediation_enabled
    )
    latest = db.scalar(
        select(Diagnosis)
        .where(Diagnosis.incident_id == incident.id)
        .order_by(Diagnosis.created_at.desc(), Diagnosis.run_id.desc())
        .limit(1)
    )
    active = db.scalar(
        select(DiagnosisRun.id)
        .where(
            DiagnosisRun.incident_id == incident.id,
            DiagnosisRun.status.in_(["pending", "running", "waiting"]),
        )
        .limit(1)
    )
    rows = db.scalars(
        select(Action)
        .where(Action.incident_id == incident.id)
        .order_by(Action.created_at.desc(), Action.id.desc())
        .limit(100)
    ).all()
    approvals = {
        a.action_id: a
        for a in db.scalars(select(Approval).where(Approval.action_id.in_([r.id for r in rows])))
    }
    runs = {
        r.id: r
        for r in db.scalars(
            select(DiagnosisRun).where(DiagnosisRun.id.in_([a.run_id for a in rows]))
        )
    }

    def with_approval(row):
        data = action_json(row)
        a = approvals.get(row.id)
        data["approval"] = (
            {
                "principal": a.principal,
                "decision": a.decision,
                "reason": a.reason,
                "decided_at": a.decided_at,
            }
            if a
            else None
        )
        run = runs.get(row.run_id)
        current = bool(
            latest and latest.run_id == row.run_id and (not active or active == row.run_id)
        )
        expired = row.expires_at <= now()
        reason = ""
        if not run or run.workflow_kind != "aiops-hypothesis-v2" or row.action_type not in SPECS:
            reason = "歷史方案僅供查閱，請建立新版診斷。"
        elif not current:
            reason = "已有較新的診斷或調查進行中，不能核准此舊方案。"
        elif row.approval_status != "pending":
            reason = "此方案目前不是等待核准狀態。"
        elif expired:
            reason = "方案已過期，請重新診斷取得新草案。"
        elif row.execution_status != "pending":
            reason = "此方案已進入執行流程，不能重複核准。"
        elif not enabled:
            reason = "受限修復尚未啟用，請由操作者確認部署設定。"
        elif latest.validation_status != "validated":
            reason = "最新診斷尚未通過證據驗證。"
        elif incident.lifecycle != "WAITING_APPROVAL":
            reason = "事件目前不在等待核准階段。"
        # Display hints only; POST /decision still performs all evidence and live target policy checks.
        data["review"] = {
            "can_approve": not reason,
            "reason": reason,
            "can_reject": bool(
                current
                and run
                and run.workflow_kind == "aiops-hypothesis-v2"
                and row.approval_status == "pending"
                and not expired
                and row.execution_status == "pending"
            ),
            "policy_recheck_required": True,
        }
        return data

    return {"items": [with_approval(x) for x in rows], "remediation_enabled": enabled}


from app.aiops.actions import Decision


@app.post("/api/incidents/{incident_id}/actions/{action_id}/decision")
def review(
    incident_id: UUID,
    action_id: UUID,
    request: Decision,
    principal=Depends(operator),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    from app.models.aiops import Action
    from app.aiops.actions import action_json, decide
    from app.aiops.executor import KubernetesActionClient

    incident = owned(db, incident_id, settings, lock=True)
    action = db.get(Action, str(action_id), with_for_update=True)
    if not action or action.incident_id != incident.id:
        raise HTTPException(404, "Action not found")
    try:
        row = decide(db, action, request, principal, KubernetesActionClient(), settings)
        db.commit()
        return action_json(row)
    except PermissionError as exc:
        db.rollback()
        raise HTTPException(403, str(exc)) from None
    except ValueError as exc:
        db.rollback()
        raise HTTPException(409, str(exc)) from None


@app.post("/api/incidents/{incident_id}/actions/{action_id}/verify")
def verify_action(
    incident_id: UUID,
    action_id: UUID,
    principal=Depends(operator),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    from app.models.aiops import Action, Approval, Diagnosis
    from app.aiops.actions import action_json, binding, configure, SPECS
    from app.aiops.executor import KubernetesActionClient, recovery_valid
    import httpx

    incident = owned(db, incident_id, settings, lock=True)
    action = db.get(Action, str(action_id), with_for_update=True)
    if not action or action.incident_id != incident.id:
        raise HTTPException(404, "Action not found")
    approval = db.get(Approval, action.id)
    if (
        action.action_type not in SPECS
        or action.execution_status != "uncertain"
        or action.approval_status != "approved"
        or not approval
        or approval.decision != "approved"
        or approval.binding_hash != action.binding_hash
        or action.binding_hash != binding(action)
        or approval.principal != settings.aiops_operator_principal
    ):
        raise HTTPException(409, "僅可重新驗證已核准且結果不確定的原影片修復")
    if db.scalar(
        select(DiagnosisRun.id)
        .where(
            DiagnosisRun.incident_id == incident.id,
            DiagnosisRun.status.in_(["pending", "running", "waiting"]),
        )
        .limit(1)
    ):
        raise HTTPException(409, "請等待目前診斷流程結束再重新驗證")
    report = db.scalar(
        select(Diagnosis)
        .where(Diagnosis.incident_id == incident.id)
        .order_by(Diagnosis.created_at.desc(), Diagnosis.run_id.desc())
        .limit(1)
    )
    if not report or report.run_id != action.run_id:
        raise HTTPException(409, "請查核最新診斷的修復動作")
    client = KubernetesActionClient(settings=settings)
    configure(client, action.action_type, action)
    try:
        verification = client.verify(
            read_only=True
        )  # Read existing receipt/output only; never call patch or /repair.
        if not recovery_valid(action, verification):
            raise ValueError("Recovery proof does not match action")
    except (ValueError, PermissionError, httpx.HTTPError, KeyError) as exc:
        timeline(
            db,
            incident,
            "recovery_recheck_failed",
            principal,
            "recheck:" + str(uuid4()),
            {"action_id": action.id, "error_type": type(exc).__name__, "mutation_attempted": False},
        )
        db.commit()
        return {
            "verified": False,
            "message": "仍未通過復原驗證；請核對原影片結果與服務狀態",
            "action": action_json(action),
        }
    previous = json.loads(action.result_json or "{}")
    action.result_json = json.dumps(
        {
            "prior_result": previous,
            "verification": verification,
            "rechecked_at": now().isoformat() + "Z",
            "recheck_mutation_attempted": False,
        }
    )
    action.execution_status = "succeeded"
    output = json.loads(report.payload_json)
    output["recovery_verified"] = True
    report.payload_json = json.dumps(output, ensure_ascii=False)
    if incident.lifecycle == "ESCALATED":
        transition(db, incident, "RESOLVED", principal, "Read-only recovery recheck passed")
    timeline(
        db,
        incident,
        "recovery_rechecked",
        principal,
        "recheck:" + str(uuid4()),
        {
            "action_id": action.id,
            "job_id": json.loads(action.params_json)["job_id"],
            "recovery_verified": True,
            "mutation_attempted": False,
        },
    )
    db.commit()
    return {
        "verified": True,
        "message": "同一影片的新輸出已通過復原驗證，沒有重跑影片",
        "action": action_json(action),
    }


@app.get("/api/knowledge/chunk")
def chunk(
    chunk_id: str = Query(min_length=1, max_length=256),
    principal=Depends(operator),
    db: Session = Depends(get_db),
):
    from app.models.diagnostics import KBChunk

    row = db.get(KBChunk, chunk_id)
    if not row or not row.document_id.startswith("aiops-kb-"):
        raise HTTPException(404, "Runbook chunk not found")
    document = db.get(KBDocument, row.document_id)
    return {
        "chunk_id": row.id,
        "title": document.title,
        "heading": row.heading,
        "content": row.content,
        "version": row.document_version,
        "source_path": document.source_path,
        "checksum": row.content_hash,
        "active": row.active,
    }


@app.middleware("http")
async def count_requests(request, call_next):
    response = await call_next(request)
    from app.aiops.metrics import REQUESTS

    if request.url.path != "/metrics":
        REQUESTS.labels(
            request.method
            if request.method in {"GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"}
            else "OTHER",
            str(response.status_code),
        ).inc()
    return response


@app.get("/metrics")
def metrics():
    from prometheus_client import generate_latest, CONTENT_TYPE_LATEST
    from starlette.responses import Response
    from prometheus_client import CollectorRegistry
    from app.aiops.metrics import IncidentCollector

    registry = CollectorRegistry()
    registry.register(IncidentCollector())
    return Response(generate_latest() + generate_latest(registry), media_type=CONTENT_TYPE_LATEST)
