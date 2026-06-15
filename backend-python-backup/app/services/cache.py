"""Tiny Redis JSON cache wrapper. Fail-open: never raises on Redis trouble."""

import json
import logging
from typing import Any

import redis

from app.config import settings

logger = logging.getLogger(__name__)

_redis: redis.Redis | None = None


def get_redis() -> redis.Redis:
    global _redis
    if _redis is None:
        _redis = redis.Redis.from_url(settings.REDIS_URL, decode_responses=True)
    return _redis


def cache_get(key: str) -> Any | None:
    try:
        raw = get_redis().get(key)
        if raw is None:
            return None
        return json.loads(raw)
    except Exception as e:
        logger.warning("cache_get failed for %s: %s", key, e)
        return None


def cache_set(key: str, value: Any, ttl: int | None = None) -> None:
    try:
        ttl = ttl or settings.CACHE_TTL
        get_redis().setex(key, ttl, json.dumps(value, default=str))
    except Exception as e:
        logger.warning("cache_set failed for %s: %s", key, e)


def cache_delete_prefix(prefix: str) -> None:
    try:
        r = get_redis()
        for key in r.scan_iter(match=f"{prefix}*"):
            r.delete(key)
    except Exception as e:
        logger.warning("cache_delete_prefix failed for %s: %s", prefix, e)
