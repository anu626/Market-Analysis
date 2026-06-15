"""
layoffs_fyi_scraper.py
Scrapes the public layoffs.fyi Airtable into normalized layoff records.

layoffs.fyi has no RSS/official API. Its data is a public Airtable shared view.
Two strategies here:

  1. scrape_with_playwright()  -- RECOMMENDED.
     Loads the real page in a headless browser and captures the
     `readSharedViewData` XHR response. Survives Airtable's token/param
     rotation because the live page generates them. Slower, needs a browser.

  2. scrape_with_requests()    -- FASTER, BRITTLE.
     Calls the readSharedViewData endpoint directly. You must paste in the
     current params from the page network tab (see CONFIG). Breaks whenever
     Airtable rotates those values — expect periodic maintenance.

Both return: list[dict] in this shape (feeds your layoffs pipeline):
  {company, location, count, percent, date, industry, stage, country,
   source, source_url, raw_source: "layoffs.fyi", authority: 0.95}

Setup (his env, not the sandbox):
  pip install playwright requests
  playwright install chromium      # only needed for strategy 1
"""

from __future__ import annotations
import json
import re
import sys
from datetime import datetime, timezone

import requests

# =====================================================
# CONFIG
# Get these from the LIVE page once:
#   1. open https://layoffs.fyi  (it embeds an Airtable shared view)
#   2. open DevTools > Network, filter "readSharedViewData"
#   3. reload; click the request. From it copy:
#        - the shared view id in the URL  (.../view/<viewId>/readSharedViewData)
#        - request header  x-airtable-application-id
#        - the full query string (stringifiedObjectParams, accessPolicy, etc.)
# The SHARE_URL is what Playwright loads; the rest is only for the direct API.
# =====================================================
SHARE_URL = "https://airtable.com/embed/shrLeS9osE5oKM3Iv/tblmlZi3hT0xkXqdK"  # VERIFY on live page
VIEW_ID = "viwTepKZmgZxd4N4P"          # VERIFY — from readSharedViewData URL
APP_ID = "appdcleHb1f1IZkbE"           # VERIFY — from x-airtable-application-id header
ACCESS_POLICY = ""                      # VERIFY — paste accessPolicy query param if present

REQUEST_TIMEOUT = 30
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


# =====================================================
# NORMALIZER  (shared by both strategies)
# Airtable returns cells keyed by COLUMN ID, plus a columns[] map of id->name.
# We build a name->id map and read by human name so this survives column
# reordering. Column names below match layoffs.fyi's public schema; adjust
# if they rename headers.
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
    """name(lower) -> column id"""
    return {c.get("name", "").strip().lower(): c.get("id") for c in columns}


def _resolve(field: str, colmap: dict[str, str]) -> str | None:
    for alias in COLUMN_ALIASES.get(field, []):
        cid = colmap.get(alias.lower())
        if cid:
            return cid
    return None


def _cell(cells: dict, cid: str | None):
    if not cid:
        return None
    v = cells.get(cid)
    # Airtable wraps some types; flatten common shapes
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
    Accepts the parsed readSharedViewData JSON and returns normalized records.
    Defensive: tolerates the two common response shapes Airtable uses.
    """
    data = payload.get("data", payload)
    table = data.get("table", data)
    columns = table.get("columns") or data.get("columns") or []
    rows = (table.get("rows") or data.get("rows")
            or table.get("records") or data.get("records") or [])
    colmap = _build_colmap(columns)

    fields = {f: _resolve(f, colmap) for f in COLUMN_ALIASES}
    out = []
    for row in rows:
        cells = row.get("cellValuesByColumnId") or row.get("fields") or {}
        rec = {
            "company":  _cell(cells, fields["company"]),
            "location": _cell(cells, fields["location"]),
            "count":    _parse_int(_cell(cells, fields["count"])),
            "percent":  _cell(cells, fields["percent"]),
            "date":     _cell(cells, fields["date"]),
            "industry": _cell(cells, fields["industry"]),
            "stage":    _cell(cells, fields["stage"]),
            "country":  _cell(cells, fields["country"]),
            "source_url": _cell(cells, fields["source"]),
            "raw_source": "layoffs.fyi",
            "authority": 0.95,
        }
        if rec["company"]:                       # skip empty rows
            out.append(rec)
    return out


# =====================================================
# STRATEGY 1: Playwright (recommended)
# =====================================================
def scrape_with_playwright(share_url: str = SHARE_URL, headless: bool = True) -> list[dict]:
    from playwright.sync_api import sync_playwright

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
        page.wait_for_timeout(2500)              # let lazy XHRs settle
        browser.close()

    if "payload" not in captured:
        raise RuntimeError(
            "Did not capture readSharedViewData. The share URL or page structure "
            "may have changed — open it in a browser and re-check the network tab."
        )
    return normalize_airtable(captured["payload"])


# =====================================================
# STRATEGY 2: direct API (brittle)
# =====================================================
def scrape_with_requests() -> list[dict]:
    if "VERIFY" in (VIEW_ID + APP_ID) or not VIEW_ID:
        raise RuntimeError("Fill VIEW_ID / APP_ID / ACCESS_POLICY from the live page first.")
    url = f"https://airtable.com/v0.3/view/{VIEW_ID}/readSharedViewData"
    params = {"requestId": "req" + datetime.now().strftime("%H%M%S")}
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
# PUBLIC ENTRYPOINT — tries fast path, falls back to browser
# =====================================================
def scrape_layoffs_fyi() -> list[dict]:
    try:
        if "VERIFY" not in (VIEW_ID + APP_ID):
            return scrape_with_requests()
    except Exception as e:
        print(f"[layoffs.fyi] direct API failed ({e}); falling back to Playwright", file=sys.stderr)
    return scrape_with_playwright()


if __name__ == "__main__":
    records = scrape_layoffs_fyi()
    print(f"Fetched {len(records)} layoff records")
    for r in records[:5]:
        print(json.dumps(r, ensure_ascii=False))
