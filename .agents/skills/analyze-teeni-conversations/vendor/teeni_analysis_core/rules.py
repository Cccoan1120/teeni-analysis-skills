from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any


DEFAULT_RULES_PATH = Path(__file__).resolve().parents[2] / "references" / "default-rules.json"


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = deepcopy(value)
    return merged


def _read_json_object(value: str | Path | dict[str, Any] | None) -> dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, dict):
        return value
    candidate = Path(value)
    text = candidate.read_text(encoding="utf-8") if candidate.is_file() else str(value)
    parsed = json.loads(text)
    if not isinstance(parsed, dict):
        raise ValueError("rule overrides must be a JSON object")
    return parsed


def load_rules(
    path: str | Path | None = None,
    overrides: str | Path | dict[str, Any] | None = None,
) -> dict[str, Any]:
    source = Path(path) if path else DEFAULT_RULES_PATH
    rules = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(rules, dict):
        raise ValueError("default rules must be a JSON object")
    rules = _deep_merge(rules, _read_json_object(overrides))
    for field in (
        "schema_version",
        "rules_version",
        "scene_capabilities",
        "opening",
        "invalid_turns",
        "quality",
        "ending_analysis",
        "safety",
        "user_safety",
    ):
        if field not in rules:
            raise ValueError(f"default rules missing required field: {field}")
    return rules


def scene_capability(rules: dict[str, Any], scene_id: str) -> dict[str, Any]:
    capabilities = rules.get("scene_capabilities", {})
    current = str(scene_id)
    visited: set[str] = set()
    merged: dict[str, Any] = {}
    while current:
        if current in visited:
            raise ValueError(f"scene capability alias cycle: {current}")
        visited.add(current)
        raw = capabilities.get(current)
        if not isinstance(raw, dict):
            return merged
        merged = _deep_merge(raw, merged)
        current = str(raw.get("alias_of") or "")
    merged.pop("alias_of", None)
    return merged
