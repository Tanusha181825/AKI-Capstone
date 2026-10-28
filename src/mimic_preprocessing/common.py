"""Shared validation and mapping helpers."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pandas as pd


class PreprocessingError(ValueError):
    """Raised when an input schema or approved mapping is not usable."""


def read_json(path: str | Path) -> dict[str, Any]:
    with Path(path).open(encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise PreprocessingError("Configuration root must be a JSON object")
    return value


def require_columns(frame: pd.DataFrame, columns: set[str], label: str) -> None:
    missing = columns.difference(frame.columns)
    if missing:
        raise PreprocessingError(f"{label} missing required columns: {sorted(missing)}")


def mappings_by_item(mappings: list[dict[str, Any]], label: str) -> dict[int, dict[str, Any]]:
    result: dict[int, dict[str, Any]] = {}
    for mapping in mappings:
        raw_itemid = mapping.get("itemid")
        if raw_itemid is None or str(raw_itemid).strip() == "":
            if mapping.get("enabled", False):
                raise PreprocessingError(f"Enabled {label} mapping needs an integer itemid")
            continue
        try:
            itemid = int(raw_itemid)
        except (KeyError, TypeError, ValueError) as exc:
            raise PreprocessingError(f"{label} mapping needs an integer itemid") from exc
        if itemid in result:
            raise PreprocessingError(f"Duplicate {label} mapping for itemid {itemid}")
        result[itemid] = mapping
    return result


def parse_times(values: pd.Series) -> pd.Series:
    # MIMIC timestamps are naive local clinical times. Preserve that convention.
    return pd.to_datetime(values, format="mixed", errors="coerce")


def validate_item_dictionary(item_dictionary: pd.DataFrame | None, mapped_ids: set[int]) -> None:
    if item_dictionary is None:
        return
    require_columns(item_dictionary, {"itemid", "label"}, "d_items")
    ids = pd.to_numeric(item_dictionary["itemid"], errors="coerce").dropna().astype(int)
    missing = mapped_ids.difference(set(ids))
    if missing:
        raise PreprocessingError(f"Mapped itemids absent from d_items: {sorted(missing)}")
