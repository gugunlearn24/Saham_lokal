from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml  # type: ignore[import-untyped]

from .schema_validator import validate_brokers_config, validate_rules_config


def _load_yaml_file(path: str | Path) -> Any:
    yaml_path = Path(path)
    if not yaml_path.exists():
        raise FileNotFoundError(f"Configuration file not found: {yaml_path}")

    try:
        with yaml_path.open("r", encoding="utf-8") as handle:
            payload = yaml.safe_load(handle) or {}
    except yaml.YAMLError as exc:
        raise ValueError(f"Invalid YAML syntax in '{yaml_path}': {exc}") from exc

    if not isinstance(payload, dict):
        raise ValueError(f"Top-level YAML document in '{yaml_path}' must be an object/map.")

    return payload


def load_brokers_config(path: str | Path) -> list[dict[str, Any]]:
    """Load and validate the broker adapter YAML file."""
    return validate_brokers_config(_load_yaml_file(path))


def load_rules_config(path: str | Path) -> dict[str, Any]:
    """Load and validate the evaluation rules YAML file."""
    return validate_rules_config(_load_yaml_file(path))
