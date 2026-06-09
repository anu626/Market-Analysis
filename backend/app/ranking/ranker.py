"""Multi-signal ranking.

Weights:
  recency          30%  — freshness with 6-hour half-life decay
  velocity         20%  — how fast the story is spreading (articles in last 4h)
  source_count     20%  — how many distinct outlets covered the story
  topic_relevance  20%  — how well the article fits the platform's focus (ai/software/hardware)
  source_authority 10%  — publisher credibility from sources.yaml

Engagement (HN points / Reddit upvotes) was removed: RSS and JSON API sources
hardcode score=0, so the signal only disadvantages non-HN/Reddit content unfairly.
"""

import hashlib
import math
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from pathlib import Path

import yaml
from sqlalchemy.orm import Session

from app.models import Article

_YAML_PATH = Path(__file__).parent.parent / "config" / "sources.yaml"

_STOPWORDS = frozenset(
    "a an the and or but in on at to for of is are was were be been have has had "
    "do does did will would could should may might can this that with from by as "
    "how what who which when where why its it not no new also just".split()
)

_INDIAN_TOKENS = frozenset(
    "india indian bengaluru bangalore mumbai delhi hyderabad pune chennai kolkata "
    "gurgaon noida zomato swiggy zepto blinkit flipkart meesho paytm phonepe "
    "razorpay cred nykaa ola rapido ixigo makemytrip oyo byju unacademy groww "
    "zerodha dunzo lenskart mamaearth jio airtel infosys wipro tcs hcl hdfc "
    "icici sbi reliance tata mahindra adani bajaj sebi rbi trai meity nifty "
    "sensex nse bse rupee crore lakh startup".split()
)


@lru_cache(maxsize=1)
def _load_source_config() -> tuple[dict, set]:
    """Returns (authority_map, indian_source_names)."""
    try:
        with open(_YAML_PATH) as f:
            data = yaml.safe_load(f)
    except FileNotFoundError:
        return {}, set()

    authority_map: dict[str, float] = {}
    indian_sources: set[str] = set()
    for s in data.get("sources", []):
        name = s.get("name", "")
        authority_map[name] = float(s.get("authority", 0.5))
        if s.get("country", "").upper() == "IN":
            indian_sources.add(name)
    return authority_map, indian_sources


def story_hash(title: str) -> str:
    """12-char hex hash of the top 5 title keywords (order-insensitive)."""
    words = re.sub(r"[^a-z0-9\s]", "", title.lower()).split()
    keywords = sorted(w for w in words if w not in _STOPWORDS and len(w) > 2)[:5]
    return hashlib.md5(" ".join(keywords).encode()).hexdigest()[:12]


# ---------------------------------------------------------------------------
# Individual signal functions — each returns a float in [0, 1]
# ---------------------------------------------------------------------------

def _recency_signal(created_at: datetime, now: datetime) -> float:
    age_hours = max(0.0, (now - created_at).total_seconds() / 3600.0)
    return math.exp(-age_hours / 12.0)  # 12-hour half-life


def _locale_signal(source_name: str, title: str, indian_sources: set) -> float:
    base = 0.7 if source_name in indian_sources else 0.2
    words = set(re.sub(r"[^a-z\s]", "", title.lower()).split())
    entity_boost = min(len(words & _INDIAN_TOKENS) * 0.1, 0.3)
    return min(base + entity_boost, 1.0)


def _source_count_signal(n_sources: int) -> float:
    return min(max(n_sources - 1, 0), 9) / 9.0


def _velocity_signal(recent_count: int) -> float:
    return min(recent_count / 5.0, 1.0)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def compute_rank(
    created_at: datetime,
    source_name: str = "",
    title: str = "",
    source_count: int = 1,
    velocity_count: int = 0,
    now: datetime | None = None,
    published_at: datetime | None = None,
) -> float:
    now = now or datetime.now(timezone.utc).replace(tzinfo=None)

    authority_map = _load_source_config()

    # Use published_at for age so newly-ingested old articles don't score as fresh
    age_ref = published_at if published_at and published_at < created_at else created_at
    recency = _recency_signal(age_ref, now)
    locale = _locale_signal(source_name, title, indian_sources)
    authority = authority_map.get(source_name, 0.5)
    src_count = _source_count_signal(source_count)
    velocity = _velocity_signal(velocity_count)

    return (
        0.30 * recency
        + 0.20 * velocity
        + 0.20 * src_count
        + 0.20 * topic
        + 0.10 * authority
    )


def recompute_all(db: Session) -> int:
    """Batch-recompute rank_score for every article using full story-level stats."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    recent_cutoff = now - timedelta(hours=4)
    articles = db.query(Article).all()

    # Build story-level stats in one pass
    by_hash: dict[str, dict] = defaultdict(lambda: {"sources": set(), "recent": 0})
    for a in articles:
        h = a.story_hash or story_hash(a.title)
        by_hash[h]["sources"].add(a.source_name)
        if a.created_at and a.created_at >= recent_cutoff:
            by_hash[h]["recent"] += 1

    for a in articles:
        h = a.story_hash or story_hash(a.title)
        stats = by_hash[h]
        a.rank_score = compute_rank(
            created_at=a.created_at,
            source_name=a.source_name or "",
            title=a.title or "",
            source_count=len(stats["sources"]),
            velocity_count=stats["recent"],
            now=now,
            published_at=a.published_at,
        )

    db.commit()
    return len(articles)


def _should_highlight(vertical: str, rank_score: float) -> bool:
    """True for high-ranking articles in the platform's core verticals."""
    return vertical in ("ai", "software", "hardware", "hiring") and rank_score >= 0.50
