# Tech News Aggregator (Local Prototype)

A working local prototype of a tech-news aggregator (Hacker News + Techmeme style)
implementing the full pipeline:

```
INGEST → NORMALIZE → DEDUP → STORE → RANK → CACHE → SERVE → DISPLAY
```

## Architecture

```
┌──────────────┐    ┌──────────────────────────────────────────────┐    ┌──────────┐
│ Celery Beat  │──▶│  Celery worker                                │──▶│  MySQL   │
└──────────────┘    │  ingest → normalize → dedup → store → rank  │    └──────────┘
                    └──────────────────────────────────────────────┘          ▲
                                          │ bust cache                        │
                                          ▼                                   │
                                    ┌─────────┐                               │
                                    │  Redis  │◀──── cache ──── FastAPI ──────┘
                                    └─────────┘                  ▲
                                                                 │ HTTP
                                                          ┌──────┴──────┐
                                                          │   Next.js   │
                                                          └─────────────┘
```

### Key design decisions

- **Single ingestion orchestrator** (`app/services/ingestion_service.py`) so the
  same code path runs from Celery, the `POST /ingest` endpoint, or a CLI.
- **Two-stage dedup**: indexed exact-URL match first, then a fuzzy title check
  (`rapidfuzz.token_set_ratio` ≥ 88) over the last 7 days only — cheap and
  good enough for the prototype.
- **HN-style ranking**: `score = (upvotes + 1) / (age_hours + 2)^1.5`, recomputed
  on insert and every 5 minutes by a Celery Beat task.
- **Cache fail-open**: Redis errors degrade to direct DB reads; never raise.
- **No microservices**: one FastAPI app + one Celery worker process.

## Project structure

```
backend/
  app/
    api/            # FastAPI routes + Pydantic schemas
    dedup/          # URL + fuzzy title deduplication
    ingestion/      # Source-specific fetchers (HN, RSS, Reddit)
    models/         # SQLAlchemy models
    normalization/  # URL/title cleaning
    ranking/        # Time-decay scoring
    services/       # Cache + ingestion orchestrator
    workers/        # Celery app + tasks
    config.py
    database.py
    main.py
  requirements.txt
frontend/
  components/
  pages/
  styles/
  package.json
docker-compose.yml
```

## Prerequisites

- Docker + Docker Compose
- Python 3.11+
- Node.js 18+

## 1. Start MySQL + Redis

```bash
docker compose up -d
```

This launches:
- MySQL 8 on `localhost:3306` (db `tech_news`, user `news_user`, password `news_password`)
- Redis 7 on `localhost:6379`

## 2. Backend — install + run API

```bash
cd backend
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# tables are auto-created on app startup
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

The API is now at `http://localhost:8000`. Try:

- `GET http://localhost:8000/health`
- `GET http://localhost:8000/articles`
- `GET http://localhost:8000/articles/latest`
- `POST http://localhost:8000/ingest` (manual ingestion trigger)

OpenAPI docs: `http://localhost:8000/docs`

## 3. Background workers

In **two separate terminals**, both from `backend/` with the venv activated:

```bash
# Terminal A — Celery worker
celery -A app.workers.celery_app.celery_app worker --loglevel=info

# Terminal B — Celery Beat (scheduler — runs ingestion every 5 min, rerank every 5 min)
celery -A app.workers.celery_app.celery_app beat --loglevel=info
```

## 4. Frontend

```bash
cd frontend
npm install
npm run dev
```

Open `http://localhost:3000`.

The UI has:
- **Ranked / Latest** toggle
- **Refresh** button
- **Trigger Ingestion** button (calls `POST /ingest`)

## 5. Trigger ingestion manually

Any of:

```bash
curl -X POST http://localhost:8000/ingest
```

```bash
# from backend/ with venv:
python -c "from app.database import SessionLocal; from app.services.ingestion_service import run_full_ingestion; print(run_full_ingestion(SessionLocal()))"
```

```bash
# Enqueue Celery task
celery -A app.workers.celery_app.celery_app call app.workers.tasks.ingest_all
```

## Configuration

All settings have sensible defaults in `backend/app/config.py`. Override via env vars:

| Var | Default |
| --- | --- |
| `DATABASE_URL` | `mysql+pymysql://news_user:news_password@localhost:3306/tech_news` |
| `REDIS_URL` | `redis://localhost:6379/0` |
| `CELERY_BROKER_URL` | `redis://localhost:6379/1` |
| `CELERY_RESULT_BACKEND` | `redis://localhost:6379/2` |
| `CACHE_TTL` | `300` (seconds) |
| `INGEST_INTERVAL_SECONDS` | `300` |

## Data sources

- **Hacker News** Firebase API (top 80 stories, fetched concurrently)
- **RSS**: TechCrunch, The Verge, Ars Technica
- **Reddit**: `r/programming` JSON endpoint

No scraping, no auth, no proxies — strictly public APIs.

## Database schema

- `articles` — `id, title, url (unique), source_id, source_name, score, summary, published_at, created_at, rank_score, external_id`
- `sources` — `id, name (unique), type` (rss/api)
- `ingestion_logs` — per-source fetch stats (fetched/inserted/duplicates/errors)

## Future work (intentionally out of scope)

- Embedding-based semantic dedup (stage 3)
- LLM-generated TL;DR summaries
- Tags / topic classification
- Pagination cursor instead of offset
- Migrations via Alembic
