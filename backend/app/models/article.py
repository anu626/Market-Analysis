from datetime import datetime

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    Index,
)
from sqlalchemy.orm import relationship

from app.database import Base


class Source(Base):
    __tablename__ = "sources"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(128), unique=True, nullable=False)
    type = Column(String(32), nullable=False)  # 'rss' | 'api'
    vertical = Column(String(16), nullable=False, default='tech', index=True)

    articles = relationship("Article", back_populates="source")


class Article(Base):
    __tablename__ = "articles"

    id = Column(Integer, primary_key=True, autoincrement=True)
    title = Column(String(512), nullable=False)
    url = Column(String(1024), unique=True, nullable=False)
    source_id = Column(Integer, ForeignKey("sources.id"), nullable=True)
    source_name = Column(String(128), nullable=False, index=True)
    score = Column(Integer, default=0, nullable=False)
    summary = Column(Text, nullable=True)
    published_at = Column(DateTime, nullable=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)
    rank_score = Column(Float, default=0.0, nullable=False, index=True)
    story_hash = Column(String(12), nullable=True, index=True)
    external_id = Column(String(64), nullable=True, index=True)
    vertical = Column(String(16), nullable=False, default='industry', index=True)
    is_highlighted = Column(Boolean, default=False, nullable=False, index=True)

    source = relationship("Source", back_populates="articles")

    __table_args__ = (
        Index("ix_articles_source_external", "source_name", "external_id"),
    )


class IngestionLog(Base):
    __tablename__ = "ingestion_logs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    source = Column(String(128), nullable=False)
    fetched = Column(Integer, default=0)
    inserted = Column(Integer, default=0)
    duplicates = Column(Integer, default=0)
    errors = Column(Integer, default=0)
    started_at = Column(DateTime, default=datetime.utcnow, nullable=False)
    finished_at = Column(DateTime, nullable=True)
    notes = Column(Text, nullable=True)
