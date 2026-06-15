"""Background article enrichment using Vertex AI (Gemini).

Mirrors the pattern used across naukri-iimjobs-jobseeker services:
  - Auth via GOOGLE_APPLICATION_CREDENTIALS → cred.json
  - Project: naukri-iimjobs-jobseeker, Location: us-central1
  - Model: gemini-2.5-flash-lite
  - Retry with exponential backoff
  - Robust JSON parsing (strip fences, fix common issues)

Flow per article:
  1. Fetch full article HTML via trafilatura
  2. Call Vertex AI with title + content → ai_title + ai_summary
  3. Update article row in DB

Runs in a background thread after each ingestion batch so ingestion
is never blocked.
"""

import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

_CREDS_PATH = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "./cred.json")
_PROJECT    = os.getenv("VERTEX_PROJECT", "naukri-iimjobs-jobseeker")
_LOCATION   = os.getenv("VERTEX_LOCATION", "us-central1")
_MODEL      = os.getenv("VERTEX_MODEL", "gemini-2.5-flash-lite")

_BATCH_SIZE  = int(os.getenv("ENRICH_BATCH_SIZE", "10"))
_RPM_LIMIT   = int(os.getenv("ENRICH_RPM_LIMIT", "15"))
_SLEEP_BETWEEN = 60.0 / _RPM_LIMIT
_MAX_RETRIES = 3

_SYSTEM_PROMPT = """You are a market intelligence editor for Hirist and IIMJobs — India's leading tech job platforms.

Your job is to rewrite article titles and generate summaries that are sharp, specific, and relevant to:
- Tech job seekers (engineers, PMs, designers) tracking the market
- Recruiters and hiring managers watching industry trends

Rules:
- Titles: Be specific. Replace vague phrases with actual names/numbers. Max 90 characters. No clickbait.
- Summaries: 2-3 sentences. Lead with the most important fact. Include company names, numbers, locations where present. End with the implication for hiring or careers.
- Output ONLY valid JSON — no markdown, no explanation.

Output schema:
{"ai_title": "...", "ai_summary": "..."}"""

# ---------------------------------------------------------------------------
# Vertex AI model — initialised once, lazily
# ---------------------------------------------------------------------------

_model = None
_model_lock = threading.Lock()


def _get_model():
    global _model
    if _model is not None:
        return _model
    with _model_lock:
        if _model is not None:
            return _model
        if not os.path.exists(_CREDS_PATH):
            logger.warning("cred.json not found at %s — enrichment disabled", _CREDS_PATH)
            return None
        try:
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = _CREDS_PATH
            import vertexai
            from vertexai.generative_models import GenerativeModel, GenerationConfig
            vertexai.init(project=_PROJECT, location=_LOCATION)
            _model = GenerativeModel(
                model_name=_MODEL,
                generation_config=GenerationConfig(
                    temperature=0.3,
                    max_output_tokens=512,
                    candidate_count=1,
                ),
                system_instruction=_SYSTEM_PROMPT,
            )
            logger.info("Vertex AI model initialised (%s)", _MODEL)
        except Exception as e:
            logger.error("Failed to initialise Vertex AI model: %s", e)
            return None
    return _model


# ---------------------------------------------------------------------------
# JSON helpers — mirrors fixCommonJsonIssues / parseJson from vertex-ai.service.ts
# ---------------------------------------------------------------------------

def _fix_json(s: str) -> str:
    s = re.sub(r",(\s*[}\]])", r"\1", s)        # trailing commas
    s = re.sub(r",\s*}", "}", s)
    s = re.sub(r",\s*]", "]", s)
    return s


def _parse_json(raw: str) -> dict | None:
    # Strip code fences
    text = raw.strip()
    if text.startswith("```json"):
        text = text[7:]
    if text.startswith("```"):
        text = text[3:]
    if text.endswith("```"):
        text = text[:-3]
    text = text.strip()

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    # Try extracting first {...} block
    m = re.search(r"\{[\s\S]*\}", text)
    if m:
        try:
            return json.loads(m.group())
        except json.JSONDecodeError:
            pass
        try:
            return json.loads(_fix_json(m.group()))
        except json.JSONDecodeError:
            pass

    return None


# ---------------------------------------------------------------------------
# Article content fetcher
# ---------------------------------------------------------------------------

def _fetch_content(url: str) -> str | None:
    try:
        import trafilatura
        downloaded = trafilatura.fetch_url(url)
        if not downloaded:
            return None
        text = trafilatura.extract(
            downloaded,
            include_comments=False,
            include_tables=False,
            no_fallback=False,
        )
        if text and len(text) > 100:
            return text[:3000]
    except Exception as e:
        logger.debug("Content fetch failed for %s: %s", url, e)
    return None


# ---------------------------------------------------------------------------
# Core enrichment call — with exponential backoff retry
# ---------------------------------------------------------------------------

def _enrich_one(model, title: str, url: str, existing_summary: str | None) -> tuple[str, str] | None:
    content = _fetch_content(url)

    if content:
        prompt = f"Title: {title}\n\nArticle content:\n{content}"
    elif existing_summary:
        prompt = f"Title: {title}\n\nExcerpt: {existing_summary}"
    else:
        prompt = f"Title: {title}"

    last_err = None
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            response = model.generate_content(prompt)
            raw = response.text or ""
            parsed = _parse_json(raw)
            if parsed and parsed.get("ai_title") and parsed.get("ai_summary"):
                return parsed["ai_title"].strip(), parsed["ai_summary"].strip()
            # Bad format — retry with stricter instruction
            prompt = f"CRITICAL: Return ONLY valid JSON with keys ai_title and ai_summary. No markdown.\n\n{prompt}"
        except Exception as e:
            last_err = e
            if attempt < _MAX_RETRIES:
                time.sleep(1.0 * (2 ** (attempt - 1)))  # 1s, 2s
    logger.warning("Enrichment failed for '%s' after %d attempts: %s", title[:60], _MAX_RETRIES, last_err)
    return None


# ---------------------------------------------------------------------------
# Batch processing
# ---------------------------------------------------------------------------

def enrich_batch(article_ids: list[int]) -> None:
    """Enrich a specific list of article IDs. Runs in its own DB session."""
    from app.database import SessionLocal
    from app.models import Article

    model = _get_model()
    if not model:
        return

    db = SessionLocal()
    try:
        articles = (
            db.query(Article)
            .filter(Article.id.in_(article_ids), Article.ai_enriched_at.is_(None))
            .all()
        )
        for article in articles:
            result = _enrich_one(model, article.title, article.url, article.summary)
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            if result:
                article.ai_title, article.ai_summary = result
            article.ai_enriched_at = now
            db.commit()
            logger.info("Enriched [%d] %s", article.id, article.ai_title or article.title)
            time.sleep(_SLEEP_BETWEEN)
    except Exception as e:
        logger.exception("enrich_batch failed: %s", e)
        db.rollback()
    finally:
        db.close()


def enrich_pending(limit: int = 50) -> None:
    """Enrich oldest unenriched articles — for backfill / manual catch-up."""
    from app.database import SessionLocal
    from app.models import Article

    db = SessionLocal()
    try:
        ids = [
            row.id for row in db.query(Article.id)
            .filter(Article.ai_enriched_at.is_(None))
            .order_by(Article.created_at.desc())
            .limit(limit)
            .all()
        ]
    finally:
        db.close()

    if ids:
        logger.info("Backfill enriching %d articles", len(ids))
        enrich_batch(ids)


def enrich_batch_async(article_ids: list[int]) -> None:
    """Fire-and-forget: spawn a background thread to enrich the given IDs."""
    if not article_ids or not os.path.exists(_CREDS_PATH):
        return
    t = threading.Thread(target=enrich_batch, args=(article_ids,), daemon=True)
    t.start()
