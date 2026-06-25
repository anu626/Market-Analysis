from fastapi import APIRouter, Depends, HTTPException, Query, Request
from rapidfuzz import fuzz as _fuzz
from sqlalchemy import case, func, or_, and_
from sqlalchemy.orm import Session

from app.api.schemas import ArticleOut, IngestionResult
from app.database import get_db
from app.ingestion.source_loader import CATEGORY_TO_VERTICAL, get_logo_map, sources_of_type
from app.models import Article
from app.ranking.ranker import recompute_all
from app.services.cache import cache_delete_prefix, cache_get, cache_set
from app.services.ingestion_service import run_full_ingestion, run_vertical_ingestion



_S3_BASE = "https://qahiristmedia.s3.ap-south-1.amazonaws.com"


def _resolve_ai_image(ai_image_url: str | None, base_url: str) -> str | None:
    if not ai_image_url:
        return None
    if ai_image_url.startswith("http"):
        return ai_image_url
    if ai_image_url.startswith("/static/ai-images/"):
        return ai_image_url.replace("/static/ai-images/", f"{_S3_BASE}/ai-images/")
    return f"{base_url.rstrip('/')}{ai_image_url}"


_DISPLAY_DEDUP_THRESHOLD = 80  # token_set_ratio threshold for same-story fuzzy grouping


def _source_entry(name: str, logos: dict) -> dict:
    return {"name": name, "logo": logos.get(name)}


def _dedup_by_story(rows: list, offset: int, limit: int, base_url: str = "") -> list[dict]:
    """Deduplicate articles sharing the same story_hash, keeping the top-ranked.
    A fuzzy second pass merges groups whose representative titles are similar enough
    to be the same story reported differently across sources.
    Returns dicts with an injected source_count and source_logo field."""
    logos = get_logo_map()
    seen: dict[str, dict] = {}         # story_hash -> best article dict
    sources: dict[str, dict] = {}      # story_hash -> {source_name: logo}
    titles: dict[str, str] = {}        # story_hash -> title for fuzzy second pass

    for r in rows:
        key = r.story_hash or str(r.id)
        d = ArticleOut.model_validate(r).model_dump()
        d["source_logo"] = logos.get(r.source_name)
        d["ai_image_url"] = _resolve_ai_image(d.get("ai_image_url"), base_url)
        if key not in seen:
            seen[key] = d
            sources[key] = {r.source_name: logos.get(r.source_name)}
            titles[key] = (r.ai_title or r.title or "").lower()
        else:
            sources[key].setdefault(r.source_name, logos.get(r.source_name))

    # Second pass: merge groups whose representative titles are fuzzy-similar.
    # Catches same-story articles that got different story_hashes due to title phrasing.
    keys = list(seen.keys())
    absorbed: set[str] = set()
    for i in range(len(keys)):
        ki = keys[i]
        if ki in absorbed:
            continue
        for j in range(i + 1, len(keys)):
            kj = keys[j]
            if kj in absorbed:
                continue
            if _fuzz.token_set_ratio(titles[ki], titles[kj]) >= _DISPLAY_DEDUP_THRESHOLD:
                sources[ki].update(sources[kj])
                absorbed.add(kj)

    result = []
    for key, article in seen.items():
        if key in absorbed:
            continue
        src_map = sources[key]
        article["source_count"] = len(src_map)
        article["sources"] = [
            {"name": name, "logo": logo} for name, logo in src_map.items()
        ]
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
        return q.filter(Article.vertical == "Tech")
    return q.filter(Article.vertical == vertical)


_FEED_VERTICALS = ["Hiring", "Layoffs", "Funding", "AI", "Tech", "Market Trends", "Blogs"]


def _diverse_feed(db: Session, limit: int, offset: int, source: str | None, search: str | None) -> list[dict]:
    """Round-robin across verticals so no single vertical dominates the feed."""
    logos = get_logo_map()
    ai_priority = case(
        (Article.ai_title.isnot(None) & Article.ai_summary.isnot(None), 0),
        (Article.ai_title.isnot(None), 1),
        else_=2,
    )
    per_vertical = (offset + limit) * 3
    buckets: dict[str, list] = {}
    for v in _FEED_VERTICALS:
        q = db.query(Article).filter(Article.vertical == v)
        if source:
            q = q.filter(Article.source_name == source)
        if search:
            q = _apply_search(q, search)
        buckets[v] = q.order_by(ai_priority, Article.rank_score.desc()).limit(per_vertical).all()

    # Round-robin interleave
    interleaved = []
    seen_hashes: set[str] = set()
    idx = {v: 0 for v in _FEED_VERTICALS}
    while len(interleaved) < (offset + limit) * 5:
        added = False
        for v in _FEED_VERTICALS:
            bucket = buckets[v]
            i = idx[v]
            while i < len(bucket):
                row = bucket[i]
                i += 1
                key = row.story_hash or str(row.id)
                if key not in seen_hashes:
                    seen_hashes.add(key)
                    interleaved.append(row)
                    added = True
                    break
            idx[v] = i
        if not added:
            break

    # Build output with dedup and source_count
    seen_out: dict[str, dict] = {}
    counts: dict[str, set] = {}
    for r in interleaved:
        key = r.story_hash or str(r.id)
        if key not in seen_out:
            d = ArticleOut.model_validate(r).model_dump()
            d["source_logo"] = logos.get(r.source_name)
            seen_out[key] = d
            counts[key] = {r.source_name}
        else:
            counts[key].add(r.source_name)

    result = []
    for key, article in seen_out.items():
        article["source_count"] = len(counts[key])
        result.append(article)

    return result[offset: offset + limit]


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
    request: Request,
    db: Session = Depends(get_db),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    source: str | None = Query(None),
    q: str | None = Query(None, description="Search title and summary"),
    vertical: str | None = Query(None, description="tech | business"),
    diverse: bool = Query(True, description="Round-robin across verticals for a balanced feed"),
):
    cache_key = f"articles:ranked:{source or 'all'}:{q or '_'}:{vertical or 'all'}:{diverse}:{limit}:{offset}"
    cached = cache_get(cache_key)
    if cached is not None:
        return cached

    # Diverse mode — round-robin across verticals so no single vertical dominates
    if diverse and not vertical and not source and not q:
        payload = _diverse_feed(db, limit, offset, source, q)
        cache_set(cache_key, payload)
        return payload

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
    payload = _dedup_by_story(rows, offset, limit, str(request.base_url))
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
        d["sources"] = [_source_entry(r.source_name, logos)]
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
        d["sources"] = [_source_entry(r.source_name, logos)]
        payload.append(d)

    cache_set(cache_key, payload)
    return payload


@router.get("/articles/category/{category}", response_model=list[ArticleOut])
def articles_by_category(
    request: Request,
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
    payload = _dedup_by_story(rows, offset, limit, str(request.base_url))
    cache_set(cache_key, payload)
    return payload


@router.get("/articles/ai-images", response_model=list[ArticleOut])
def articles_with_ai_images(
    request: Request,
    db: Session = Depends(get_db),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    vertical: str | None = Query(None, description="Filter by vertical e.g. Layoffs, Hiring, AI"),
):
    """Return articles that have an AI-generated infographic image."""
    logos = get_logo_map()
    base_url = str(request.base_url)

    query = db.query(Article).filter(Article.ai_image_url.isnot(None))
    if vertical:
        query = query.filter(Article.vertical == vertical)

    rows = query.order_by(Article.rank_score.desc()).offset(offset).limit(limit).all()

    payload = []
    for r in rows:
        d = ArticleOut.model_validate(r).model_dump()
        d["source_logo"] = logos.get(r.source_name)
        d["ai_image_url"] = _resolve_ai_image(d.get("ai_image_url"), base_url)
        d["sources"] = [_source_entry(r.source_name, logos)]
        payload.append(d)
    return payload


@router.get("/articles/{article_id}", response_model=ArticleOut)
def get_article(article_id: int, db: Session = Depends(get_db)):
    row = db.get(Article, article_id)
    if not row:
        raise HTTPException(status_code=404, detail="Article not found")
    logos = get_logo_map()
    d = ArticleOut.model_validate(row).model_dump()
    d["source_logo"] = logos.get(row.source_name)
    d["sources"] = [_source_entry(row.source_name, logos)]
    return d


@router.post("/enrich/backfill")
def backfill_enrichment(limit: int = Query(50, ge=1, le=100000)):
    """Enrich up to `limit` unenriched articles in the background."""
    from app.enrichment.enricher import enrich_pending
    import threading
    t = threading.Thread(target=enrich_pending, args=(limit,), daemon=True)
    t.start()
    return {"status": "started", "limit": limit}


@router.post("/enrich/images/reset")
def reset_ai_image_urls(db: Session = Depends(get_db)):
    """Clear ai_image_url on all articles so images can be regenerated."""
    updated = db.query(Article).filter(Article.ai_image_url.isnot(None)).update({"ai_image_url": None})
    db.commit()
    return {"status": "ok", "cleared": updated}


@router.post("/enrich/images/backfill")
def backfill_images(
    limit: int = Query(250, ge=1, le=10000),
    db: Session = Depends(get_db),
):
    """Generate + upload S3 images for all enriched articles missing ai_image_url."""
    import threading, time as _time
    from app.enrichment.enricher import _get_http_client, _generate_article_image

    ids = [
        row.id for row in
        db.query(Article.id)
        .filter(Article.ai_enriched_at.isnot(None), Article.ai_image_url.is_(None))
        .order_by(Article.id.desc())
        .limit(limit)
        .all()
    ]

    def _run(article_ids):
        from app.database import SessionLocal
        from app.enrichment.enricher import _get_http_client, _generate_article_image
        client = _get_http_client()
        if not client:
            return
        session = SessionLocal()
        try:
            articles = session.query(Article).filter(Article.id.in_(article_ids)).all()
            for a in articles:
                path = _generate_article_image(
                    a.id, a.ai_title or a.title, a.vertical or "Tech", a.ai_summary or ""
                )
                if path:
                    a.ai_image_url = path
                    session.commit()
                _time.sleep(2)
        except Exception as e:
            session.rollback()
        finally:
            session.close()

    threading.Thread(target=_run, args=(ids,), daemon=True).start()
    return {"status": "started", "queued": len(ids)}


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

    query = db.query(Article).filter(Article.ai_image_url.is_(None))
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
            article.ai_image_url = url
            db.commit()
            generated += 1
        else:
            failed += 1

    cache_delete_prefix("articles:")
    return {"generated": generated, "failed": failed, "total": len(articles)}
