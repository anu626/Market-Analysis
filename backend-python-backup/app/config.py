import os
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    DATABASE_URL: str = os.getenv(
        "DATABASE_URL",
        "mysql+pymysql://news_user:news_password@localhost:3306/tech_news",
    )
    REDIS_URL: str = os.getenv("REDIS_URL", "redis://localhost:6379/0")
    CELERY_BROKER_URL: str = os.getenv("CELERY_BROKER_URL", "redis://localhost:6379/1")
    CELERY_RESULT_BACKEND: str = os.getenv("CELERY_RESULT_BACKEND", "redis://localhost:6379/2")

    CACHE_TTL: int = int(os.getenv("CACHE_TTL", "300"))
    INGEST_INTERVAL_SECONDS: int = int(os.getenv("INGEST_INTERVAL_SECONDS", "300"))

    HN_ITEM_URL: str = "https://hacker-news.firebaseio.com/v0/item/{id}.json"
    HN_FETCH_LIMIT: int = 80


settings = Settings()
