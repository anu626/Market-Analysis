"""Reddit r/programming ingestion via public .json endpoint."""

import logging
from datetime import datetime

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


def fetch_reddit() -> list[dict]:
    headers = {"User-Agent": settings.REDDIT_USER_AGENT}
    try:
        resp = httpx.get(settings.REDDIT_URL, headers=headers, timeout=10.0)
        resp.raise_for_status()
        payload = resp.json()
    except Exception as e:
        logger.warning("Reddit fetch failed: %s", e)
        return []

    children = payload.get("data", {}).get("children", []) or []
    items: list[dict] = []
    for c in children:
        d = c.get("data", {}) or {}
        url = d.get("url_overridden_by_dest") or d.get("url")
        title = d.get("title")
        if not url or not title or d.get("is_self"):
            continue
        ts = d.get("created_utc")
        author = d.get("author") or "unknown"
        num_comments = int(d.get("num_comments") or 0)
        score = int(d.get("score") or 0)
        permalink = d.get("permalink") or ""

        summary_bits = [
            f"Posted by u/{author}",
            f"{score} points",
            f"{num_comments} comments",
        ]
        summary = " · ".join(summary_bits)
        if permalink:
            summary += f". Discussion: https://reddit.com{permalink}"

        items.append(
            {
                "title": title,
                "url": url,
                "source_name": "Reddit r/programming",
                "score": score,
                "external_id": d.get("id"),
                "published_at": datetime.utcfromtimestamp(ts) if ts else None,
                "summary": summary,
            }
        )
    return items
