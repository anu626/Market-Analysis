"""RSS / Google News feed ingestion. Uses feedparser for tolerant parsing."""

import logging
import socket
from datetime import datetime
from time import mktime

import feedparser

from app.ingestion.source_loader import sources_of_type

logger = logging.getLogger(__name__)

_FEED_TIMEOUT = 15  # seconds per feed


def _parse_feed(name: str, url: str, vertical: str = 'tech') -> list[dict]:
    items: list[dict] = []
    old_timeout = socket.getdefaulttimeout()
    try:
        socket.setdefaulttimeout(_FEED_TIMEOUT)
        parsed = feedparser.parse(url, request_headers={"User-Agent": "tech-news-aggregator/0.1"})
    except Exception as e:
        logger.warning("RSS parse failed %s: %s", url, e)
        return items
    finally:
        socket.setdefaulttimeout(old_timeout)

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
                "vertical": vertical,
            }
        )
    return items


def fetch_rss() -> list[dict]:
    out: list[dict] = []
    for feed in sources_of_type("rss", "google_news"):
        items = _parse_feed(feed["name"], feed["url"], feed.get("vertical", "tech"))
        logger.info("RSS %s -> %d items", feed["name"], len(items))
        out.extend(items)
    return out
