"""Hacker News ingestion. Fetches top story IDs then story details concurrently."""

import asyncio
import logging
from datetime import datetime

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


async def _fetch_json(client: httpx.AsyncClient, url: str) -> dict | list | None:
    try:
        resp = await client.get(url, timeout=10.0)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.warning("HN fetch failed %s: %s", url, e)
        return None


async def _fetch_item(client: httpx.AsyncClient, item_id: int) -> dict | None:
    data = await _fetch_json(client, settings.HN_ITEM_URL.format(id=item_id))
    if not data or not isinstance(data, dict):
        return None
    if data.get("type") != "story" or data.get("dead") or data.get("deleted"):
        return None
    url = data.get("url")
    title = data.get("title")
    if not url or not title:
        # Ask/Show HN with no external link — link to HN itself
        if title:
            url = f"https://news.ycombinator.com/item?id={item_id}"
        else:
            return None
    ts = data.get("time")
    by = data.get("by")
    descendants = data.get("descendants") or 0
    score = data.get("score") or 0
    text = data.get("text")  # Ask/Show HN sometimes ships text

    if text:
        summary = text
    else:
        bits = []
        if by:
            bits.append(f"Posted by {by}")
        bits.append(f"{score} points")
        bits.append(f"{descendants} comments")
        summary = " · ".join(bits) + f". Discussion: https://news.ycombinator.com/item?id={item_id}"

    return {
        "title": title,
        "url": url,
        "source_name": "Hacker News",
        "score": score,
        "external_id": str(item_id),
        "published_at": datetime.utcfromtimestamp(ts) if ts else None,
        "summary": summary,
    }


async def fetch_hn_async(limit: int | None = None) -> list[dict]:
    limit = limit or settings.HN_FETCH_LIMIT
    async with httpx.AsyncClient() as client:
        ids = await _fetch_json(client, settings.HN_TOP_STORIES_URL)
        if not ids:
            return []
        ids = ids[:limit]
        sem = asyncio.Semaphore(20)

        async def bounded(i):
            async with sem:
                return await _fetch_item(client, i)

        results = await asyncio.gather(*(bounded(i) for i in ids))
        return [r for r in results if r]


def fetch_hn(limit: int | None = None) -> list[dict]:
    return asyncio.run(fetch_hn_async(limit))
