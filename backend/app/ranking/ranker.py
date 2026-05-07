"""HN-style time-decay ranking: score = (upvotes + 1) / (age_hours + 2)^1.5"""

from datetime import datetime

from sqlalchemy.orm import Session

from app.models import Article

GRAVITY = 1.5


def compute_rank(score: int, created_at: datetime, now: datetime | None = None) -> float:
    now = now or datetime.utcnow()
    age_hours = max(0.0, (now - created_at).total_seconds() / 3600.0)
    return (max(0, score) + 1) / ((age_hours + 2) ** GRAVITY)


def recompute_all(db: Session) -> int:
    """Recompute rank_score for every article. Returns count updated."""
    now = datetime.utcnow()
    articles = db.query(Article).all()
    for a in articles:
        a.rank_score = compute_rank(a.score or 0, a.created_at, now=now)
    db.commit()
    return len(articles)
