from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

from app.config import settings

_is_sqlite = settings.DATABASE_URL.startswith("sqlite")
engine = create_engine(
    settings.DATABASE_URL,
    pool_pre_ping=True,
    **({} if _is_sqlite else {"pool_recycle": 3600, "pool_size": 10, "max_overflow": 20}),
    connect_args={"check_same_thread": False} if _is_sqlite else {},
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    from app.models import article  # noqa: F401
    Base.metadata.create_all(bind=engine, checkfirst=True)
    _migrate(engine)


def _migrate(eng):
    """Add columns that were introduced after initial table creation."""
    from sqlalchemy import inspect as sa_inspect, text
    inspector = sa_inspect(eng)
    tables = inspector.get_table_names()
    if "articles" not in tables:
        return
    existing = {c["name"] for c in inspector.get_columns("articles")}
    with eng.connect() as conn:
        if "story_hash" not in existing:
            conn.execute(text("ALTER TABLE articles ADD COLUMN story_hash VARCHAR(12)"))
            conn.commit()
        if "is_highlighted" not in existing:
            conn.execute(text("ALTER TABLE articles ADD COLUMN is_highlighted BOOLEAN NOT NULL DEFAULT 0"))
            conn.commit()
        if "ai_title" not in existing:
            conn.execute(text("ALTER TABLE articles ADD COLUMN ai_title VARCHAR(512)"))
            conn.commit()
        if "ai_summary" not in existing:
            conn.execute(text("ALTER TABLE articles ADD COLUMN ai_summary TEXT"))
            conn.commit()
        if "ai_enriched_at" not in existing:
            conn.execute(text("ALTER TABLE articles ADD COLUMN ai_enriched_at DATETIME"))
            conn.commit()
        if "hiring_relevant" not in existing:
            conn.execute(text("ALTER TABLE articles ADD COLUMN hiring_relevant BOOLEAN NOT NULL DEFAULT 0"))
            conn.commit()
