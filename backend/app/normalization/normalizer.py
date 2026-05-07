"""URL + text normalization. Strips trackers, lowercases host, normalizes path."""

import html
import re
from datetime import datetime, timezone
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

TRACKING_PARAM_PREFIXES = ("utm_", "ga_", "mc_", "fb_", "hsa_", "yclid", "gclid")
TRACKING_PARAM_EXACT = {
    "ref",
    "ref_src",
    "ref_url",
    "fbclid",
    "gclid",
    "igshid",
    "mc_cid",
    "mc_eid",
    "_hsenc",
    "_hsmi",
    "source",
    "campaign_id",
}

WHITESPACE_RE = re.compile(r"\s+")
HTML_TAG_RE = re.compile(r"<[^>]+>")
SUMMARY_MAX_CHARS = 320


def clean_url(url: str) -> str:
    if not url:
        return url
    url = url.strip()
    parsed = urlparse(url)
    if not parsed.scheme or not parsed.netloc:
        return url

    netloc = parsed.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]

    query_pairs = [
        (k, v)
        for k, v in parse_qsl(parsed.query, keep_blank_values=False)
        if not k.lower().startswith(TRACKING_PARAM_PREFIXES)
        and k.lower() not in TRACKING_PARAM_EXACT
    ]
    new_query = urlencode(query_pairs)

    path = parsed.path.rstrip("/") or "/"

    return urlunparse((parsed.scheme.lower(), netloc, path, "", new_query, ""))


def clean_title(title: str) -> str:
    if not title:
        return ""
    title = WHITESPACE_RE.sub(" ", html.unescape(title)).strip()
    return title


def clean_summary(raw: str | None) -> str | None:
    """Strip HTML tags, decode entities, collapse whitespace, truncate."""
    if not raw:
        return None
    text = HTML_TAG_RE.sub(" ", raw)
    text = html.unescape(text)
    text = WHITESPACE_RE.sub(" ", text).strip()
    if not text:
        return None
    if len(text) > SUMMARY_MAX_CHARS:
        text = text[: SUMMARY_MAX_CHARS - 1].rstrip() + "…"
    return text


def to_utc(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc).replace(tzinfo=None)
    return dt.astimezone(timezone.utc).replace(tzinfo=None)


def normalize_item(raw: dict) -> dict | None:
    """Take a raw fetched dict, return normalized dict or None if invalid."""
    title = clean_title(raw.get("title", ""))
    url = clean_url(raw.get("url", ""))
    if not title or not url:
        return None

    return {
        "title": title,
        "url": url,
        "source_name": raw.get("source_name", "unknown"),
        "score": int(raw.get("score") or 0),
        "external_id": str(raw.get("external_id")) if raw.get("external_id") else None,
        "published_at": to_utc(raw.get("published_at")),
        "summary": clean_summary(raw.get("summary")),
    }
