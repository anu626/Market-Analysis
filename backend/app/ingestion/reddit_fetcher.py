"""Reddit ingestion via public .json endpoints. Handles multiple subreddits."""

import logging
from datetime import datetime

import httpx

from app.ingestion.source_loader import sources_of_type, sources_of_type_and_vertical

logger = logging.getLogger(__name__)

_USER_AGENT = "tech-news-aggregator/0.1 (local prototype)"


def _fetch_subreddit(name: str, url: str, vertical: str = 'tech') -> list[dict]:
    try:
        resp = httpx.get(url, headers={"User-Agent": _USER_AGENT}, timeout=10.0)
        resp.raise_for_status()
        payload = resp.json()
    except Exception as e:
        logger.warning("Reddit fetch failed %s: %s", name, e)
        return []

    children = payload.get("data", {}).get("children", []) or []
    items: list[dict] = []
    for c in children:
        d = c.get("data", {}) or {}
        post_url = d.get("url_overridden_by_dest") or d.get("url")
        title = d.get("title")
        if not post_url or not title or d.get("is_self"):
            continue
        ts = d.get("created_utc")
        author = d.get("author") or "unknown"
        num_comments = int(d.get("num_comments") or 0)
        score = int(d.get("score") or 0)
        permalink = d.get("permalink") or ""

        summary = f"Posted by u/{author} · {score} points · {num_comments} comments"
        if permalink:
            summary += f". Discussion: https://reddit.com{permalink}"

        items.append(
            {
                "title": title,
                "url": post_url,
                "source_name": name,
                "score": score,
                "external_id": d.get("id"),
                "published_at": datetime.utcfromtimestamp(ts) if ts else None,
                "summary": summary,
                "vertical": vertical,
            }
        )
    return items


def fetch_reddit(vertical: str | None = None) -> list[dict]:
    base = sources_of_type_and_vertical(vertical, "api") if vertical else sources_of_type("api")
    out: list[dict] = []
    for src in base:
        if "reddit.com" not in src.get("url", ""):
            continue
        items = _fetch_subreddit(src["name"], src["url"], src.get("category") or src.get("vertical", "tech"))
        logger.info("Reddit %s -> %d items", src["name"], len(items))
        out.extend(items)
    return out
