import os

from celery import Celery
from celery.signals import after_setup_logger, after_setup_task_logger
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), "..", "..", ".env"))
celery_app = Celery("video_worker", broker=os.getenv("REDIS_URL", "redis://localhost:6379/0"))
celery_app.conf.update(
    task_track_started=True,
    worker_prefetch_multiplier=1,
    task_acks_late=True,
)
celery_app.autodiscover_tasks(["worker.app"])


@after_setup_logger.connect
@after_setup_task_logger.connect
def configure_json_logging(logger, **_kwargs):
    from app.services.logging import configure_worker_logs

    configure_worker_logs(logger)
