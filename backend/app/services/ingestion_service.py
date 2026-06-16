"""Orchestrator: fetch -> normalize -> dedup -> store -> rank -> bust cache.

Kept as a single function so it can be invoked from a Celery task, FastAPI
endpoint, or CLI without re-wiring.
"""

import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.dedup.deduplicator import find_duplicate, merge_into_existing
from app.ingestion.source_loader import CATEGORY_TO_VERTICAL
from app.ingestion.hn_fetcher import fetch_hn
from app.ingestion.json_api_fetcher import fetch_json_api
from app.ingestion.layoffs_fyi_scraper import fetch_layoffs_fyi
from app.ingestion.reddit_fetcher import fetch_reddit
from app.ingestion.rss_fetcher import fetch_rss
from app.models import Article, IngestionLog, Source
from app.normalization.normalizer import normalize_item
from app.enrichment.enricher import enrich_batch_async
from app.pipeline.filters import run_pre_db_pipeline
from app.ranking.ranker import _load_source_config, compute_rank, recompute_all, story_hash
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
    filtered = 0
    errors = 0
    seen_urls: set[str] = set()

    # Normalize first so the pipeline filters operate on clean, typed data
    authority_map, _ = _load_source_config()
    normalized_raw: list[dict] = []
    for raw in raw_items:
        item = normalize_item(raw)
        if item:
            item["source_authority"] = authority_map.get(item["source_name"], 0.5)
            item["country"] = raw.get("country", "")
            normalized_raw.append(item)
        else:
            errors += 1

    # Pre-DB pipeline: junk filter → in-batch dedupe → category override → pre-score
    pipeline_out = run_pre_db_pipeline(normalized_raw)
    filtered = len(normalized_raw) - len(pipeline_out)
    if filtered:
        logger.debug("pipeline filters dropped %d junk/stale items", filtered)

    for item in pipeline_out:
        try:
            url = item["url"]
            if url in seen_urls:
                duplicates += 1
                continue
            seen_urls.add(url)

            vertical = CATEGORY_TO_VERTICAL.get(item.get("vertical", "tech").lower(), item.get("vertical", "tech"))
            src = _ensure_source(db, item["source_name"], source_type, vertical)

            existing = find_duplicate(db, url=url, title=item["title"])
            if existing:
                if merge_into_existing(existing, item):
                    existing.rank_score = compute_rank(
                        existing.created_at,
                        source_name=existing.source_name or "",
                        title=existing.title or "",
                        published_at=existing.published_at,
                    )
                duplicates += 1
                continue

            now = datetime.now(timezone.utc).replace(tzinfo=None).replace(tzinfo=None)
            title = item["title"]
            source_name = item["source_name"]
            article = Article(
                title=title,
                url=item["url"],
                source_id=src.id,
                source_name=source_name,
                score=item["score"],
                summary=item.get("summary"),
                published_at=item.get("published_at"),
                created_at=now,
                external_id=item.get("external_id"),
                story_hash=story_hash(title),
                rank_score=compute_rank(
                    now,
                    source_name=source_name,
                    title=title,
                    published_at=item.get("published_at"),
                ),
                vertical=vertical,
            )
            db.add(article)
            inserted += 1
        except Exception as e:
            errors += 1
            logger.exception("Failed to persist item: %s", e)

    db.commit()

    # Flush to get IDs, then enrich new articles in background
    new_ids = [a.id for a in db.query(Article.id)
               .filter(Article.ai_enriched_at.is_(None))
               .order_by(Article.created_at.desc())
               .limit(inserted)
               .all()] if inserted else []
    enrich_batch_async(new_ids)

    return {"fetched": fetched, "inserted": inserted, "duplicates": duplicates, "filtered": filtered, "errors": errors}


def _run_source(db: Session, name: str, fetcher, source_type: str) -> dict:
    log = IngestionLog(source=name, started_at=datetime.now(timezone.utc).replace(tzinfo=None))
    db.add(log)
    db.commit()

    try:
        raw = fetcher() or []
    except Exception as e:
        logger.exception("Fetcher %s failed: %s", name, e)
        log.errors = 1
        log.notes = str(e)[:500]
        log.finished_at = datetime.now(timezone.utc).replace(tzinfo=None)
        db.commit()
        return {"fetched": 0, "inserted": 0, "duplicates": 0, "filtered": 0, "errors": 1}

    stats = _persist_batch(db, raw, source_type)
    log.fetched = stats["fetched"]
    log.inserted = stats["inserted"]
    log.duplicates = stats["duplicates"]
    log.errors = stats["errors"]
    log.finished_at = datetime.now(timezone.utc).replace(tzinfo=None)
    db.commit()
    return stats


def run_vertical_ingestion(db: Session, vertical: str) -> dict:
    """Fetch from sources matching a specific vertical, then rerank and bust cache."""
    totals = {"fetched": 0, "inserted": 0, "duplicates": 0, "filtered": 0, "errors": 0}

    for name, fetcher, kind in (
        ("Hacker News", lambda: fetch_hn(vertical), "api"),
        ("RSS", lambda: fetch_rss(vertical), "rss"),
        ("Reddit", lambda: fetch_reddit(vertical), "api"),
        ("Job Boards", lambda: fetch_json_api(vertical), "json_api"),
    ):
        stats = _run_source(db, name, fetcher, kind)
        for k in totals:
            totals[k] += stats[k]
        logger.info("Vertical ingestion [%s] %s done: %s", vertical, name, stats)

    if vertical.lower() == "layoffs":
        stats = _run_source(db, "layoffs.fyi", fetch_layoffs_fyi, "scraper")
        for k in totals:
            totals[k] += stats[k]
        logger.info("Vertical ingestion [%s] layoffs.fyi done: %s", vertical, stats)

    recompute_all(db)
    cache_delete_prefix("articles:")
    return totals


def run_full_ingestion(db: Session) -> dict:
    """Fetch from all configured sources, normalize, dedup, persist, bust cache."""
    totals = {"fetched": 0, "inserted": 0, "duplicates": 0, "filtered": 0, "errors": 0}

    for name, fetcher, kind in (
        ("Hacker News", fetch_hn, "api"),
        ("RSS", fetch_rss, "rss"),
        ("Reddit", fetch_reddit, "api"),
        ("Job Boards", fetch_json_api, "json_api"),
        ("layoffs.fyi", fetch_layoffs_fyi, "scraper"),
    ):
        stats = _run_source(db, name, fetcher, kind)
        for k in totals:
            totals[k] += stats[k]
        logger.info("Ingestion %s done: %s", name, stats)

    recompute_all(db)
    cache_delete_prefix("articles:")
    return totals
