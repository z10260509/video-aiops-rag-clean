import logging
import time

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy import text

from app.api.jobs import router as jobs_router
from app.core.config import get_settings
from app.database.session import SessionLocal
from app.services.storage import ensure_storage
from app.services.tasks import celery_app
from app.services.metrics import HTTP_REQUEST_DURATION, HTTP_REQUESTS, update_job_metrics

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
app = FastAPI(title="Video Processing Platform")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://localhost:5174"],
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(jobs_router)
from app.api.video_integration import router as video_integration_router

app.include_router(video_integration_router)


@app.middleware("http")
async def record_request_metrics(request: Request, call_next):
    started = time.perf_counter()
    response = await call_next(request)
    route = request.scope.get("route")
    path = getattr(route, "path", request.url.path)
    HTTP_REQUESTS.labels(method=request.method, path=path, status=response.status_code).inc()
    HTTP_REQUEST_DURATION.labels(method=request.method, path=path).observe(
        time.perf_counter() - started
    )
    return response


@app.on_event("startup")
def startup() -> None:
    ensure_storage(get_settings())


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/ready")
def ready():
    try:
        with SessionLocal() as db:
            db.execute(text("SELECT 1"))
        # Integrated mode must keep job/evidence reads available during a broker outage.
        # Upload still records a FAILED enqueue attempt and returns 503 when publishing fails.
        if not get_settings().aiops_video_enabled:
            celery_app.connection_for_read().ensure_connection(max_retries=0)
    except Exception as exc:
        raise HTTPException(status_code=503, detail="Dependencies are not ready") from exc
    return {"status": "ready"}


@app.get("/metrics", include_in_schema=False)
def metrics():
    with SessionLocal() as db:
        update_job_metrics(db)
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
