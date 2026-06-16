from datetime import datetime

from pydantic import BaseModel


class ArticleOut(BaseModel):
    id: int
    title: str
    url: str
    source_name: str
    source_logo: str | None = None
    score: int
    summary: str | None = None
    published_at: datetime | None = None
    created_at: datetime
    rank_score: float
    vertical: str = "tech"
    source_count: int = 1
    sources: list[dict] = []
    ai_title: str | None = None
    ai_summary: str | None = None
    hiring_relevant: bool = False
    image_url: str | None = None
    ai_image_url: str | None = None

    class Config:
        from_attributes = True


class IngestionResult(BaseModel):
    fetched: int
    inserted: int
    duplicates: int
    filtered: int
    errors: int
