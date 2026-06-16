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

# S3 config for ai-images
_S3_BUCKET        = os.getenv("AWS_S3_BUCKET", "qahiristmedia")
_S3_REGION        = os.getenv("AWS_S3_REGION", "ap-south-1")
_S3_PREFIX        = "ai-images/"
_S3_BASE_URL      = f"https://{_S3_BUCKET}.s3.{_S3_REGION}.amazonaws.com/"

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


_VERTICAL_SCENE_VARIANTS: dict[str, list[str]] = {
    "Layoffs": [
        "Wide shot of an empty open-plan office with cleared desks and stacked cardboard boxes near the exit, harsh overhead fluorescent lighting",
        "A single wilting plant left on an otherwise bare desk in a dim corporate office, chairs pushed in, no personal items remaining",
        "Rows of disconnected monitors with tangled cables draped over vacated workstations in a large tech floor",
        "A corporate security badge and expired access card lying on an empty desk under cool office lighting",
        "A conference room with chairs pushed in neatly, whiteboard half-erased, lights switched off, blinds half-drawn",
        "Corporate lobby wall with half-removed employee portrait photos, some frames still hanging crookedly",
        "Aerial shot of a tech campus parking lot mostly empty on a weekday morning, only a few scattered cars",
        "A stack of sealed manila envelopes on an HR desk, organised in rows, waiting to be distributed",
        "Server room with decommissioned equipment racks, cables unplugged and hanging loose, dust on empty shelves",
        "Employee notice board with all job postings torn down, only pushpin holes and torn paper corners remain",
        "A coffee mug and a framed family photo left behind on an otherwise completely cleared office desk",
        "Locked glass office doors with dim interiors visible through frosted panels, out-of-order sign taped to handle",
        "A collection point desk stacked with returned company laptops in neat rows, a sign-off sheet on top",
        "Empty canteen tables in a once-busy tech office cafeteria, trays stacked, chairs upturned, lights off at one end",
        "Overhead aerial view of a corporate campus with most parking spots empty, a few cars clustered near one entrance",
        "Moving boxes stacked in a corporate hallway beside a vacated team bay, desks already stripped of equipment",
        "A severance letter on a polished desk next to a company laptop and ID badge, ready for return",
        "Abandoned call centre floor with headsets hanging on empty chairs, screens dark, dead plants on partition tops",
        "A departures board in a corporate atrium listing names and final dates, several entries highlighted",
        "Exterior of a corporate tower at dusk with entire floors dark, only a few windows lit, subdued atmosphere",
    ],
    "Hiring": [
        "A confident professional handshake across a modern glass desk in a bright, plant-filled office",
        "Job fair with candidates in business attire browsing company booths, banner signage overhead",
        "A candidate reviewing their resume on a tablet in a modern waiting room with other applicants seated",
        "A recruiter reviewing a stack of printed CVs at a well-lit office desk, red pen in hand",
        "Laptop screen showing a smiling remote candidate during a video interview, interviewer's hands visible",
        "Campus recruitment drive with company banners and students filling a college auditorium, brochures on desks",
        "A welcoming desk with a 'New Joiners' sign, gift bags and onboarding kits arranged neatly",
        "A hiring manager writing a job offer letter with a fountain pen on company letterhead",
        "HR dashboard on a wide monitor showing open positions, applicant pipeline, and department headcount",
        "A bright modern office lobby where candidates sign in at a reception desk, lanyards ready",
        "Aerial shot of a recruitment fair with long queues at major tech company booths inside a convention hall",
        "A stack of white offer letter envelopes aligned on a desk, ready for dispatch, company logo embossed",
        "New employee orientation — a group of hires in a circle with a facilitator, nametags and notebooks in hand",
        "Close-up of a laptop showing a LinkedIn profile with connection requests from recruiters",
        "A candidate shaking hands with a three-person interview panel in a formal glass-walled meeting room",
        "University career centre with students lining up at placement desks, notice boards full of company flyers",
        "A Bengaluru tech park entrance in morning light with job seekers arriving and checking their phones",
        "HR professional adding a new role to a job board displayed on a large digital screen in a co-working space",
        "Diverse group of new hires collaborating around a table during their first week, laptops and coffee cups",
        "Two professionals across a table reviewing printed compensation packages during a salary negotiation",
    ],
    "AI": [
        "Rows of glowing blue server racks stretching into the distance inside a dark hyperscale data centre",
        "Abstract 3D neural network with thousands of interconnected luminous nodes floating in deep space",
        "A robotic arm precisely placing microchips on a circuit board in a cleanroom under clinical white lighting",
        "Lines of AI model output streaming on a dark terminal screen, green monospace text on black",
        "A translucent holographic human brain made of light filaments suspended in a dark room",
        "Aerial top-down view of a massive GPU cluster inside a data centre, symmetrical rows of hardware",
        "Extreme macro close-up of a processor chip, golden circuitry reflecting dramatic side-lighting",
        "A monitor displaying token-by-token text generation with probability scores highlighted alongside",
        "Fibre-optic cables carrying pulses of coloured light through a long dark server corridor",
        "Futuristic operations centre with curved screens showing AI inference dashboards and live model metrics",
        "Frost-covered cooling pipes running along server racks, condensation visible in cold data centre air",
        "Abstract digital particles gradually assembling into the outline of a brain in deep blue and violet tones",
        "An AI chip wafer held under laboratory lighting, intricate circuit patterns refracting rainbow colours",
        "Rows of specialised AI accelerator boards mounted in racks, status LEDs blinking in synchrony",
        "A lone researcher's silhouette at a dark desk, face illuminated only by a monitor running model training",
        "A 3D scatter plot of high-dimensional embeddings on a monitor, clusters colour-coded by category",
        "A mechanical robotic hand reaching toward a softly glowing translucent sphere in a dark studio",
        "Satellite dish array at night, pointing skyward, lit by moonlight and blue LED ground lighting",
        "Motion-blur time-lapse of data flowing through transparent network cables, light pulses streaking",
        "Macro photograph of a silicon wafer under diffused light, refracting an iridescent spectrum of colour",
    ],
    "Funding": [
        "A venture capitalist signing a term sheet across a polished boardroom table, city skyline behind glass",
        "A startup founder pitching on a spotlit stage to a dark auditorium full of attentive investors",
        "Close-up of a monitor showing an exponential revenue growth bar chart with a cursor highlighting the peak",
        "Two executives shaking hands at floor-to-ceiling glass windows, reflected city skyline between them",
        "Stack of legal contracts and a fountain pen on a mahogany boardroom desk under warm lamp light",
        "A whiteboard covered with a startup's business model canvas and funding milestone markers",
        "Aerial shot of a modern tech campus expansion under active construction, cranes visible above roofline",
        "Corporate lobby digital display showing a funding announcement, employees gathered around reading it",
        "Two co-founders toasting with sparkling water in front of a whiteboard dense with financial projections",
        "A pitch deck slide projected in a dark room, '$50M Series B' in large bold text lit from below",
        "Investor portfolio dashboard on a curved ultrawide monitor, valuation and growth metrics filling the screen",
        "A cheque being carefully signed in a formal financial meeting room, witness present across the table",
        "A co-working space glass meeting room with investors leaning forward, listening to a founder present",
        "IPO debut on a stock exchange ticker — green numbers, celebration confetti visible at the edge of frame",
        "A startup office buzzing with energy post-announcement, team clustered around a printed press release",
        "Lawyer and founder reviewing due diligence documents spread across a glass conference table",
        "A post-funding strategy wall covered in sticky notes mapping go-to-market stages and hiring targets",
        "Private equity firm entrance — frosted glass door with logo, city skyline reflected in the building facade",
        "Aerial drone shot of a newly acquired headquarters building, company flag being raised at entrance",
        "A financial term sheet covered in red-ink annotations under warm incandescent office lighting",
    ],
    "Tech": [
        "Developer at a dual-monitor setup, VS Code open with a dark theme, coffee mug and mechanical keyboard",
        "Extreme close-up of keycaps on a mechanical keyboard, code reflected faintly in the monitor behind",
        "A GitHub pull request diff page in a browser, green additions and red deletions clearly visible",
        "GitHub trending page on a monitor, a repository star count ticking upward in real time",
        "A terminal window mid-package installation, dependency tree scrolling in monospace text",
        "Kubernetes dashboard in a browser showing container pods across a cluster, resource usage graphs",
        "A developer's den — technical books stacked beside a laptop, sticky notes on monitor bezel",
        "CI/CD pipeline diagram on a whiteboard, each build stage labelled with sticky notes and arrows",
        "Close-up of a Raspberry Pi board on a workbench, surrounded by jumper wires and breadboards",
        "A developer testing a web app across phone, tablet, and laptop simultaneously on a desk",
        "Microservices architecture diagram drawn with markers on a glass office wall, team gathered around it",
        "Dark-mode IDE with a component tree panel, props inspector, and live preview split across three panes",
        "Network operations centre with a topology map glowing on a large screen, on-call engineer visible",
        "Terminal showing a successful build — green checkmarks and 'Build passed' in bold monospace text",
        "API documentation in a browser tab alongside a Postman window with a 200 OK response highlighted",
        "A 3D printer building a circuit board prototype on a bench in a hardware startup's lab",
        "Browser DevTools performance panel open, waterfall chart showing a page load timeline in detail",
        "Pair programming — two developers sharing a workstation, one pointing at the screen, collaborative mood",
        "Hackathon setting — laptops, energy drinks, sticky notes covering every surface, overhead lighting harsh",
        "Cloud architecture diagram on a projector screen during a technical design review, team with laptops",
    ],
    "Blogs": [
        "A developer's tidy desk at night — notebook, coffee, a markdown editor open with a half-written post",
        "A whiteboard dense with system design diagrams, arrows, and margin notes being used to draft a blog post",
        "Close-up of hands typing on a laptop, a markdown editor visible with formatted headers and code blocks",
        "An engineering team crowded around a monitor, one person narrating while reviewing a draft technical post",
        "A browser showing a technical article with syntax-highlighted code blocks and diagram illustrations",
        "A conference speaker's handwritten notes spread across a desk alongside a laptop mid-draft",
        "Stack of printed pages with dense margin annotations, being organised for a long-form engineering piece",
        "Home office at 2 AM — monitor glowing with a draft blog post, rest of the room dark and quiet",
        "A large technical mind map on paper, pen still in hand, arrows connecting architecture concepts",
        "A developer recording their screen for a blog post walkthrough, phone propped up as a second camera",
        "O'Reilly and technical books lined on a shelf beside a laptop running a static site generator preview",
        "A distributed system diagram annotated with coloured markers — the base illustration for an engineering post",
        "A pen-and-paper database schema sketch about to be photographed for a blog post illustration",
        "A quiet library corner, a developer taking notes from documentation with a physical notebook and pen",
        "A Slack thread printed and annotated, being prepared as source material for a postmortem blog post",
        "Two co-authors reviewing a shared Google Doc on separate laptops, tracking changes visible in sidebar",
        "A terminal showing benchmark comparison results — raw numbers being formatted for a performance article",
        "A GitHub README being written, diagrams and code examples laid out in a split editor view",
        "A developer's hand annotating a printed stack trace with a red marker for a debugging walkthrough post",
        "An engineering all-hands slide deck open on screen, being adapted and repurposed into a blog article",
    ],
    "Market Trends": [
        "A financial analyst studying four data screens showing market indices on a trading floor",
        "Aerial view of a dense commercial district at golden hour, representing economic activity and scale",
        "A bar chart showing year-on-year tech sector growth projected on a conference room screen",
        "Close-up of a business newspaper front page with India IT sector headlines, coffee cup beside it",
        "A Bloomberg terminal displaying live market data in a dim analyst's office, reflections on the screen",
        "An economist presenting GDP growth slides to a corporate boardroom audience, charts behind them",
        "A large LED wall displaying a market share pie chart during an industry analyst briefing",
        "Overhead shot of an annual report spread open on a glass table, financial figures highlighted in yellow",
        "A think-tank meeting room — analysts debating around a table with data projected on the wall",
        "Satellite image of Bengaluru's tech corridor showing the density of campuses, roads, and infrastructure",
        "Stock exchange trading floor during peak session, every screen filled with live price data, traders focused",
        "A consultant's desk with a thick sector report, key paragraphs highlighted, handwritten margin notes",
        "Heatmap of global tech hiring demand on a research analytics dashboard, India glowing bright",
        "An executive reading a sector outlook report in a quiet lounge, charts visible, pen poised for notes",
        "A startup ecosystem map of India pinned to a wall — cities, sectors, and funding flows marked",
        "A finance team reviewing quarterly results on a shared dashboard in a glass-walled open office",
        "India digital economy growth infographic on a wall display, trend line curving steeply upward",
        "A research analyst's dual-monitor setup showing regression models and a scatter plot of trend data",
        "Printed NASSCOM or sector report open on a desk, key statistics circled in red pen",
        "A CEO being interviewed in a glass office, large chart on the wall behind showing industry performance",
    ],
    "Youtube": [
        "A professional video recording setup — DSLR on tripod, ring light, acoustic foam panels on the wall",
        "Extreme close-up of a camera lens, a blurred recording studio softly visible in the bokeh behind",
        "A creator editing a tech tutorial in a video editor on a wide ultrawide monitor, timeline visible below",
        "A condenser microphone in sharp focus in the foreground, softbox light illuminating a clean desk behind",
        "Behind-the-scenes of a tech channel shoot — teleprompter, multi-camera rig, studio lighting overhead",
        "A thumbnail design workspace — Photoshop open with a bold YouTube thumbnail in progress, layers panel",
        "A dark studio with RGB LED strips along the ceiling, video editing timeline glowing on the screen",
        "A 'Recording' red indicator light glowing in a dimly lit professional video studio",
        "A content creator's desk with analytics dashboards on two monitors showing views, CTR, and watch time",
        "A green screen setup in a home studio — camera on tripod, key light and fill light positioned carefully",
        "A tripod-mounted camera aimed at a whiteboard, marker and eraser resting on the ledge, ready to film",
        "Close-up of a Stream Deck controller with labelled shortcut buttons for a live streaming workflow",
        "A dual-monitor live streaming setup — one screen showing the stream output, one showing scene controls",
        "A creator under bright product lighting filming an unboxing, tech device partially unwrapped on desk",
        "Aerial view of a podcast table — two microphones, headphones, water bottles, notebooks, and a mixer",
        "A YouTube Studio dashboard on screen, subscriber growth chart climbing, latest video thumbnail visible",
        "A creator reviewing raw footage on a laptop in a dimly lit editing suite, headphones on, focused",
        "A teleprompter reflecting script text beside a camera aimed at a neatly dressed presenter's chair",
        "Wide shot of a YouTube filming setup in a bright co-working space, natural light from large windows",
        "A creator's production mood board — script notes, shot list, reference frames, and storyboard pinned up",
    ],
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
        raw = _call_gemini_raw(client, prompt) or ""
        phrase = raw.strip().strip('"').strip("'").strip()
        if phrase and len(phrase) < 80:
            return phrase
    except Exception:
        pass
    # Fallback: use first 6 words of title
    words = title.split()
    return " ".join(words[:6]) + ("…" if len(words) > 6 else "")


def _make_image_prompt(client, title: str, vertical: str, article_id: int, summary: str = "") -> str:
    """Use Gemini to write a story-specific Imagen prompt in infographic/blog-thumbnail style."""
    variants = _VERTICAL_SCENE_VARIANTS.get(vertical, _VERTICAL_SCENE_VARIANTS["Tech"])
    fallback_icons = variants[article_id % len(variants)]

    accent = _VERTICAL_ACCENT_COLORS.get(vertical, (13, 110, 253))
    accent_hex = "#{:02x}{:02x}{:02x}".format(*accent)

    context = f"Headline: \"{title}\""
    if summary:
        context += f"\nSummary: {summary}"

    try:
        gemini_prompt = (
            f"{context}\n"
            f"Category: {vertical}\n"
            f"Accent color: {accent_hex}\n\n"
            "You are a graphic designer creating a blog thumbnail for a tech news site. "
            "Generate an Imagen prompt for a DESIGNED INFOGRAPHIC THUMBNAIL — NOT a photograph.\n\n"
            "Style reference: dark navy or dark grey background, bold illustrated 3D icons relevant to the story "
            "(robots, brain, charts, buildings, laptops, coins, etc.), vibrant neon accent colors, "
            "clean modern layout, flat/semi-3D illustration style, like a professional blog cover image.\n\n"
            "For THIS specific story, decide:\n"
            "- What 2-3 illustrated icons or visual elements best represent the topic? "
            "  (e.g. for AI model launch: glowing brain + neural network; for layoffs: empty office chair + door; "
            "  for funding: coin stack + upward arrow + rocket; for hiring: handshake + resume + briefcase)\n"
            "- What background gradient or pattern fits the mood? "
            "  (dark navy with cyan grid for tech, dark red with cracks for layoffs, "
            "  dark green with upward lines for hiring, purple with stars for AI)\n"
            "- What accent color pops against the dark background?\n\n"
            "Hard rules:\n"
            "- ABSOLUTELY NO TEXT, NO LETTERS, NO WORDS, NO NUMBERS, NO SYMBOLS anywhere in the image — not even decorative or blurred\n"
            "- NO logos, NO watermarks, NO human faces\n"
            "- Illustrated / flat-design / 3D render style — NOT a photograph\n"
            "- 16:9 aspect ratio, high detail, vibrant colors, professional blog thumbnail quality\n"
            "- Only icons, shapes, objects, and abstract elements — zero text of any kind\n"
            "- Return ONLY the final Imagen prompt sentence. No explanation."
        )
        raw = _call_gemini_raw(client, gemini_prompt, temperature=0.9) or ""
        lines = [l.strip() for l in raw.strip().split("\n") if l.strip()]
        generated = lines[-1].strip('"').strip("'") if lines else ""
        if generated and 30 < len(generated) < 500:
            logger.info("Image prompt for article %d: %s", article_id, generated)
            return generated
    except Exception as e:
        logger.debug("Image prompt generation failed for article %d: %s", article_id, e)
    # Fallback: generic infographic style for this vertical
    return (
        f"Blog thumbnail infographic, dark navy background, illustrated 3D icons: {fallback_icons}, "
        f"vibrant {accent_hex} accent color, flat design style, "
        "absolutely no text no letters no words no numbers no symbols, no logos, no faces, 16:9."
    )


def _overlay_headline(img_bytes: bytes, phrase: str, vertical: str) -> bytes:
    """Overlay a short phrase centered on the image with a clean dark vignette. Returns PNG bytes."""
    from PIL import Image, ImageDraw, ImageFont, ImageFilter
    import io

    img = Image.open(io.BytesIO(img_bytes)).convert("RGBA")
    W, H = img.size

    accent = _VERTICAL_ACCENT_COLORS.get(vertical, (13, 110, 253))
    r_a, g_a, b_a = accent

    # --- Load fonts ---
    font_title = font_badge = font_tag = None
    for font_path in [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
    ]:
        if Path(font_path).exists():
            font_title = ImageFont.truetype(font_path, size=max(44, H // 10))
            font_badge = ImageFont.truetype(font_path, size=max(22, H // 24))
            font_tag   = ImageFont.truetype(font_path, size=max(18, H // 30))
            break

    draw = ImageDraw.Draw(img)

    # --- Bottom gradient bar (dark → transparent, bottom 40%) ---
    bar_h = int(H * 0.55)
    bar_top = H - bar_h
    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    odraw = ImageDraw.Draw(overlay)
    for y_off in range(bar_h):
        alpha = int(210 * (y_off / bar_h) ** 0.6)
        odraw.rectangle([(0, bar_top + y_off), (W, bar_top + y_off + 1)], fill=(10, 10, 20, alpha))
    img = Image.alpha_composite(img, overlay)
    draw = ImageDraw.Draw(img)

    # --- Accent left border stripe ---
    stripe_w = max(6, W // 80)
    draw.rectangle([(0, 0), (stripe_w, H)], fill=(*accent, 255))

    # --- Wrap title into lines (max 3, left-aligned inside safe margin) ---
    margin_l = stripe_w + 28
    margin_r = int(W * 0.06)
    max_w = W - margin_l - margin_r
    words = phrase.split()
    lines, line = [], []
    for word in words:
        test = " ".join(line + [word])
        tw = draw.textlength(test, font=font_title) if font_title else len(test) * 22
        if tw <= max_w:
            line.append(word)
        else:
            if line:
                lines.append(" ".join(line))
            line = [word]
        if len(lines) >= 3:
            break
    if line and len(lines) < 3:
        lines.append(" ".join(line))

    line_h = (font_title.size if font_title else 48) + 10
    text_block_h = len(lines) * line_h

    # Position: bottom section, above the very bottom
    bottom_pad = int(H * 0.07)
    ty = H - bottom_pad - text_block_h

    # Accent underline beneath title block
    underline_y = ty - 14
    draw.rectangle([(margin_l, underline_y), (margin_l + min(120, max_w // 3), underline_y + 4)],
                   fill=(*accent, 255))

    # Title text with drop shadow
    for i, ln in enumerate(lines):
        y = ty + i * line_h
        for dx, dy in [(2, 2), (1, 1)]:
            draw.text((margin_l + dx, y + dy), ln, fill=(0, 0, 0, 160), font=font_title)
        draw.text((margin_l, y), ln, fill=(255, 255, 255, 255), font=font_title)


    out = io.BytesIO()
    img.convert("RGB").save(out, format="PNG", optimize=True)
    return out.getvalue()


def _generate_article_image(article_id: int, title: str, vertical: str, summary: str = "") -> str | None:
    """Generate a Gemini Imagen background, overlay headline, save to static/ai-images/."""
    # Call _get_http_client() first — this populates _vertex_project as a side effect
    client = _get_http_client()
    api_key = os.getenv("GEMINI_API_KEY")

    prompt = _make_image_prompt(client, title, vertical, article_id, summary)

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

        # Save locally
        out_dir = Path(__file__).parent.parent / "static" / "ai-images"
        out_dir.mkdir(exist_ok=True)
        img_path = out_dir / f"{article_id}.png"
        img_path.write_bytes(infographic_bytes)
        logger.info("Generated infographic for article %d -> %s", article_id, img_path.name)

        # Upload to S3 and return the public URL if successful
        s3_url = _upload_image_to_s3(article_id, infographic_bytes)
        return s3_url or f"/static/ai-images/{article_id}.png"
    except Exception as e:
        logger.warning("Image generation failed for article %d: %s", article_id, e)
        return None


def _upload_image_to_s3(article_id: int, img_bytes: bytes) -> str | None:
    """Upload PNG bytes to S3 qahiristmedia/ai-images/<id>.png and return the public URL.

    Uses the server's IAM role (instance profile) by default.
    Falls back to AWS_S3_KEY/AWS_S3_SECRET env vars for local dev.
    """
    try:
        import boto3
        from botocore.exceptions import BotoCoreError, ClientError

        # Explicit keys for local dev; on server the IAM role is picked up automatically
        aws_key    = os.getenv("AWS_S3_KEY")
        aws_secret = os.getenv("AWS_S3_SECRET")
        kwargs = {"region_name": _S3_REGION}
        if aws_key and aws_secret:
            kwargs["aws_access_key_id"]     = aws_key
            kwargs["aws_secret_access_key"] = aws_secret

        s3  = boto3.client("s3", **kwargs)
        # Timestamp suffix so re-generations don't collide with old S3 objects
        key = f"{_S3_PREFIX}{article_id}_{int(time.time())}.png"
        s3.put_object(
            Bucket=_S3_BUCKET,
            Key=key,
            Body=img_bytes,
            ContentType="image/png",
            ACL="public-read",
        )
        url = f"{_S3_BASE_URL}{key}"
        logger.info("Uploaded article %d image to S3: %s", article_id, url)
        return url
    except (BotoCoreError, ClientError) as e:
        logger.warning("S3 upload failed for article %d: %s", article_id, e)
        return None
    except ImportError:
        logger.warning("boto3 not installed — skipping S3 upload")
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


def _call_gemini_raw(client: httpx.Client, user_msg: str, temperature: float = 0.8) -> str | None:
    """Gemini call without the news-classifier system prompt — for creative/freeform tasks."""
    if _vertex_project:
        url = _GEMINI_VERTEX_URL.format(project=_vertex_project, model=_MODEL)
    else:
        url = _GEMINI_AI_STUDIO_URL.format(model=_MODEL)
    payload = {
        "contents": [{"role": "user", "parts": [{"text": user_msg}]}],
        "generationConfig": {"temperature": temperature, "maxOutputTokens": 512, "thinkingConfig": {"thinkingBudget": 0}},
    }
    resp = client.post(url, json=payload)
    resp.raise_for_status()
    data = resp.json()
    return data["candidates"][0]["content"]["parts"][0]["text"]


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
            if not article.ai_image_url and _GENERATE_IMAGES:
                article.ai_image_url = _generate_article_image(
                    article.id,
                    article.ai_title or article.title,
                    article.vertical or "Tech",
                    article.ai_summary or "",
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
