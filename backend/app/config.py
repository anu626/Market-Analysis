from pathlib import Path
from pydantic_settings import BaseSettings

_ENV_FILE = Path(__file__).parent.parent / ".env"


class Settings(BaseSettings):
    DATABASE_URL: str = "mysql+pymysql://news_user:news_password@localhost:3306/tech_news"
    REDIS_URL: str = "redis://localhost:6379/0"
    CELERY_BROKER_URL: str = "redis://localhost:6379/1"
    CELERY_RESULT_BACKEND: str = "redis://localhost:6379/2"
    CACHE_TTL: int = 300
    INGEST_INTERVAL_SECONDS: int = 300
    GEMINI_API_KEY: str = ""
    GEMINI_MODEL: str = "gemini-2.5-flash"
    GOOGLE_CRED_PATH: str = ""

    HN_ITEM_URL: str = "https://hacker-news.firebaseio.com/v0/item/{id}.json"
    HN_FETCH_LIMIT: int = 80

    class Config:
        env_file = str(_ENV_FILE)
        env_file_encoding = "utf-8"
        extra = "ignore"


settings = Settings()
