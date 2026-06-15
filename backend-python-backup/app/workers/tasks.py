"""Celery tasks. Each opens its own DB session — workers are separate processes."""

import logging

from app.database import SessionLocal, init_db
from app.ranking.ranker import recompute_all
from app.services.cache import cache_delete_prefix
from app.services.ingestion_service import run_full_ingestion
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)

# Make sure tables exist when worker boots — keeps single-machine prototype trivial.
init_db()


@celery_app.task(name="app.workers.tasks.ingest_all", bind=True, max_retries=2)
def ingest_all(self):
    db = SessionLocal()
    try:
        stats = run_full_ingestion(db)
        logger.info("Scheduled ingestion stats: %s", stats)
        return stats
    except Exception as e:
        logger.exception("ingest_all failed: %s", e)
        raise self.retry(exc=e, countdown=30)
    finally:
        db.close()


@celery_app.task(name="app.workers.tasks.rerank_all")
def rerank_all():
    db = SessionLocal()
    try:
        n = recompute_all(db)
        cache_delete_prefix("articles:")
        logger.info("Reranked %d articles", n)
        return {"reranked": n}
    finally:
        db.close()
