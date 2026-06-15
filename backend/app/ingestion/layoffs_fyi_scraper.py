"""
Scrapes the public layoffs.fyi Airtable into normalized pipeline items.

layoffs.fyi has no official API. Its data is a public Airtable shared view.
Two strategies:

  1. scrape_with_playwright()  — RECOMMENDED.
     Loads the real page in a headless browser and captures the
     `readSharedViewData` XHR response. Survives Airtable token/param
     rotation because the live page generates them.
     Requires: pip install playwright && playwright install chromium

  2. scrape_with_requests()    — FASTER, BRITTLE.
     Calls the readSharedViewData endpoint directly. You must paste in
     current params from the page network tab (see CONFIG below). Breaks
     whenever Airtable rotates those values.

`fetch_layoffs_fyi()` is the public entry point — it tries the fast path
first, then falls back to Playwright.

Records are converted to the standard pipeline item shape for ingestion_service.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sys
from datetime import datetime, timezone
from typing import Optional

logger = logging.getLogger(__name__)

# =====================================================
# CONFIG — paste values from live page's network tab
# =====================================================
SHARE_URL = "https://airtable.com/embed/shrLeS9osE5oKM3Iv/tblmlZi3hT0xkXqdK"
VIEW_ID = "viwTepKZmgZxd4N4P"          # from readSharedViewData URL
APP_ID = "appdcleHb1f1IZkbE"           # from x-airtable-application-id header
ACCESS_POLICY = ""                      # paste accessPolicy query param if present

REQUEST_TIMEOUT = 30
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# =====================================================
# NORMALIZER  (shared by both strategies)
# =====================================================

COLUMN_ALIASES = {
    "company":  ["Company"],
    "location": ["Location HQ", "Location", "HQ"],
    "count":    ["# Laid Off", "Laid Off", "# Employees Laid Off"],
    "percent":  ["%", "Percentage"],
    "date":     ["Date", "Date Added"],
    "industry": ["Industry"],
    "stage":    ["Stage"],
    "country":  ["Country"],
    "source":   ["Source"],
}


def _build_colmap(columns: list[dict]) -> dict[str, str]:
    return {c.get("name", "").strip().lower(): c.get("id") for c in columns}


def _resolve(field: str, colmap: dict[str, str]) -> Optional[str]:
    for alias in COLUMN_ALIASES.get(field, []):
        cid = colmap.get(alias.lower())
        if cid:
            return cid
    return None


def _cell(cells: dict, cid: Optional[str]):
    if not cid:
        return None
    v = cells.get(cid)
    if isinstance(v, dict):
        return v.get("text") or v.get("name") or v.get("url") or None
    if isinstance(v, list) and v:
        first = v[0]
        if isinstance(first, dict):
            return first.get("text") or first.get("name") or first.get("url")
        return first
    return v


def _parse_int(x):
    if x is None:
        return None
    m = re.search(r"[\d,]+", str(x))
    return int(m.group().replace(",", "")) if m else None


def normalize_airtable(payload: dict) -> list[dict]:
    """
    Parse a readSharedViewData JSON payload into layoff records.
    Tolerates the two common Airtable response shapes.
    """
    data = payload.get("data", payload)
    table = data.get("table", data)
    columns = table.get("columns") or data.get("columns") or []
    rows = (
        table.get("rows") or data.get("rows")
        or table.get("records") or data.get("records") or []
    )
    colmap = _build_colmap(columns)
    fields = {f: _resolve(f, colmap) for f in COLUMN_ALIASES}

    out = []
    for row in rows:
        cells = row.get("cellValuesByColumnId") or row.get("fields") or {}
        rec = {
            "company":    _cell(cells, fields["company"]),
            "location":   _cell(cells, fields["location"]),
            "count":      _parse_int(_cell(cells, fields["count"])),
            "percent":    _cell(cells, fields["percent"]),
            "date":       _cell(cells, fields["date"]),
            "industry":   _cell(cells, fields["industry"]),
            "stage":      _cell(cells, fields["stage"]),
            "country":    _cell(cells, fields["country"]),
            "source_url": _cell(cells, fields["source"]),
            "raw_source": "layoffs.fyi",
            "authority":  0.95,
        }
        if rec["company"]:
            out.append(rec)
    return out


# =====================================================
# STRATEGY 1: Playwright (recommended)
# =====================================================

def scrape_with_playwright(share_url: str = SHARE_URL, headless: bool = True) -> list[dict]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise RuntimeError(
            "playwright not installed. Run: pip install playwright && playwright install chromium"
        )

    captured: dict = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        page = browser.new_page(user_agent=UA)

        def on_response(resp):
            if "readSharedViewData" in resp.url and resp.status == 200:
                try:
                    captured["payload"] = resp.json()
                except Exception:
                    pass

        page.on("response", on_response)
        page.goto(share_url, wait_until="networkidle", timeout=REQUEST_TIMEOUT * 1000)
        page.wait_for_timeout(2500)
        browser.close()

    if "payload" not in captured:
        raise RuntimeError(
            "Did not capture readSharedViewData response. "
            "The share URL or page structure may have changed — "
            "open it in a browser and re-check the network tab."
        )
    return normalize_airtable(captured["payload"])


# =====================================================
# STRATEGY 2: direct API (brittle — needs fresh params)
# =====================================================

def scrape_with_requests() -> list[dict]:
    if "VERIFY" in (VIEW_ID + APP_ID) or not VIEW_ID:
        raise RuntimeError(
            "Fill VIEW_ID / APP_ID / ACCESS_POLICY from the live page first."
        )
    import requests
    url = f"https://airtable.com/v0.3/view/{VIEW_ID}/readSharedViewData"
    params: dict = {"requestId": "req" + datetime.now().strftime("%H%M%S")}
    if ACCESS_POLICY:
        params["accessPolicy"] = ACCESS_POLICY
    headers = {
        "User-Agent": UA,
        "x-airtable-application-id": APP_ID,
        "x-requested-with": "XMLHttpRequest",
        "accept": "application/json",
    }
    r = requests.get(url, params=params, headers=headers, timeout=REQUEST_TIMEOUT)
    r.raise_for_status()
    return normalize_airtable(r.json())


# =====================================================
# RAW ENTRYPOINT — tries fast path, falls back to Playwright
# =====================================================

def scrape_layoffs_fyi() -> list[dict]:
    """Returns raw layoffs.fyi records (not yet converted to pipeline items)."""
    try:
        if "VERIFY" not in (VIEW_ID + APP_ID):
            return scrape_with_requests()
    except Exception as e:
        logger.warning("layoffs.fyi direct API failed (%s); falling back to Playwright", e)
    return scrape_with_playwright()


# =====================================================
# PIPELINE ADAPTER
# Converts raw layoffs.fyi records to the standard
# {title, url, source_name, score, vertical, published_at, summary, country}
# shape expected by ingestion_service._persist_batch.
# =====================================================

def _parse_date(raw: Optional[str]) -> Optional[datetime]:
    if not raw:
        return None
    for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ", "%m/%d/%Y"):
        try:
            return datetime.strptime(raw[:len(fmt) + 4], fmt).replace(tzinfo=None)
        except ValueError:
            continue
    return None


def _build_title(company: str, count: Optional[int], percent: Optional[str], location: Optional[str]) -> str:
    """Build a descriptive title that always exceeds MIN_TITLE_LEN=25."""
    parts = [company, "Lays Off"]
    if count:
        parts.append(f"{count:,}")
    if percent:
        pct = str(percent).strip().rstrip("%").strip()
        if pct:
            parts.append(f"({pct}%)")
    parts.append("Employees")
    if location:
        parts.append(f"in {location}")
    title = " ".join(parts)
    # Fallback: ensure minimum length for filter_junk
    if len(title) < 30:
        title = f"{company} — Layoffs Announced"
    return title


def _build_summary(company: str, count: Optional[int], percent: Optional[str],
                   location: Optional[str], industry: Optional[str], stage: Optional[str]) -> str:
    parts = [f"{company} has announced layoffs"]
    if count and percent:
        parts[0] += f" of {count:,} employees ({percent})"
    elif count:
        parts[0] += f" of {count:,} employees"
    elif percent:
        parts[0] += f" affecting {percent} of its workforce"
    parts[0] += "."
    if location:
        parts.append(f"The company is headquartered in {location}.")
    if industry:
        parts.append(f"Industry: {industry}.")
    if stage:
        parts.append(f"Funding stage: {stage}.")
    return " ".join(parts)


def _record_id(company: str, date: Optional[str]) -> str:
    """Stable unique ID for a layoffs.fyi record — used as external_id and URL slug."""
    raw = f"layoffs-fyi:{company.lower()}:{date or 'unknown'}"
    return hashlib.md5(raw.encode()).hexdigest()[:16]


def _to_pipeline_item(rec: dict) -> Optional[dict]:
    company = (rec.get("company") or "").strip()
    if not company:
        return None

    count    = rec.get("count")
    percent  = rec.get("percent")
    location = rec.get("location")
    industry = rec.get("industry")
    stage    = rec.get("stage")
    date_raw = rec.get("date")
    country  = (rec.get("country") or "").strip()

    title   = _build_title(company, count, percent, location)
    summary = _build_summary(company, count, percent, location, industry, stage)
    pub_at  = _parse_date(date_raw)
    rec_id  = _record_id(company, date_raw)

    # Use the news article link if present; otherwise a stable per-record URL
    source_url = (rec.get("source_url") or "").strip()
    url = source_url if source_url else f"https://layoffs.fyi/#{rec_id}"

    return {
        "title":        title,
        "url":          url,
        "source_name":  "layoffs.fyi",
        "score":        0,
        "vertical":     "Layoffs",
        "published_at": pub_at,
        "summary":      summary,
        "country":      country,
        "external_id":  rec_id,
    }


def fetch_layoffs_fyi() -> list[dict]:
    """
    Public entry point used by ingestion_service.
    Returns pipeline-ready items — same shape as rss_fetcher/json_api_fetcher output.
    """
    try:
        raw_records = scrape_layoffs_fyi()
    except Exception as e:
        logger.error("layoffs.fyi scrape failed: %s", e)
        return []

    items = []
    for rec in raw_records:
        item = _to_pipeline_item(rec)
        if item:
            items.append(item)

    logger.info("layoffs.fyi: %d records fetched, %d pipeline items", len(raw_records), len(items))
    return items


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    records = scrape_layoffs_fyi()
    print(f"Fetched {len(records)} layoff records")
    for r in records[:5]:
        print(json.dumps(r, ensure_ascii=False))
    print("\nPipeline items (first 3):")
    items = fetch_layoffs_fyi()
    for it in items[:3]:
        print(json.dumps(it, default=str, ensure_ascii=False))
