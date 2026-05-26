from datetime import datetime

from pydantic import BaseModel


class ArticleOut(BaseModel):
    id: int
    title: str
    url: str
    source_name: str
    score: int
    summary: str | None = None
    published_at: datetime | None = None
    created_at: datetime
    rank_score: float
    vertical: str = "tech"

    class Config:
        from_attributes = True


class IngestionResult(BaseModel):
    fetched: int
    inserted: int
    duplicates: int
    errors: int
