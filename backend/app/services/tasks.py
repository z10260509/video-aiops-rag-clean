from celery import Celery

from app.core.config import get_settings

celery_app = Celery("video_backend", broker=get_settings().redis_url)


def enqueue_job(job_id: str, *, demonstrate_failure=False) -> None:
    import uuid

    task_id = str(uuid.uuid4())
    kwargs = {"demo_invalid_codec": True} if demonstrate_failure else {}
    try:
        celery_app.send_task(
            "worker.process_transcode", args=[job_id], kwargs=kwargs, task_id=task_id
        )
    except Exception as exc:
        exc.video_task_id = task_id
        raise
