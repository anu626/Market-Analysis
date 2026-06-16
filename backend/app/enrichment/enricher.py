"""Background article enrichment using Google Gemini.

  - Auth: GEMINI_API_KEY env var (Google AI Studio key) OR
           service-account JSON at GOOGLE_CRED_PATH (or backend/cred.json)
           with the Generative Language API enabled in the GCP project.
  - Model: gemini-2.0-flash (override via GEMINI_MODEL)
  - Uses httpx directly — no Google SDK version dependency.
  - Retry with exponential backoff
  - Robust JSON parsing (strip fences, fix common issues)
"""

import json
import logging
import os
import re
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

_MODEL            = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
_RPM_LIMIT        = int(os.getenv("ENRICH_RPM_LIMIT", "25"))
_SLEEP_BETWEEN    = 60.0 / _RPM_LIMIT
_MAX_RETRIES      = 3
_GENERATE_IMAGES  = os.getenv("ENRICH_GENERATE_IMAGES", "false").lower() == "true"
_IMAGE_MODEL      = "imagen-3.0-fast-generate-001"

# API key path: Google AI Studio endpoint
_GEMINI_AI_STUDIO_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
# Service account path: Vertex AI endpoint (v1 works for gemini-2.5-flash)
_GEMINI_VERTEX_URL = "https://us-central1-aiplatform.googleapis.com/v1/projects/{project}/locations/us-central1/publishers/google/models/{model}:generateContent"

_DEFAULT_CRED_PATH = str(Path(__file__).parent.parent.parent / "cred.json")

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
# Auth helpers — build an httpx.Client with the right auth header
# ---------------------------------------------------------------------------

_http_client: httpx.Client | None = None
_client_lock = threading.Lock()


_vertex_project: str = ""  # populated when using service account


def _build_http_client() -> httpx.Client | None:
    """Return a shared httpx.Client with Gemini auth pre-wired."""
    global _vertex_project

    api_key = os.getenv("GEMINI_API_KEY")
    if api_key:
        client = httpx.Client(params={"key": api_key}, timeout=30)
        logger.info("Gemini auth: API key (model: %s)", _MODEL)
        return client

    # Service account → Vertex AI with bearer token
    cred_path = os.getenv("GOOGLE_CRED_PATH", _DEFAULT_CRED_PATH)
    if not Path(cred_path).exists():
        logger.warning(
            "GEMINI_API_KEY not set and no cred file at %s — enrichment disabled",
            cred_path,
        )
        return None

    try:
        from google.oauth2 import service_account
        import google.auth.transport.requests as ga_requests

        with open(cred_path) as f:
            _vertex_project = json.load(f).get("project_id", "")

        credentials = service_account.Credentials.from_service_account_file(
            cred_path,
            scopes=["https://www.googleapis.com/auth/cloud-platform"],
        )
        credentials.refresh(ga_requests.Request())

        class _BearerAuth(httpx.Auth):
            def __init__(self, creds):
                self._creds = creds

            def auth_flow(self, request):
                if not self._creds.valid:
                    self._creds.refresh(ga_requests.Request())
                request.headers["Authorization"] = f"Bearer {self._creds.token}"
                yield request

        client = httpx.Client(auth=_BearerAuth(credentials), timeout=60)
        logger.info("Gemini auth: Vertex AI service account (project: %s, model: %s)", _vertex_project, _MODEL)
        return client
    except Exception as e:
        logger.error("Failed to initialise Gemini auth: %s", e)
        return None


def _get_http_client() -> httpx.Client | None:
    global _http_client
    if _http_client is not None:
        return _http_client
    with _client_lock:
        if _http_client is not None:
            return _http_client
        _http_client = _build_http_client()
    return _http_client


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
# ---------------------------------------------------------------------------

_FETCH_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}


_VERTICAL_IMAGE_THEMES = {
    "Layoffs":       "corporate office empty desks, downsizing, professional workplace",
    "Hiring":        "job interview, career growth, recruitment, professional handshake",
    "AI":            "artificial intelligence, neural network visualization, futuristic technology",
    "Funding":       "startup investment, venture capital, business growth charts",
    "Tech":          "software development, coding on laptop, modern technology",
    "Blogs":         "technology writing, engineering team, software architecture",
    "Market Trends": "business analytics, stock market, economy data visualization",
    "Youtube":       "video content creation, camera, social media",
}

_VERTICAL_ACCENT_COLORS = {
    "Layoffs":       (220, 53,  69),
    "Hiring":        (40,  167, 69),
    "AI":            (111, 66,  193),
    "Funding":       (255, 152, 0),
    "Tech":          (13,  110, 253),
    "Blogs":         (32,  201, 151),
    "Market Trends": (108, 117, 125),
    "Youtube":       (255, 0,   0),
}


def _make_catchphrase(client, title: str, vertical: str) -> str:
    """Use Gemini to distill the title into a short 4-6 word punchy phrase."""
    try:
        prompt = (
            f"News headline: \"{title}\"\n\n"
            "Write a SHORT, punchy 4-6 word phrase that captures the core impact of this story. "
            "Think magazine cover copy — bold, urgent, vivid. "
            "Title Case. No punctuation at the end. Return ONLY the phrase, nothing else."
        )
        raw = _call_gemini(client, prompt) or ""
        phrase = raw.strip().strip('"').strip("'").strip()
        if phrase and len(phrase) < 80:
            return phrase
    except Exception:
        pass
    # Fallback: use first 6 words of title
    words = title.split()
    return " ".join(words[:6]) + ("…" if len(words) > 6 else "")


def _overlay_headline(img_bytes: bytes, phrase: str, vertical: str) -> bytes:
    """Overlay a short phrase centered on the image with a clean dark vignette. Returns PNG bytes."""
    from PIL import Image, ImageDraw, ImageFont, ImageFilter
    import io

    img = Image.open(io.BytesIO(img_bytes)).convert("RGBA")
    W, H = img.size

    # Subtle radial-style vignette: darken edges, darkest center-bottom
    vignette = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    vdraw = ImageDraw.Draw(vignette)
    # Uniform semi-dark overlay so text is readable anywhere
    vdraw.rectangle([(0, 0), (W, H)], fill=(0, 0, 0, 110))
    # Extra darkening toward center-vertical for text contrast
    cx, cy = W // 2, H // 2
    for r in range(min(cx, cy), 0, -8):
        alpha = int(60 * (1 - r / min(cx, cy)))
        vdraw.ellipse([(cx - r, cy - r), (cx + r, cy + r)], fill=(0, 0, 0, alpha))

    img = Image.alpha_composite(img, vignette)
    draw = ImageDraw.Draw(img)

    # Load font
    font_large = font_small = None
    for font_path in [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
    ]:
        if Path(font_path).exists():
            font_large = ImageFont.truetype(font_path, size=max(40, H // 12))
            font_small = ImageFont.truetype(font_path, size=max(20, H // 28))
            break

    accent = _VERTICAL_ACCENT_COLORS.get(vertical, (255, 255, 255))

    # Wrap phrase to max 2 lines centered
    margin = int(W * 0.10)
    max_w = W - margin * 2
    words = phrase.split()
    lines, line = [], []
    for word in words:
        test = " ".join(line + [word])
        tw = draw.textlength(test, font=font_large) if font_large else len(test) * 20
        if tw <= max_w:
            line.append(word)
        else:
            if line:
                lines.append(" ".join(line))
            line = [word]
        if len(lines) >= 2:
            break
    if line and len(lines) < 2:
        lines.append(" ".join(line))

    line_h = (font_large.size if font_large else 44) + 12
    text_block_h = len(lines) * line_h
    ty = (H - text_block_h) // 2 - line_h // 4  # slightly above center

    # Accent line above text
    line_y = ty - 20
    line_x1 = W // 2 - 40
    line_x2 = W // 2 + 40
    draw.rectangle([(line_x1, line_y), (line_x2, line_y + 4)], fill=(*accent, 255))

    # Text centered with multi-layer shadow for depth
    for i, ln in enumerate(lines):
        y = ty + i * line_h
        tw = draw.textlength(ln, font=font_large) if font_large else len(ln) * 20
        x = (W - tw) // 2
        for dx, dy, alpha in [(3, 3, 120), (1, 1, 80)]:
            draw.text((x + dx, y + dy), ln, fill=(0, 0, 0, alpha), font=font_large)
        draw.text((x, y), ln, fill=(255, 255, 255, 255), font=font_large)

    # Accent line below text
    below_y = ty + text_block_h + 16
    draw.rectangle([(line_x1, below_y), (line_x2, below_y + 4)], fill=(*accent, 255))

    # Subtle vertical label bottom-right
    label = vertical.upper()
    lw = draw.textlength(label, font=font_small) if font_small else len(label) * 10
    draw.text((W - lw - 20, H - (font_small.size if font_small else 22) - 16), label,
              fill=(*accent, 200), font=font_small)

    out = io.BytesIO()
    img.convert("RGB").save(out, format="PNG", optimize=True)
    return out.getvalue()


def _generate_article_image(article_id: int, title: str, vertical: str) -> str | None:
    """Generate a Gemini Imagen background, overlay headline, save to static/ai-images/."""
    if not _GENERATE_IMAGES:
        return None

    # Call _get_http_client() first — this populates _vertex_project as a side effect
    client = _get_http_client()
    api_key = os.getenv("GEMINI_API_KEY")

    theme = _VERTICAL_IMAGE_THEMES.get(vertical, "technology, business, innovation")
    prompt = (
        f"Editorial news photograph. Visual themes: {theme}. "
        "Wide shot, clean professional composition, photorealistic, "
        "no text, no watermarks, no logos, no people's faces."
    )

    try:
        if api_key:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{_IMAGE_MODEL}:predict?key={api_key}"
            resp = httpx.post(url, json={
                "instances": [{"prompt": prompt}],
                "parameters": {"sampleCount": 1, "aspectRatio": "16:9"},
            }, timeout=30)
        elif client and _vertex_project:
            url = (
                f"https://us-central1-aiplatform.googleapis.com/v1/projects/{_vertex_project}"
                f"/locations/us-central1/publishers/google/models/{_IMAGE_MODEL}:predict"
            )
            resp = client.post(url, json={
                "instances": [{"prompt": prompt}],
                "parameters": {"sampleCount": 1, "aspectRatio": "16:9"},
            })
        else:
            logger.warning("Image generation skipped: no API key and no Vertex project")
            return None

        resp.raise_for_status()
        prediction = resp.json().get("predictions", [{}])[0]
        b64 = prediction.get("bytesBase64Encoded")
        if not b64:
            logger.warning("No image bytes in Imagen response for article %d: %s", article_id, resp.text[:200])
            return None

        import base64
        raw_bytes = base64.b64decode(b64)
        phrase = _make_catchphrase(client, title, vertical)
        logger.info("Catchphrase for article %d: %s", article_id, phrase)
        infographic_bytes = _overlay_headline(raw_bytes, phrase, vertical)

        out_dir = Path(__file__).parent.parent / "static" / "ai-images"
        out_dir.mkdir(exist_ok=True)
        img_path = out_dir / f"{article_id}.png"
        img_path.write_bytes(infographic_bytes)
        logger.info("Generated infographic for article %d -> %s", article_id, img_path.name)
        return f"/static/ai-images/{article_id}.png"
    except Exception as e:
        logger.warning("Image generation failed for article %d: %s", article_id, e)
        return None


def _fetch_content(url: str) -> tuple[str | None, str | None]:
    """Returns (text_content, og_image_url)."""
    if "youtube.com" in url or "youtu.be" in url:
        return None, None
    try:
        import re as _re
        import trafilatura
        r = httpx.get(url, headers=_FETCH_HEADERS, timeout=8, follow_redirects=True)
        r.raise_for_status()

        # Extract og:image from raw HTML
        og_image = None
        m = _re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', r.text, _re.IGNORECASE)
        if not m:
            m = _re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']', r.text, _re.IGNORECASE)
        if m:
            og_image = m.group(1).strip()

        text = trafilatura.extract(
            r.text,
            include_comments=False,
            include_tables=False,
            no_fallback=False,
        )
        return (text[:3000] if text and len(text) > 100 else None), og_image
    except Exception as e:
        logger.debug("Content fetch failed for %s: %s", url, e)
    return None, None


# ---------------------------------------------------------------------------
# Core enrichment call
# ---------------------------------------------------------------------------

_VALID_VERTICALS = frozenset([
    "Hiring", "Layoffs", "Funding", "AI", "Tech", "Blogs", "Market Trends", "Youtube",
])
_VERTICAL_MAP = {v.lower(): v for v in _VALID_VERTICALS}


def _call_gemini(client: httpx.Client, user_msg: str) -> str | None:
    payload = {
        "system_instruction": {"parts": [{"text": _SYSTEM_PROMPT}]},
        "contents": [{"role": "user", "parts": [{"text": user_msg}]}],
        "generationConfig": {"temperature": 0.6, "maxOutputTokens": 2048, "thinkingConfig": {"thinkingBudget": 0}},
    }
    if _vertex_project:
        url = _GEMINI_VERTEX_URL.format(project=_vertex_project, model=_MODEL)
    else:
        url = _GEMINI_AI_STUDIO_URL.format(model=_MODEL)
    resp = client.post(url, json=payload)
    resp.raise_for_status()
    data = resp.json()
    return data["candidates"][0]["content"]["parts"][0]["text"]


def _enrich_one(client: httpx.Client, title: str, url: str, existing_summary: str | None, source_name: str = "") -> tuple[str, str, str, bool, str | None] | None:
    content, og_image = _fetch_content(url)

    source_line = f"Source: {source_name}\n" if source_name else ""
    if content:
        user_msg = f"{source_line}Title: {title}\n\nArticle content:\n{content}"
    elif existing_summary:
        user_msg = f"{source_line}Title: {title}\n\nExcerpt: {existing_summary}"
    else:
        user_msg = f"{source_line}Title: {title}"

    last_err = None
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            raw = _call_gemini(client, user_msg) or ""
            parsed = _parse_json(raw)
            if parsed and parsed.get("ai_title"):
                vertical = _VERTICAL_MAP.get(parsed.get("vertical", "").strip().lower(), "Market Trends")
                hiring_relevant = bool(parsed.get("hiring_relevant", False))
                ai_summary = (parsed.get("ai_summary") or "").strip()
                return parsed["ai_title"].strip(), ai_summary, vertical, hiring_relevant, og_image
            user_msg += "\n\nCRITICAL: Return ONLY valid JSON with keys ai_title, ai_summary, vertical, hiring_relevant. No markdown."
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

    client = _get_http_client()
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
                article.ai_title, article.ai_summary, article.vertical, article.hiring_relevant, og_image = result
                if og_image and not article.image_url:
                    article.image_url = og_image
            if not article.image_url:
                article.image_url = _generate_article_image(
                    article.id,
                    article.ai_title or article.title,
                    article.vertical or "Tech",
                )
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
    has_api_key = bool(os.getenv("GEMINI_API_KEY"))
    has_cred = Path(os.getenv("GOOGLE_CRED_PATH", _DEFAULT_CRED_PATH)).exists()
    if not article_ids or (not has_api_key and not has_cred):
        return
    t = threading.Thread(target=enrich_batch, args=(article_ids,), daemon=True)
    t.start()
