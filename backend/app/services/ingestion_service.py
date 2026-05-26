"""Orchestrator: fetch -> normalize -> dedup -> store -> rank -> bust cache.

Kept as a single function so it can be invoked from a Celery task, FastAPI
endpoint, or CLI without re-wiring.
"""

import logging
from datetime import datetime

from sqlalchemy.orm import Session

from app.dedup.deduplicator import find_duplicate, merge_into_existing
from app.ingestion.hn_fetcher import fetch_hn
from app.ingestion.json_api_fetcher import fetch_json_api
from app.ingestion.reddit_fetcher import fetch_reddit
from app.ingestion.rss_fetcher import fetch_rss
from app.models import Article, IngestionLog, Source
from app.normalization.normalizer import normalize_item
from app.ranking.ranker import compute_rank
from app.services.cache import cache_delete_prefix

logger = logging.getLogger(__name__)


def _ensure_source(db: Session, name: str, type_: str, vertical: str = "tech") -> Source:
    src = db.query(Source).filter(Source.name == name).first()
    if src:
        if src.vertical != vertical:
            src.vertical = vertical
        return src
    src = Source(name=name, type=type_, vertical=vertical)
    db.add(src)
    db.flush()
    return src


def _persist_batch(db: Session, raw_items: list[dict], source_type: str) -> dict:
    fetched = len(raw_items)
    inserted = 0
    duplicates = 0
    errors = 0

    for raw in raw_items:
        try:
            item = normalize_item(raw)
            if not item:
                errors += 1
                continue

            vertical = item.get("vertical", "tech")
            src = _ensure_source(db, item["source_name"], source_type, vertical)

            existing = find_duplicate(db, url=item["url"], title=item["title"])
            if existing:
                if merge_into_existing(existing, item):
                    existing.rank_score = compute_rank(existing.score, existing.created_at)
                duplicates += 1
                continue

            now = datetime.utcnow()
            article = Article(
                title=item["title"],
                url=item["url"],
                source_id=src.id,
                source_name=item["source_name"],
                score=item["score"],
                summary=item.get("summary"),
                published_at=item.get("published_at"),
                created_at=now,
                external_id=item.get("external_id"),
                rank_score=compute_rank(item["score"], now),
                vertical=vertical,
            )
            db.add(article)
            inserted += 1
        except Exception as e:
            errors += 1
            logger.exception("Failed to persist item: %s", e)

    db.commit()
    return {"fetched": fetched, "inserted": inserted, "duplicates": duplicates, "errors": errors}


def _run_source(db: Session, name: str, fetcher, source_type: str) -> dict:
    log = IngestionLog(source=name, started_at=datetime.utcnow())
    db.add(log)
    db.commit()

    try:
        raw = fetcher() or []
    except Exception as e:
        logger.exception("Fetcher %s failed: %s", name, e)
        log.errors = 1
        log.notes = str(e)[:500]
        log.finished_at = datetime.utcnow()
        db.commit()
        return {"fetched": 0, "inserted": 0, "duplicates": 0, "errors": 1}

    stats = _persist_batch(db, raw, source_type)
    log.fetched = stats["fetched"]
    log.inserted = stats["inserted"]
    log.duplicates = stats["duplicates"]
    log.errors = stats["errors"]
    log.finished_at = datetime.utcnow()
    db.commit()
    return stats


def run_full_ingestion(db: Session) -> dict:
    """Fetch from all configured sources, normalize, dedup, persist, bust cache."""
    totals = {"fetched": 0, "inserted": 0, "duplicates": 0, "errors": 0}

    for name, fetcher, kind in (
        ("Hacker News", fetch_hn, "api"),
        ("RSS", fetch_rss, "rss"),
        ("Reddit", fetch_reddit, "api"),
        ("Job Boards", fetch_json_api, "json_api"),
    ):
        stats = _run_source(db, name, fetcher, kind)
        for k in totals:
            totals[k] += stats[k]
        logger.info("Ingestion %s done: %s", name, stats)

    cache_delete_prefix("articles:")
    return totals
