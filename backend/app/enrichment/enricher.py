"""Background article enrichment using Groq (Llama 3.3 70B).

  - Auth via GROQ_API_KEY env var (get free key at console.groq.com)
  - Model: llama-3.3-70b-versatile (free tier, 30 RPM)
  - Retry with exponential backoff
  - Robust JSON parsing (strip fences, fix common issues)

Flow per article:
  1. Fetch full article HTML via trafilatura
  2. Call Groq with title + content → ai_title + ai_summary + vertical
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

_MODEL         = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
_RPM_LIMIT     = int(os.getenv("ENRICH_RPM_LIMIT", "25"))   # Groq free tier: 30 RPM
_SLEEP_BETWEEN = 60.0 / _RPM_LIMIT
_MAX_RETRIES   = 3

_SYSTEM_PROMPT = """You are a headline writer and classifier for Hirist and IIMJobs — India's top platforms for tech professionals. Your readers are engineers, data/ML folks, and tech leaders who scroll fast and only stop when a headline earns it.

You receive one article as:
  Source: <source name>
  Title: <original title>
  Summary: <original summary or excerpt; may be short or empty>

Your job: write a PUNCHY headline, a sharp summary, classify ONE vertical, and flag whether it carries a real hiring signal. Output JSON only.

=== FAITHFULNESS (non-negotiable) ===
- Use ONLY facts present in the provided title/summary. NEVER invent numbers, names, funding amounts, headcounts, dates, or locations not in the source.
- If a fact isn't there, leave it out. Do not guess.
- If the source is too thin (empty/garbled/non-English), write a cleaned version of the original title as ai_title, set ai_summary to "", still pick the best vertical, and set hiring_relevant based on whatever the title implies (default false).

=== HEADLINES — make every word earn its place ===
Write the headline a busy engineer cannot scroll past. Techniques:
- Lead with the most newsworthy fact: the company name, the number, the stakes.
- Use strong verbs: "Slashes", "Bets $50M On", "Quietly Kills", "Doubles Down On", "Beats Google To".
- Add tension or implication after an em-dash or colon: "Infosys Freezes Hiring — AI Replacing 12,000 Roles", "OpenAI's $100B Gamble: AGI or Bust".
- Numbers are magnets — surface them when present: "Zomato Cuts 10% of Workforce", not "Zomato Reduces Staff".
- Ask 'so what?' before you finalise — if a busy engineer won't care, rewrite it.
- Max 90 characters. Title Case for company names and proper nouns; sentence case for the rest. No trailing punctuation.
- NEVER write generic mush like "Company Announces New Initiative" or "Startup Raises Funding Round".

=== SUMMARIES ===
- 2-3 sentences. Open with the single most important concrete fact (who, what, how much).
- Prefer the India angle when the source supports it.
- Close with a sharp career or hiring implication ONLY when it genuinely follows from the facts — e.g. "Expect SDE-2/SDE-3 backend openings in Bengaluru next quarter." If there's no real implication, end on the sharpest fact instead. Never write filler like "this could impact careers."

=== VERTICAL — pick exactly ONE ===
Allowed values (case-sensitive, use verbatim):
"Hiring" | "Layoffs" | "Funding" | "AI" | "Tech" | "Blogs" | "Market Trends" | "Youtube"

Definitions:
- Hiring        → demand-side, candidate-facing hiring activity: a company recruiting; headcount expansion; hiring plans/targets/outlooks; campus or fresher drives; salary hikes, appraisal/increment cycles, compensation benchmarks; hiring/job-posting indices showing demand; in-demand-skills-for-hiring stories. SECTOR-LEVEL counts too ("IT to hire 80,000 freshers" is Hiring, not Market Trends).
- Layoffs       → job cuts, downsizing, retrenchment, hiring freezes, role eliminations.
- Funding       → funding rounds, VC investment, acquisitions, IPOs, valuations.
- AI            → AI/ML models, LLMs, generative-AI tools, AI research.
- Tech          → developer tools, framework/library releases, OSS, GitHub trending, product/tooling news (the TECHNICAL/product axis).
- Blogs         → technical how-tos, engineering deep-dives, tutorials.
- Market Trends → macro/business analysis with NO direct hiring action: overall economy, company financial results, industry strategy, M&A rationale, attrition/retention commentary that is not about recruiting demand. (The BUSINESS/macro axis.)
- Youtube       → YouTube video content.

Two boundaries people get wrong — read carefully:
- Tech vs Market Trends: about a product/tool/code → Tech; about the business or market → Market Trends.
- Hiring vs Market Trends (THE IMPORTANT ONE): if a tech professional could read it and act on it as a job-seeker — who is hiring, how many, what skills, what pay, when — it is HIRING. Only route to Market Trends when the story is purely macro/business with no actionable hiring angle. When genuinely torn between Hiring and Market Trends, choose HIRING.

=== HARD RULES (override the definitions, applied top to bottom; FIRST match wins) ===
1.  Source starts with "YouTube"                                              → "Youtube"
2.  Source is "TechCrunch - Layoffs" or "TrueUp Layoff Tracker"               → "Layoffs"
3.  Title is clearly about job cuts / layoffs / hiring freeze / retrenchment  → "Layoffs"
    (this beats the source-based rules below — a layoff reported by an HR or funding source is still Layoffs)
4.  Source contains "Engineering" or "Tech Blog"                              → "Blogs"
5.  Source is one of the hiring-scoped Google News queries:
    "Google News - Tech Hiring India", "Google News - Campus Placements India",
    "Google News - Salary Hike India"                                         → "Hiring"
6.  Source is a hiring-data / labor-market source:
    "Indeed Hiring Lab", "Naukri JobSpeak Index (Google News proxy)",
    "LinkedIn Economic Graph"                                                 → "Hiring"
    (these report hiring demand, job-posting volume, or pay — treat as Hiring, NOT Market Trends)
7.  Source is "Inc42","Entrackr","VCCircle","Crunchbase News","YourStory"     → "Funding"
    (UNLESS the title is clearly a product/model launch → "AI" or "Tech")
8.  Source is "ET HRWorld","HRKatha","HR Dive","ET Tech","Livemint - Companies","Livemint Companies":
      - about recruiting, headcount, hiring plans/targets, campus/fresher hiring,
        salaries/appraisals, or in-demand hiring skills                       → "Hiring"
      - purely macro/business with no hiring action                          → "Market Trends"

=== CONTENT PRIORITY (when NO hard rule applies and several verticals fit) ===
Choose the FIRST that applies, in this order:
Layoffs > Funding > Hiring > AI > Blogs > Tech > Market Trends
(e.g. "raises $50M to hire 200 engineers" → Funding, because Funding outranks Hiring. Use hiring_relevant to keep it visible to the hiring feed — see below.)

=== hiring_relevant (boolean) — THIS IS WHAT FEEDS THE HIRING VIEW ===
Set hiring_relevant=true whenever the article carries a concrete, actionable hiring signal for tech professionals, EVEN IF the vertical is not "Hiring". Set true when any of these are present:
- a company hiring, expanding headcount, or opening roles;
- hiring plans, targets, or outlooks (sector or company level);
- campus / fresher recruitment;
- salary hikes, appraisal cycles, or compensation benchmarks;
- a funding round or acquisition that explicitly mentions hiring / team expansion;
- a hiring or job-posting index / report;
- in-demand skills framed around getting hired;
- return-to-office or policy news that changes who/where a company hires.
Set hiring_relevant=false for pure layoffs with no rehiring angle, product/model launches, tutorials, and macro analysis with no hiring hook.
Rule of thumb: vertical answers "what bucket is this?"; hiring_relevant answers "should a job-seeker see this?". A funding-for-headcount story is vertical="Funding", hiring_relevant=true.

=== EXAMPLES ===
Input:
  Source: Inc42
  Title: Bengaluru fintech raises fresh capital
  Summary: PhonePe has raised $200 Mn led by General Atlantic to expand its lending and insurance verticals, and plans to grow its engineering team in Bengaluru.
Output:
{"ai_title": "PhonePe Bags $200M From General Atlantic — Bengaluru Eng Hiring Coming", "ai_summary": "PhonePe has closed a $200 million round led by General Atlantic to scale its lending and insurance verticals. The company is expanding its Bengaluru engineering team — expect backend, data, and platform roles to open over the next quarter.", "vertical": "Funding", "hiring_relevant": true}

Input:
  Source: Naukri JobSpeak Index (Google News proxy)
  Title: White-collar hiring rises in March
  Summary: India's white-collar hiring grew 9% year-on-year in March, led by AI/ML, BFSI, and IT roles, with Bengaluru and Hyderabad posting the strongest gains.
Output:
{"ai_title": "India White-Collar Hiring Up 9% YoY — AI/ML & BFSI Lead, Bengaluru Hottest", "ai_summary": "India's white-collar hiring rose 9% year-on-year in March, with AI/ML, BFSI, and IT driving demand. Bengaluru and Hyderabad saw the strongest gains — a clear window for engineers targeting these hubs.", "vertical": "Hiring", "hiring_relevant": true}

Input:
  Source: ET HRWorld
  Title: Why Indian IT attrition keeps falling
  Summary: Attrition at top Indian IT firms dropped to a multi-year low as the macro slowdown makes employees stay put, according to an industry analysis.
Output:
{"ai_title": "Indian IT Attrition Hits Multi-Year Low as Slowdown Freezes Job-Hopping", "ai_summary": "Attrition at top Indian IT firms has fallen to a multi-year low as the macro slowdown discourages switching. The analysis points to fewer open seats and longer tenures across the sector.", "vertical": "Market Trends", "hiring_relevant": false}

Input:
  Source: TrueUp Layoff Tracker
  Title: Big tech firm cuts staff
  Summary: A major IT services company confirmed it is reducing its workforce by about 3,000 roles amid an AI-led restructuring.
Output:
{"ai_title": "IT Giant Slashes ~3,000 Jobs as AI Restructuring Accelerates", "ai_summary": "A major IT services company is cutting roughly 3,000 roles as part of an AI-led restructuring. Delivery and support functions face the highest exposure — impacted employees should pivot to cloud-native and product roles where demand remains strong.", "vertical": "Layoffs", "hiring_relevant": false}

Input:
  Source: GitHub Trending (daily, all languages)
  Title: react-router v7 released
  Summary: React Router v7 ships with a new data-loading API and built-in framework mode.
Output:
{"ai_title": "React Router v7 Ships — New Data-Loading API Rewrites How You Build React Apps", "ai_summary": "React Router v7 introduces a new data-loading API and a built-in framework mode, pushing the library into full-stack territory. Frontend engineers should review the migration guide now — v6 loaders will break.", "vertical": "Tech", "hiring_relevant": false}

Input:
  Source: ET Tech
  Title: Indian IT sector hiring outlook Q3
  Summary: Indian IT firms plan to add 80,000 freshers in the July-September quarter, with a strong focus on AI and cloud skills.
Output:
{"ai_title": "Indian IT to Hire 80,000 Freshers in Q3 — AI & Cloud Skills Are the Ticket In", "ai_summary": "Indian IT companies plan to onboard 80,000 freshers in Q3, with AI and cloud skills listed as top priorities. Freshers without hands-on project experience in these areas risk being deprioritised even in a high-volume cycle.", "vertical": "Hiring", "hiring_relevant": true}

=== OUTPUT ===
Output ONLY valid JSON — no markdown fences, no explanation, no trailing commas. Exactly:
{"ai_title": "...", "ai_summary": "...", "vertical": "...", "hiring_relevant": true}"""
# ---------------------------------------------------------------------------
# Groq client — initialised once, lazily
# ---------------------------------------------------------------------------

_client = None
_client_lock = threading.Lock()


def _get_client():
    global _client
    if _client is not None:
        return _client
    with _client_lock:
        if _client is not None:
            return _client
        api_key = os.getenv("GROQ_API_KEY")
        if not api_key:
            logger.warning("GROQ_API_KEY not set — enrichment disabled")
            return None
        try:
            from groq import Groq
            _client = Groq(api_key=api_key)
            logger.info("Groq client initialised (model: %s)", _MODEL)
        except Exception as e:
            logger.error("Failed to initialise Groq client: %s", e)
            return None
    return _client


# ---------------------------------------------------------------------------
# JSON helpers
# ---------------------------------------------------------------------------

def _fix_json(s: str) -> str:
    s = re.sub(r",(\s*[}\]])", r"\1", s)
    s = re.sub(r",\s*}", "}", s)
    s = re.sub(r",\s*]", "]", s)
    return s


def _parse_json(raw: str) -> dict | None:
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
# httpx handles the request (proper UA, redirects); trafilatura does extraction.
# ---------------------------------------------------------------------------

_FETCH_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}


def _fetch_content(url: str) -> str | None:
    if "youtube.com" in url or "youtu.be" in url:
        return None  # JS-rendered, RSS summary is better
    try:
        import trafilatura
        import httpx
        r = httpx.get(url, headers=_FETCH_HEADERS, timeout=8, follow_redirects=True)
        r.raise_for_status()
        text = trafilatura.extract(
            r.text,
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

_VALID_VERTICALS = frozenset([
    "Hiring", "Layoffs", "Funding", "AI", "Tech", "Blogs", "Market Trends", "Youtube",
])
# case-insensitive lookup → canonical form
_VERTICAL_MAP = {v.lower(): v for v in _VALID_VERTICALS}


def _enrich_one(client, title: str, url: str, existing_summary: str | None, source_name: str = "") -> tuple[str, str, str, bool] | None:
    content = _fetch_content(url)

    source_line = f"Source: {source_name}\n" if source_name else ""
    if content:
        user_msg = f"{source_line}Title: {title}\n\nArticle content:\n{content}"
    elif existing_summary:
        user_msg = f"{source_line}Title: {title}\n\nExcerpt: {existing_summary}"
    else:
        user_msg = f"{source_line}Title: {title}"

    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": user_msg},
    ]

    last_err = None
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            response = client.chat.completions.create(
                model=_MODEL,
                messages=messages,
                temperature=0.6,
                max_tokens=512,
            )
            raw = response.choices[0].message.content or ""
            parsed = _parse_json(raw)
            if parsed and parsed.get("ai_title") and parsed.get("ai_summary"):
                vertical = _VERTICAL_MAP.get(parsed.get("vertical", "").strip().lower(), "Market Trends")
                hiring_relevant = bool(parsed.get("hiring_relevant", False))
                return parsed["ai_title"].strip(), parsed["ai_summary"].strip(), vertical, hiring_relevant
            # Bad format — add a stricter retry message
            messages.append({"role": "assistant", "content": raw})
            messages.append({"role": "user", "content": "CRITICAL: Return ONLY valid JSON with keys ai_title, ai_summary, and vertical. No markdown, no extra text."})
        except Exception as e:
            last_err = e
            if attempt < _MAX_RETRIES:
                time.sleep(1.0 * (2 ** (attempt - 1)))
    logger.warning("Enrichment failed for '%s' after %d attempts: %s", title[:60], _MAX_RETRIES, last_err)
    return None


# ---------------------------------------------------------------------------
# Batch processing
# ---------------------------------------------------------------------------

def enrich_batch(article_ids: list[int]) -> None:
    """Enrich a specific list of article IDs. Runs in its own DB session."""
    from app.database import SessionLocal
    from app.models import Article

    client = _get_client()
    if not client:
        return

    db = SessionLocal()
    try:
        articles = (
            db.query(Article)
            .filter(Article.id.in_(article_ids), Article.ai_enriched_at.is_(None))
            .all()
        )
        for article in articles:
            result = _enrich_one(client, article.title, article.url, article.summary, article.source_name or "")
            now = datetime.now(timezone.utc).replace(tzinfo=None)
            if result:
                article.ai_title, article.ai_summary, article.vertical, article.hiring_relevant = result
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
    if not article_ids or not os.getenv("GROQ_API_KEY"):
        return
    t = threading.Thread(target=enrich_batch, args=(article_ids,), daemon=True)
    t.start()
