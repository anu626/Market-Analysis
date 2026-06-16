from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import case, func, or_, and_
from sqlalchemy.orm import Session

from app.api.schemas import ArticleOut, IngestionResult
from app.database import get_db
from app.ingestion.source_loader import CATEGORY_TO_VERTICAL, get_logo_map, sources_of_type
from app.models import Article
from app.ranking.ranker import recompute_all
from app.services.cache import cache_delete_prefix, cache_get, cache_set
from app.services.ingestion_service import run_full_ingestion, run_vertical_ingestion



def _dedup_by_story(rows: list, offset: int, limit: int) -> list[dict]:
    """Deduplicate articles sharing the same story_hash, keeping the top-ranked.
    Returns dicts with an injected source_count and source_logo field."""
    logos = get_logo_map()
    seen: dict[str, dict] = {}  # story_hash -> best article dict
    counts: dict[str, set] = {}  # story_hash -> set of source_names

    for r in rows:
        key = r.story_hash or str(r.id)
        d = ArticleOut.model_validate(r).model_dump()
        d["source_logo"] = logos.get(r.source_name)
        if key not in seen:
            seen[key] = d
            counts[key] = {r.source_name}
        else:
            counts[key].add(r.source_name)

    result = []
    for key, article in seen.items():
        article["source_count"] = len(counts[key])
        result.append(article)

    return result[offset: offset + limit]

router = APIRouter()


@router.get("/health")
def health():
    return {"status": "ok"}


@router.get("/sources")
def list_sources(db: Session = Depends(get_db)):
    logos = get_logo_map()
    rows = (
        db.query(Article.source_name, func.count(Article.id).label("count"))
        .group_by(Article.source_name)
        .order_by(func.count(Article.id).desc())
        .all()
    )
    return [{"name": r[0], "count": r[1], "logo": logos.get(r[0])} for r in rows]


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
    logos = get_logo_map()
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
            "logo": logos.get(s["name"]),
        }
        for s in srcs
    ]


def _apply_vertical(q, vertical: str | None):
    if not vertical:
        return q
    if vertical == "Tech":
        return q.filter(Article.vertical.in_(["Tech", "Market Trends"]))
    return q.filter(Article.vertical == vertical)


def _apply_search(q, search: str | None):
    if not search:
        return q
    pattern = f"%{search.strip()}%"
    return q.filter(or_(Article.title.ilike(pattern), Article.summary.ilike(pattern)))


def _quality_order():
    """Sort: fully enriched with image first, then enriched, then raw."""
    return case(
        (Article.ai_title.isnot(None) & Article.ai_summary.isnot(None) & Article.image_url.isnot(None), 0),
        (Article.ai_title.isnot(None) & Article.ai_summary.isnot(None), 1),
        (Article.ai_title.isnot(None), 2),
        else_=3,
    )


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
    ai_priority = _quality_order()
    # Fetch extra rows so dedup still fills the requested page after collapsing same-story articles
    rows = (
        query.order_by(ai_priority, Article.rank_score.desc())
        .limit((offset + limit) * 5)
        .all()
    )
    payload = _dedup_by_story(rows, offset, limit)
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
    ai_priority = _quality_order()
    rows = (
        query.order_by(ai_priority, Article.created_at.desc())
        .offset(offset)
        .limit(limit)
        .all()
    )
    logos = get_logo_map()
    payload = []
    for r in rows:
        d = ArticleOut.model_validate(r).model_dump()
        d["source_logo"] = logos.get(r.source_name)
        payload.append(d)
    cache_set(cache_key, payload)
    return payload


_CANONICAL_VERTICALS = frozenset(["Hiring", "Layoffs", "Funding", "AI", "Tech", "Blogs", "Market Trends", "Youtube"])


@router.get("/articles/digest", response_model=list[ArticleOut])
def vertical_digest(
    db: Session = Depends(get_db),
    x: int = Query(5, ge=1, le=50, description="Number of articles to return, one best per vertical"),
):
    """Return x articles — the highest-ranked article from each canonical vertical, ordered by rank_score."""
    cache_key = f"articles:digest:{x}"
    cached = cache_get(cache_key)
    if cached is not None:
        return cached

    logos = get_logo_map()

    ai_priority = _quality_order()

    # One best enriched article per canonical vertical — prefer articles with image
    best: list[Article] = []
    for vertical in _CANONICAL_VERTICALS:
        top = (
            db.query(Article)
            .filter(Article.vertical == vertical, Article.image_url.isnot(None))
            .order_by(ai_priority, Article.rank_score.desc())
            .first()
        )
        if not top:
            top = (
                db.query(Article)
                .filter(Article.vertical == vertical)
                .order_by(ai_priority, Article.rank_score.desc())
                .first()
            )
        if top:
            best.append(top)

    # Sort final set: image+enriched first, then by rank_score, return top x
    best.sort(key=lambda a: (
        0 if (a.ai_title and a.ai_summary and a.image_url) else
        1 if (a.ai_title and a.ai_summary) else
        2 if a.ai_title else 3,
        -a.rank_score
    ))
    best = best[:x]

    payload = []
    for r in best:
        d = ArticleOut.model_validate(r).model_dump()
        d["source_logo"] = logos.get(r.source_name)
        payload.append(d)

    cache_set(cache_key, payload)
    return payload


@router.get("/articles/category/{category}", response_model=list[ArticleOut])
def articles_by_category(
    category: str,
    db: Session = Depends(get_db),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    q: str | None = Query(None, description="Search title and summary"),
    sort: str = Query("ranked", description="ranked | latest"),
):
    """Fetch articles for a specific category (e.g. recruitment, ai, layoffs)."""
    mapped_vertical = CATEGORY_TO_VERTICAL.get(category.lower())
    if not mapped_vertical:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown category '{category}'. Valid values: {sorted(CATEGORY_TO_VERTICAL)}",
        )

    cache_key = f"articles:category:{category}:{q or '_'}:{sort}:{limit}:{offset}"
    cached = cache_get(cache_key)
    if cached is not None:
        return cached

    query = db.query(Article).filter(Article.vertical == mapped_vertical)
    query = _apply_search(query, q)

    ai_priority = _quality_order()
    order = Article.rank_score.desc() if sort == "ranked" else Article.created_at.desc()

    # For recruitment: surface purely hiring-focused articles first
    if category.lower() == "recruitment":
        _HIRING_KEYWORDS = [
            "hiring", "to hire", "recruitment", "recruit", "job opening",
            "fresher", "campus placement", "salary hike", "appraisal",
            "headcount", "workforce expansion", "talent acquisition",
            "job market", "it jobs", "tech jobs", "engineer hiring",
        ]
        hiring_signal = case(
            (or_(*[
                Article.title.ilike(f"%{kw}%") for kw in _HIRING_KEYWORDS
            ]), 0),
            else_=1,
        )
        rows = (
            query.order_by(hiring_signal, ai_priority, order)
            .limit((offset + limit) * 5)
            .all()
        )
    else:
        rows = (
            query.order_by(ai_priority, order)
            .limit((offset + limit) * 5)
            .all()
        )
    payload = _dedup_by_story(rows, offset, limit)
    cache_set(cache_key, payload)
    return payload


@router.get("/articles/{article_id}", response_model=ArticleOut)
def get_article(article_id: int, db: Session = Depends(get_db)):
    row = db.get(Article, article_id)
    if not row:
        raise HTTPException(status_code=404, detail="Article not found")
    d = ArticleOut.model_validate(row).model_dump()
    d["source_logo"] = get_logo_map().get(row.source_name)
    return d


@router.post("/enrich/backfill")
def backfill_enrichment(limit: int = Query(50, ge=1, le=100000)):
    """Enrich up to `limit` unenriched articles in the background."""
    from app.enrichment.enricher import enrich_pending
    import threading
    t = threading.Thread(target=enrich_pending, args=(limit,), daemon=True)
    t.start()
    return {"status": "started", "limit": limit}


@router.post("/ingest", response_model=IngestionResult)
def trigger_ingestion(db: Session = Depends(get_db)):
    """Manual trigger. Runs synchronously — fine for prototype, slow for prod."""
    stats = run_full_ingestion(db)
    return IngestionResult(**stats)


_VALID_VERTICALS = {"Hiring", "Recruitment", "Layoffs", "Funding", "AI", "Tech", "Blogs", "Market Trends", "Youtube"}

# Map API-facing vertical names to the internal category key used by source_loader
_VERTICAL_TO_CATEGORY = {
    "Recruitment": "recruitment",
}


@router.post("/ingest/{vertical}", response_model=IngestionResult)
def trigger_vertical_ingestion(vertical: str, db: Session = Depends(get_db)):
    """Ingest news for a single vertical only."""
    if vertical not in _VALID_VERTICALS:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown vertical '{vertical}'. Valid values: {sorted(_VALID_VERTICALS)}",
        )
    category = _VERTICAL_TO_CATEGORY.get(vertical, vertical)
    stats = run_vertical_ingestion(db, category)
    return IngestionResult(**stats)


@router.post("/rerank")
def trigger_rerank(db: Session = Depends(get_db)):
    """Recompute rank_score for all articles with the current multi-signal formula and bust cache."""
    n = recompute_all(db)
    cache_delete_prefix("articles:")
    return {"reranked": n}


@router.post("/generate-images")
def trigger_image_generation(
    limit: int = Query(50, ge=1, le=200, description="Max articles to generate images for"),
    vertical: str | None = Query(None, description="Filter by vertical e.g. Layoffs, Hiring, AI"),
    db: Session = Depends(get_db),
):
    """Generate AI infographic images for articles that have no image_url. Manual trigger only."""
    from app.enrichment.enricher import _generate_article_image

    query = db.query(Article).filter(Article.image_url.is_(None))
    if vertical:
        query = query.filter(Article.vertical == vertical)
    articles = query.order_by(Article.rank_score.desc()).limit(limit).all()

    generated, failed = 0, 0
    for article in articles:
        url = _generate_article_image(
            article.id,
            article.ai_title or article.title,
            article.vertical or "Tech",
        )
        if url:
            article.image_url = url
            db.commit()
            generated += 1
        else:
            failed += 1

    cache_delete_prefix("articles:")
    return {"generated": generated, "failed": failed, "total": len(articles)}
