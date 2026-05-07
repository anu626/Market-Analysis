"""Celery app + Beat schedule. Workers consume the 'celery' default queue."""

from celery import Celery
from celery.schedules import schedule

from app.config import settings

celery_app = Celery(
    "tech_news",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND,
    include=["app.workers.tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    task_track_started=True,
    task_time_limit=600,
    task_soft_time_limit=540,
)

celery_app.conf.beat_schedule = {
    "ingest-every-n-seconds": {
        "task": "app.workers.tasks.ingest_all",
        "schedule": schedule(run_every=settings.INGEST_INTERVAL_SECONDS),
    },
    "rerank-every-five-minutes": {
        "task": "app.workers.tasks.rerank_all",
        "schedule": schedule(run_every=300),
    },
}
