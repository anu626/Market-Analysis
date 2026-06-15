"""RSS / Google News feed ingestion. Uses feedparser for tolerant parsing."""

import logging
from datetime import datetime
from time import mktime

import feedparser
import httpx

from app.ingestion.source_loader import sources_of_type, sources_of_type_and_vertical

logger = logging.getLogger(__name__)

_FEED_TIMEOUT = 15  # seconds per feed
_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36",
    "Accept": "application/rss+xml, application/atom+xml, text/xml, */*",
}


def _fetch_feed(url: str) -> feedparser.FeedParserDict:
    """Fetch via httpx (proper headers, SSL fallback) then parse with feedparser."""
    try:
        r = httpx.get(url, headers=_HEADERS, timeout=_FEED_TIMEOUT, follow_redirects=True)
        r.raise_for_status()
        return feedparser.parse(r.text)
    except httpx.HTTPStatusError:
        raise
    except Exception:
        # SSL cert failure or connection error — retry without verification
        try:
            r = httpx.get(url, headers=_HEADERS, timeout=_FEED_TIMEOUT,
                          follow_redirects=True, verify=False)
            r.raise_for_status()
            return feedparser.parse(r.text)
        except Exception:
            # Last resort: let feedparser use its own fetcher
            return feedparser.parse(url)


def _parse_feed(name: str, url: str, vertical: str = 'tech') -> list[dict]:
    items: list[dict] = []
    try:
        parsed = _fetch_feed(url)
    except Exception as e:
        logger.warning("RSS parse failed %s: %s", url, e)
        return items

    if not parsed or (parsed.bozo and not parsed.entries):
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


def fetch_rss(vertical: str | None = None) -> list[dict]:
    if vertical:
        feeds = sources_of_type_and_vertical(vertical, "rss", "google_news")
    else:
        feeds = sources_of_type("rss", "google_news")
    out: list[dict] = []
    for feed in feeds:
        items = _parse_feed(feed["name"], feed["url"], feed.get("category") or feed.get("vertical", "tech"))
        logger.info("RSS %s -> %d items", feed["name"], len(items))
        out.extend(items)
    return out
