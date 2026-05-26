"""Hacker News ingestion. Handles Firebase (top/best) and Algolia sources from YAML."""

import asyncio
import logging
from datetime import datetime, timezone

import httpx

from app.config import settings
from app.ingestion.source_loader import sources_of_type

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
        if title:
            url = f"https://news.ycombinator.com/item?id={item_id}"
        else:
            return None
    ts = data.get("time")
    by = data.get("by")
    descendants = data.get("descendants") or 0
    score = data.get("score") or 0
    text = data.get("text")

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
        "published_at": datetime.fromtimestamp(ts, tz=timezone.utc).replace(tzinfo=None) if ts else None,
        "summary": summary,
    }


async def _fetch_firebase_source(
    client: httpx.AsyncClient, source_name: str, list_url: str, limit: int, vertical: str = 'tech'
) -> list[dict]:
    ids = await _fetch_json(client, list_url)
    if not ids or not isinstance(ids, list):
        return []
    ids = ids[:limit]
    sem = asyncio.Semaphore(20)

    async def bounded(i):
        async with sem:
            return await _fetch_item(client, i)

    results = await asyncio.gather(*(bounded(i) for i in ids))
    items = [r for r in results if r]
    # Tag with the specific source name from YAML (e.g. "Hacker News Top")
    for item in items:
        item["source_name"] = source_name
        item["vertical"] = vertical
    return items


async def _fetch_algolia_source(
    client: httpx.AsyncClient, source_name: str, url: str, vertical: str = 'tech'
) -> list[dict]:
    data = await _fetch_json(client, url)
    if not data or not isinstance(data, dict):
        return []
    items: list[dict] = []
    for hit in data.get("hits", []):
        item_url = hit.get("url")
        title = hit.get("title")
        object_id = hit.get("objectID")
        if not title:
            continue
        if not item_url and object_id:
            item_url = f"https://news.ycombinator.com/item?id={object_id}"
        if not item_url:
            continue

        score = hit.get("points") or 0
        num_comments = hit.get("num_comments") or 0
        author = hit.get("author") or "unknown"
        created_at_i = hit.get("created_at_i")

        summary = f"Posted by {author} · {score} points · {num_comments} comments"
        if object_id:
            summary += f". Discussion: https://news.ycombinator.com/item?id={object_id}"

        items.append(
            {
                "title": title,
                "url": item_url,
                "source_name": source_name,
                "score": score,
                "external_id": str(object_id) if object_id else None,
                "published_at": datetime.fromtimestamp(created_at_i, tz=timezone.utc).replace(tzinfo=None) if created_at_i else None,
                "summary": summary,
                "vertical": vertical,
            }
        )
    return items


async def fetch_hn_async() -> list[dict]:
    hn_sources = [
        s for s in sources_of_type("api") if "hacker-news.firebaseio.com" in s.get("url", "") or "hn.algolia.com" in s.get("url", "")
    ]

    out: list[dict] = []
    async with httpx.AsyncClient() as client:
        for src in hn_sources:
            url = src["url"]
            name = src["name"]
            if "hn.algolia.com" in url:
                items = await _fetch_algolia_source(client, name, url, src.get("vertical", "tech"))
            else:
                items = await _fetch_firebase_source(client, name, url, settings.HN_FETCH_LIMIT, src.get("vertical", "tech"))
            logger.info("HN %s -> %d items", name, len(items))
            out.extend(items)
    return out


def fetch_hn() -> list[dict]:
    return asyncio.run(fetch_hn_async())
