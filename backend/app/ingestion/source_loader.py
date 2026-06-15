"""Load sources from sources.yaml and expose them by type."""

from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

import yaml

_YAML_PATH = Path(__file__).parent.parent / "config" / "sources.yaml"

CATEGORY_TO_VERTICAL: dict[str, str] = {
    "recruitment": "recruitment",
    "hiring": "Hiring",
    "layoffs": "Layoffs",
    "funding": "Funding",
    "ai": "AI",
    "skills_tools": "Tech",
    "blogs_tutorials": "Blogs",
    "youtube": "Youtube",
    "tech": "Tech",
    "market_trends": "Market Trends",
}


@lru_cache(maxsize=1)
def _load() -> list[dict]:
    with open(_YAML_PATH) as f:
        data = yaml.safe_load(f)
    return data.get("sources", [])


def _derive_logo(url: str) -> str | None:
    try:
        netloc = urlparse(url).netloc.lstrip("www.")
        if not netloc:
            return None
        return f"https://logo.clearbit.com/{netloc}"
    except Exception:
        return None


def sources_of_type(*types: str) -> list[dict]:
    return [s for s in _load() if s.get("type") in types and s.get("enabled", True)]


def sources_of_type_and_vertical(vertical: str, *types: str) -> list[dict]:
    """Filter enabled sources by type(s) AND vertical (matched via category or vertical field)."""
    vertical_lower = vertical.lower()
    matching_categories = {k for k, v in CATEGORY_TO_VERTICAL.items() if v.lower() == vertical_lower}
    return [
        s for s in _load()
        if s.get("type") in types
        and s.get("enabled", True)
        and (
            s.get("category", "").lower() in matching_categories
            or s.get("vertical", "").lower() == vertical_lower
        )
    ]


@lru_cache(maxsize=1)
def get_logo_map() -> dict[str, str | None]:
    """Returns {source_name: logo_url} for all configured sources."""
    return {
        s["name"]: s.get("logo_url") or _derive_logo(s.get("url", ""))
        for s in _load()
    }
