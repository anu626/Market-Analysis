"""
pipeline/filters.py
Filter stages for the news feed pipeline.

Flow:  fetch -> normalize -> filter_junk -> dedupe -> categorize -> score -> store

Each item entering these stages is already normalized to:
{
    "title": str, "summary": str | None, "url": str, "published_at": datetime | None,
    "source_name": str, "score": int, "vertical": str,
    "external_id": str | None,
    # optional, set by filter_junk caller:
    "source_authority": float,   # from sources.yaml; defaults to 0.5
    "country": str,              # "IN" etc.; defaults to ""
}
"""

import hashlib
import re
from datetime import datetime, timezone

from rapidfuzz import fuzz

# =====================================================
# STAGE 1: JUNK FILTER  (drop low-quality items)
# =====================================================

BLOCK_KEYWORDS = [
    "sponsored", "press release", "advertorial", "webinar",
    "horoscope", "deal of the day", "coupon", "discount code",
    "best phones under", "smartwatch review",
]

CLICKBAIT_PATTERNS = [
    r"you won'?t believe", r"number \d+ will", r"shocking",
    r"\bthis one trick\b", r"!!+",
]

MIN_TITLE_LEN = 25
MIN_SUMMARY_LEN = 80


def filter_junk(item: dict) -> bool:
    """Return True if item passes (keep), False if junk (drop)."""
    title = (item.get("title") or "").strip()
    summary = (item.get("summary") or "").strip()
    text = f"{title} {summary}".lower()

    if len(title) < MIN_TITLE_LEN:
        return False
    authority = item.get("source_authority", 0.5)
    if len(summary) < MIN_SUMMARY_LEN and authority < 0.8:
        return False
    if any(kw in text for kw in BLOCK_KEYWORDS):
        return False
    if any(re.search(p, title, re.IGNORECASE) for p in CLICKBAIT_PATTERNS):
        return False
    if title.isupper():
        return False
    return True


# =====================================================
# STAGE 2: IN-BATCH DEDUPLICATION
# Complements the DB-backed dedup in dedup/deduplicator.py.
# Runs within a single fetch batch before any DB writes to
# pick the highest-authority version and track coverage count.
# =====================================================

def _normalize_title(title: str) -> str:
    t = title.lower()
    t = re.sub(r"[^a-z0-9 ]", " ", t)
    # unify money phrasing: "$200m" vs "usd 200 million" vs "rs 200 crore"
    t = re.sub(r"\b(usd|us|rs|inr)\b", "", t)
    t = re.sub(r"\bmillion\b", "m", t)
    t = re.sub(r"\bbillion\b", "b", t)
    t = re.sub(r"\bcrore\b", "cr", t)
    t = re.sub(r"(\d+)\s+(m|b|cr)\b", r"\1\2", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _url_hash(item: dict) -> str:
    clean = re.sub(r"[?#].*$", "", item.get("url", ""))
    return hashlib.md5(clean.encode()).hexdigest()


def dedupe_batch(items: list[dict], threshold: int = 82) -> list[dict]:
    """
    Within a single fetch batch, keep the highest-authority copy of each
    story cluster and attach a coverage_count for the ranking boost.
    DB-level dedup (find_duplicate) still runs afterwards.
    """
    items = sorted(items, key=lambda x: -x.get("source_authority", 0.5))
    kept: list[dict] = []
    seen_hashes: set[str] = set()
    seen_titles: list[tuple[str, int]] = []  # (normalized_title, kept_index)

    for item in items:
        h = _url_hash(item)
        if h in seen_hashes:
            continue
        nt = _normalize_title(item.get("title", ""))
        dup_idx = next(
            (i for i, (t, _) in enumerate(seen_titles)
             if fuzz.token_set_ratio(nt, t) >= threshold),
            None,
        )
        if dup_idx is not None:
            kept[seen_titles[dup_idx][1]]["coverage_count"] = (
                kept[seen_titles[dup_idx][1]].get("coverage_count", 1) + 1
            )
            continue
        item.setdefault("coverage_count", 1)
        seen_hashes.add(h)
        seen_titles.append((nt, len(kept)))
        kept.append(item)

    return kept


# =====================================================
# STAGE 3: CATEGORY ROUTER (fine-grained override)
# The existing classifier assigns broad verticals (ai/software/hardware/
# hiring/industry). These rules apply *after* that and can override with
# more specific editorial categories when strong keyword signals are present.
# Checked in priority order — first match wins.
# =====================================================

_CATEGORY_RULES: list[tuple[str, list[str]]] = [
    ("layoffs",      [r"\blayoffs?\b", r"job cuts", r"workforce reduction",
                      r"hiring freeze", r"pink slips?", r"retrench"]),
    ("funding",      [r"raises? \$", r"raises? rs", r"series [a-e]\b", r"seed round",
                      r"funding round", r"valuation of", r"acquires?\b", r"acquisition"]),
    ("hiring",       [r"\bhiring\b", r"to hire \d+", r"recruitment drive",
                      r"salary hike", r"appraisal", r"increment", r"campus placement"]),
    ("ai",           [r"\bai\b", r"\bllm\b", r"\bgpt\b", r"\bgenai\b", r"generative ai",
                      r"machine learning",
                      r"openai|anthropic|deepmind|gemini|claude"]),
    ("skills_tools", [r"version \d+(\.\d+)? release",
                      r"\breleased?\b.*\b(framework|library|sdk)\b",
                      r"open[- ]source", r"github", r"deprecat"]),
]


def categorize_override(item: dict) -> str | None:
    """
    Return a fine-grained category string if a strong keyword matches,
    otherwise return None (caller keeps the classifier's vertical).
    """
    text = f"{item.get('title', '')} {item.get('summary') or ''}".lower()
    for category, patterns in _CATEGORY_RULES:
        if any(re.search(p, text) for p in patterns):
            return category
    return None


# =====================================================
# STAGE 4: SCORING (simple pre-DB pass; ranker.compute_rank
# is the authoritative score used after DB persistence)
# =====================================================

_HALF_LIFE: dict[str, float] = {
    "layoffs": 24, "hiring": 36, "funding": 48, "ai": 48,
    "skills_tools": 96, "blogs_tutorials": 168, "youtube": 96,
}

_INDIA_BOOST = 1.3
_COVERAGE_BOOST = 0.05
_MIN_PUBLISH_SCORE = 0.25


def pre_score(item: dict, now: datetime | None = None) -> float:
    """
    Lightweight score used to filter obviously stale items before DB writes.
    The full multi-signal score is computed by ranker.compute_rank after persist.
    """
    now = now or datetime.now(timezone.utc)
    published = item.get("published_at")
    if published is None:
        return _MIN_PUBLISH_SCORE

    if published.tzinfo is None:
        published = published.replace(tzinfo=timezone.utc)

    age_hours = max((now - published).total_seconds() / 3600, 0)
    half_life = _HALF_LIFE.get(item.get("vertical", ""), 48)
    recency = 0.5 ** (age_hours / half_life)

    s = item.get("source_authority", 0.5) * recency
    if item.get("country") == "IN":
        s *= _INDIA_BOOST
    s *= min(1 + _COVERAGE_BOOST * (item.get("coverage_count", 1) - 1), 1.25)
    return round(s, 4)


# =====================================================
# PIPELINE RUNNER  (in-memory, pre-DB)
# Accepts a list of already-normalized items and returns
# the filtered, deduped, enriched-category list ready for
# DB persistence via ingestion_service._persist_batch.
# =====================================================

def run_pre_db_pipeline(
    items: list[dict],
    now: datetime | None = None,
) -> list[dict]:
    """
    Run junk-filter → in-batch dedupe → category override → pre-score.
    Returns items sorted by pre_score descending, dropping stale ones.
    """
    items = [i for i in items if filter_junk(i)]
    items = dedupe_batch(items)
    for item in items:
        override = categorize_override(item)
        if override:
            item["vertical"] = override
        item["pre_score"] = pre_score(item, now)
    items = [i for i in items if i["pre_score"] >= _MIN_PUBLISH_SCORE]
    return sorted(items, key=lambda x: -x["pre_score"])
