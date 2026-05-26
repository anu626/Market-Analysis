# Market Analysis — News Aggregator Backend

A news aggregation pipeline serving two verticals:
- **hirist.tech** — tech, engineering, AI/ML, startups
- **iimjobs** — business, BFSI, HR, strategy, markets

```
sources.yaml → INGEST → NORMALIZE → DEDUP → STORE → RANK → SERVE
```

117 sources across RSS, Google News, Hacker News, Reddit, and job board APIs.
Each source is tagged `vertical: tech | business | both`.

---

## Prerequisites

- Python 3.10+
- pip

That's it for the quick setup. MySQL + Redis are optional (only needed for production scheduling).

---

## Quick Setup (SQLite — no Docker needed)

```bash
git clone <repo-url>
cd Market-Analysis/backend

# 1. Create and activate virtual environment
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Start the API
DATABASE_URL="sqlite:///./tech_news.db" uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

API is live at **http://localhost:8000**
Interactive docs at **http://localhost:8000/docs**

---

## First Run — Fetch Articles

Once the server is running, trigger ingestion:

```bash
curl -X POST http://localhost:8000/ingest
```

This runs through all 117 sources sequentially. Takes **2–4 minutes** on first run.
Watch progress in the terminal where uvicorn is running.

Then fetch articles:

```bash
# All articles (ranked by HN-style score)
curl http://localhost:8000/articles

# Filter by vertical
curl "http://localhost:8000/articles?vertical=tech"
curl "http://localhost:8000/articles?vertical=business"

# Latest first
curl "http://localhost:8000/articles/latest"

# Search
curl "http://localhost:8000/articles?q=razorpay"
```

---

## Key Endpoints

| Method | Endpoint | Description |
|---|---|---|
| GET | `/health` | Health check |
| GET | `/sources` | Ingested sources + article counts |
| GET | `/sources/config` | All 117 configured sources from YAML |
| GET | `/sources/config?vertical=tech` | Filter sources by vertical |
| GET | `/sources/config?type=rss` | Filter by type (rss, api, google_news, json_api) |
| GET | `/sources/config?tier=1` | Filter by tier (1=high freq, 2=mid, 3=low) |
| GET | `/articles` | Ranked articles |
| GET | `/articles?vertical=tech` | hirist.tech feed |
| GET | `/articles?vertical=business` | iimjobs feed |
| GET | `/articles?q=<term>` | Search title + summary |
| GET | `/articles/latest` | Latest articles (chronological) |
| GET | `/articles/{id}` | Single article |
| POST | `/ingest` | Trigger manual ingestion |

---

## Source Configuration

All sources live in `backend/app/config/sources.yaml`.

```yaml
sources:
  - name: ET Tech
    type: rss
    url: https://economictimes.indiatimes.com/tech/rssfeeds/13357270.cms
    country: IN
    vertical: both        # tech | business | both
    authority: 0.85
    tier: 1               # 1=5min, 2=15min, 3=60min
    tags: [mainstream, business, national]
```

**Source breakdown:**

| Vertical | Count | Examples |
|---|---|---|
| `tech` | 46 | HN, TechCrunch, arXiv, engineering blogs, OpenAI/Anthropic |
| `business` | 49 | ET Markets, BFSI, HR World, McKinsey, Bloomberg Markets |
| `both` | 22 | Inc42, YourStory, ET Tech, job boards |

To add a new source, append an entry to `sources.yaml` — no code change needed.

---

## Project Structure

```
backend/
  app/
    api/
      routes.py             # FastAPI endpoints
      schemas.py            # Pydantic response models
    config/
      sources.yaml          # All 117 news sources
    dedup/
      deduplicator.py       # URL exact + fuzzy title dedup (rapidfuzz)
    ingestion/
      source_loader.py      # Reads sources.yaml
      rss_fetcher.py        # RSS + Google News (feedparser, 15s timeout)
      hn_fetcher.py         # Hacker News Firebase + Algolia APIs
      reddit_fetcher.py     # Multi-subreddit Reddit JSON
      json_api_fetcher.py   # Greenhouse + Lever job boards
    models/
      article.py            # SQLAlchemy: Article, Source, IngestionLog
    normalization/
      normalizer.py         # URL cleaning, title/summary sanitization
    ranking/
      ranker.py             # HN-style time decay: (score+1)/(age_hours+2)^1.5
    services/
      ingestion_service.py  # Orchestrator: fetch→normalize→dedup→store→rank
      cache.py              # Redis cache (fails open — works without Redis)
    workers/
      tasks.py              # Celery tasks (ingest_all, rerank_all)
    config.py               # Settings (DATABASE_URL, REDIS_URL, etc.)
    database.py             # SQLAlchemy engine + session
    main.py                 # FastAPI app
  requirements.txt
```

---

## Configuration — Environment Variables

| Variable | Default | Notes |
|---|---|---|
| `DATABASE_URL` | `mysql+pymysql://news_user:news_password@localhost:3306/tech_news` | Use `sqlite:///./tech_news.db` for local dev |
| `REDIS_URL` | `redis://localhost:6379/0` | Optional — cache gracefully skips if Redis is down |
| `CELERY_BROKER_URL` | `redis://localhost:6379/1` | Only needed for scheduled ingestion |
| `CELERY_RESULT_BACKEND` | `redis://localhost:6379/2` | Only needed for Celery |
| `CACHE_TTL` | `300` | Cache TTL in seconds |
| `HN_FETCH_LIMIT` | `80` | Max HN items per source per run |

---

## Production Setup (MySQL + Redis + Celery)

### 1. Start infrastructure

```bash
# from repo root
docker compose up -d
```

Starts MySQL 8 on `localhost:3306` and Redis 7 on `localhost:6379`.

### 2. Start the API

```bash
cd backend
source .venv/bin/activate
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

### 3. Start Celery workers (two terminals)

```bash
# Terminal A — worker
celery -A app.workers.celery_app.celery_app worker --loglevel=info

# Terminal B — scheduler (runs ingestion every 5 min, rerank every 5 min)
celery -A app.workers.celery_app.celery_app beat --loglevel=info
```

---

## Known Broken Feeds (7 of 117)

These return 0 articles — the remaining 110 work fine:

| Source | Reason |
|---|---|
| Entrackr | Malformed XML |
| MoneyControl Tech | Malformed XML |
| Business Standard Tech | Malformed XML |
| Analytics India Magazine | Malformed XML |
| Tech in Asia India | Returns HTML instead of RSS |
| Zerodha Tech | Undefined XML entity in feed |
| Swiggy Bytes | SSL certificate error |

---

## Vertical Filtering Logic

- `?vertical=tech` → articles where `vertical IN ('tech', 'both')`
- `?vertical=business` → articles where `vertical IN ('business', 'both')`
- No param → all articles

Stories tagged `both` (funding rounds, policy, IPOs) appear on both platforms.
