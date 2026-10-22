"""Load the provider/route catalog from YAML (path from `RELAY_CONFIG_FILE`)."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml

from relay.domain.catalog import Catalog

_ENV_REF = re.compile(r"\$\{([A-Z0-9_]+)(?::-([^}]*))?\}")


def _expand(value: Any) -> Any:
    """Expand `${VAR}` / `${VAR:-default}` in strings, so URLs can differ per environment."""
    if isinstance(value, str):
        return _ENV_REF.sub(lambda m: os.environ.get(m.group(1), m.group(2) or ""), value)
    if isinstance(value, list):
        return [_expand(item) for item in value]
    if isinstance(value, dict):
        return {key: _expand(item) for key, item in value.items()}
    return value


def load_catalog(path: Path) -> Catalog:
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    return Catalog.model_validate(_expand(raw))
