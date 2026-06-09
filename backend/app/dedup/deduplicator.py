"""Two-stage dedup: exact URL match, then fuzzy title similarity (rapidfuzz).

Stage 3 (embedding similarity) is intentionally omitted for the local prototype
to keep the dependency footprint small.
"""

import logging
import re
from datetime import datetime, timedelta

from rapidfuzz import fuzz
from sqlalchemy.orm import Session

from app.models import Article

logger = logging.getLogger(__name__)

TITLE_SIMILARITY_THRESHOLD = 88  # 0-100; tuned for short news headlines
RECENT_WINDOW_DAYS = 7


def _normalize_title_for_compare(title: str) -> str:
    t = title.lower()
    t = re.sub(r"[^a-z0-9 ]", " ", t)
    # unify money phrasing so "$200m" and "usd 200 million" hash identically
    t = re.sub(r"\b(usd|us|rs|inr)\b", "", t)
    t = re.sub(r"\bmillion\b", "m", t)
    t = re.sub(r"\bbillion\b", "b", t)
    t = re.sub(r"\bcrore\b", "cr", t)
    t = re.sub(r"(\d+)\s+(m|b|cr)\b", r"\1\2", t)
    return re.sub(r"\s+", " ", t).strip()


def find_duplicate(db: Session, *, url: str, title: str) -> Article | None:
    """Return existing Article that duplicates this candidate, or None."""
    # Stage 1: exact URL match (DB-side, indexed)
    existing = db.query(Article).filter(Article.url == url).first()
    if existing:
        return existing

    # Stage 2: fuzzy title match within recent window
    cutoff = datetime.utcnow() - timedelta(days=RECENT_WINDOW_DAYS)
    candidates = (
        db.query(Article)
        .filter(Article.created_at >= cutoff)
        .with_entities(Article.id, Article.title, Article.url, Article.source_name)
        .all()
    )
    norm_target = _normalize_title_for_compare(title)
    for c in candidates:
        ratio = fuzz.token_set_ratio(norm_target, _normalize_title_for_compare(c.title))
        if ratio >= TITLE_SIMILARITY_THRESHOLD:
            return db.get(Article, c.id)
    return None


def merge_into_existing(existing: Article, incoming: dict) -> bool:
    """Merge useful fields from incoming into existing. Returns True if changed."""
    changed = False
    incoming_score = int(incoming.get("score") or 0)
    if incoming_score > (existing.score or 0):
        existing.score = incoming_score
        changed = True
    incoming_summary = incoming.get("summary")
    if incoming_summary and not existing.summary:
        existing.summary = incoming_summary
        changed = True
    return changed
