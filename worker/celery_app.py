from celery import Celery
from app.config import settings

celery_app = Celery(
    "riva_worker",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND,
    include=["worker.pipeline"]
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    task_acks_late=True, # Acknowledge only after task finishes, prevents lost tasks
    worker_prefetch_multiplier=1, # Take 1 task at a time per worker process to preserve disk space
)
