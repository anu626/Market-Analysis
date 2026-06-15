"""Job board ingestion for Greenhouse and Lever APIs (growth signal sources)."""

import logging
from datetime import datetime, timezone

import httpx

from app.ingestion.source_loader import sources_of_type

logger = logging.getLogger(__name__)

_HEADERS = {"User-Agent": "tech-news-aggregator/0.1"}


def _fetch_greenhouse(name: str, url: str, vertical: str = 'both') -> list[dict]:
    try:
        resp = httpx.get(url, headers=_HEADERS, timeout=10.0)
        resp.raise_for_status()
        payload = resp.json()
    except Exception as e:
        logger.warning("Greenhouse fetch failed %s: %s", name, e)
        return []

    items: list[dict] = []
    for job in payload.get("jobs", []):
        title = job.get("title")
        job_url = job.get("absolute_url")
        if not title or not job_url:
            continue

        location = (job.get("location") or {}).get("name", "")
        departments = ", ".join(d.get("name", "") for d in (job.get("departments") or []))
        summary_parts = [f"[Job Opening] {name}"]
        if departments:
            summary_parts.append(departments)
        if location:
            summary_parts.append(location)
        summary = " · ".join(summary_parts)

        updated_raw = job.get("updated_at")
        published_at = None
        if updated_raw:
            try:
                published_at = datetime.fromisoformat(updated_raw).astimezone(timezone.utc).replace(tzinfo=None)
            except Exception:
                pass

        items.append(
            {
                "title": f"[Hiring] {title} at {name}",
                "url": job_url,
                "source_name": name,
                "score": 0,
                "external_id": str(job.get("id")) if job.get("id") else None,
                "published_at": published_at,
                "summary": summary,
            }
        )
    return items


def _fetch_lever(name: str, url: str) -> list[dict]:
    try:
        resp = httpx.get(url, headers=_HEADERS, timeout=10.0)
        resp.raise_for_status()
        payload = resp.json()
    except Exception as e:
        logger.warning("Lever fetch failed %s: %s", name, e)
        return []

    if not isinstance(payload, list):
        return []

    items: list[dict] = []
    for posting in payload:
        title = posting.get("text")
        job_url = posting.get("hostedUrl")
        if not title or not job_url:
            continue

        categories = posting.get("categories") or {}
        department = categories.get("department", "")
        location = categories.get("location", "")
        summary_parts = [f"[Job Opening] {name}"]
        if department:
            summary_parts.append(department)
        if location:
            summary_parts.append(location)
        summary = " · ".join(summary_parts)

        created_ms = posting.get("createdAt")
        published_at = None
        if created_ms:
            try:
                published_at = datetime.fromtimestamp(created_ms / 1000, tz=timezone.utc).replace(tzinfo=None)
            except Exception:
                pass

        items.append(
            {
                "title": f"[Hiring] {title} at {name}",
                "url": job_url,
                "source_name": name,
                "score": 0,
                "external_id": posting.get("id"),
                "published_at": published_at,
                "summary": summary,
            }
        )
    return items


def fetch_json_api() -> list[dict]:
    out: list[dict] = []
    for src in sources_of_type("json_api"):
        url = src["url"]
        name = src["name"]
        if "greenhouse.io" in url:
            items = _fetch_greenhouse(name, url)
        elif "lever.co" in url:
            items = _fetch_lever(name, url)
        else:
            logger.warning("Unknown json_api source, skipping: %s", name)
            continue
        logger.info("JSON API %s -> %d items", name, len(items))
        out.extend(items)
    return out
