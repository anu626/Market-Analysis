from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session

from app.api.schemas import ArticleOut, IngestionResult
from app.database import get_db
from app.ingestion.source_loader import sources_of_type
from app.models import Article
from app.services.cache import cache_get, cache_set
from app.services.ingestion_service import run_full_ingestion

router = APIRouter()


@router.get("/health")
def health():
    return {"status": "ok"}


@router.get("/sources")
def list_sources(db: Session = Depends(get_db)):
    rows = (
        db.query(Article.source_name, func.count(Article.id).label("count"))
        .group_by(Article.source_name)
        .order_by(func.count(Article.id).desc())
        .all()
    )
    return [{"name": r[0], "count": r[1]} for r in rows]


@router.get("/sources/config")
def list_configured_sources(
    vertical: str | None = Query(None, description="tech | business | both"),
    type_: str | None = Query(None, alias="type", description="rss | api | google_news | json_api"),
    tier: int | None = Query(None, description="1 | 2 | 3"),
):
    """All sources from sources.yaml — no ingestion needed to call this."""
    srcs = sources_of_type("rss", "google_news", "api", "json_api", "scraper")
    if vertical:
        srcs = [s for s in srcs if s.get("vertical") == vertical]
    if type_:
        srcs = [s for s in srcs if s.get("type") == type_]
    if tier:
        srcs = [s for s in srcs if s.get("tier") == tier]
    return [
        {
            "name": s["name"],
            "type": s.get("type"),
            "vertical": s.get("vertical", "tech"),
            "tier": s.get("tier"),
            "authority": s.get("authority"),
            "country": s.get("country"),
            "tags": s.get("tags", []),
            "url": s.get("url"),
        }
        for s in srcs
    ]


def _apply_vertical(q, vertical: str | None):
    if not vertical:
        return q
    if vertical == "tech":
        return q.filter(Article.vertical.in_(["tech", "both"]))
    if vertical == "business":
        return q.filter(Article.vertical.in_(["business", "both"]))
    return q


def _apply_search(q, search: str | None):
    if not search:
        return q
    pattern = f"%{search.strip()}%"
    return q.filter(or_(Article.title.ilike(pattern), Article.summary.ilike(pattern)))


@router.get("/articles", response_model=list[ArticleOut])
def list_articles(
    db: Session = Depends(get_db),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    source: str | None = Query(None),
    q: str | None = Query(None, description="Search title and summary"),
    vertical: str | None = Query(None, description="tech | business"),
):
    cache_key = f"articles:ranked:{source or 'all'}:{q or '_'}:{vertical or 'all'}:{limit}:{offset}"
    cached = cache_get(cache_key)
    if cached is not None:
        return cached

    query = db.query(Article)
    if source:
        query = query.filter(Article.source_name == source)
    query = _apply_vertical(query, vertical)
    query = _apply_search(query, q)
    rows = (
        query.order_by(Article.rank_score.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    payload = [ArticleOut.model_validate(r).model_dump() for r in rows]
    cache_set(cache_key, payload)
    return payload


@router.get("/articles/latest", response_model=list[ArticleOut])
def latest_articles(
    db: Session = Depends(get_db),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    source: str | None = Query(None),
    q: str | None = Query(None, description="Search title and summary"),
    vertical: str | None = Query(None, description="tech | business"),
):
    cache_key = f"articles:latest:{source or 'all'}:{q or '_'}:{vertical or 'all'}:{limit}:{offset}"
    cached = cache_get(cache_key)
    if cached is not None:
        return cached

    query = db.query(Article)
    if source:
        query = query.filter(Article.source_name == source)
    query = _apply_vertical(query, vertical)
    query = _apply_search(query, q)
    rows = (
        query.order_by(Article.created_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    payload = [ArticleOut.model_validate(r).model_dump() for r in rows]
    cache_set(cache_key, payload)
    return payload


@router.get("/articles/{article_id}", response_model=ArticleOut)
def get_article(article_id: int, db: Session = Depends(get_db)):
    row = db.get(Article, article_id)
    if not row:
        raise HTTPException(status_code=404, detail="Article not found")
    return ArticleOut.model_validate(row)


@router.post("/ingest", response_model=IngestionResult)
def trigger_ingestion(db: Session = Depends(get_db)):
    """Manual trigger. Runs synchronously — fine for prototype, slow for prod."""
    stats = run_full_ingestion(db)
    return IngestionResult(**stats)
