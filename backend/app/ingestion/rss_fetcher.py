"""RSS feed ingestion. Uses feedparser for tolerant parsing."""

import logging
from datetime import datetime
from time import mktime

import feedparser

from app.config import settings

logger = logging.getLogger(__name__)


def _parse_feed(name: str, url: str) -> list[dict]:
    items: list[dict] = []
    try:
        parsed = feedparser.parse(url, request_headers={"User-Agent": "tech-news-aggregator/0.1"})
    except Exception as e:
        logger.warning("RSS parse failed %s: %s", url, e)
        return items

    if parsed.bozo and not parsed.entries:
        logger.warning("RSS feed appears malformed: %s (%s)", url, parsed.bozo_exception)
        return items

    for entry in parsed.entries:
        link = entry.get("link")
        title = entry.get("title")
        if not link or not title:
            continue

        published_at = None
        for field in ("published_parsed", "updated_parsed"):
            ts = entry.get(field)
            if ts:
                try:
                    published_at = datetime.utcfromtimestamp(mktime(ts))
                    break
                except Exception:
                    continue

        summary = (
            entry.get("summary")
            or entry.get("description")
            or (entry.get("content", [{}])[0].get("value") if entry.get("content") else None)
        )

        items.append(
            {
                "title": title,
                "url": link,
                "source_name": name,
                "score": 0,
                "external_id": entry.get("id") or link,
                "published_at": published_at,
                "summary": summary,
            }
        )
    return items


def fetch_rss() -> list[dict]:
    out: list[dict] = []
    for feed in settings.RSS_FEEDS:
        items = _parse_feed(feed["name"], feed["url"])
        logger.info("RSS %s -> %d items", feed["name"], len(items))
        out.extend(items)
    return out
