"""Load sources from sources.yaml and expose them by type."""

from functools import lru_cache
from pathlib import Path

import yaml

_YAML_PATH = Path(__file__).parent.parent / "config" / "sources.yaml"


@lru_cache(maxsize=1)
def _load() -> list[dict]:
    with open(_YAML_PATH) as f:
        data = yaml.safe_load(f)
    return data.get("sources", [])


def sources_of_type(*types: str) -> list[dict]:
    return [s for s in _load() if s.get("type") in types]
