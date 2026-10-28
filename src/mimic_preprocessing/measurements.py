"""Chunked chart-event filtering and mergeable hourly statistics."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator

import pandas as pd

from .common import PreprocessingError, mappings_by_item, parse_times, require_columns, validate_item_dictionary

OUTPUT_COLUMNS = ["stay_id", "hour", "feature_name", "source_unit", "canonical_unit", "mean", "min", "max", "last", "count"]
EVENT_COLUMNS = {"stay_id", "charttime", "itemid", "valuenum", "valueuom"}
STAY_COLUMNS = {"stay_id", "intime", "outtime"}


def validate_measurement_mappings(mappings: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    by_item = mappings_by_item(mappings, "measurement")
    enabled: dict[int, dict[str, Any]] = {}
    feature_units: dict[tuple[str, str], str] = {}
    for itemid, mapping in by_item.items():
        if not mapping.get("enabled", False):
            continue
        if mapping.get("approval_status") != "approved":
            raise PreprocessingError(f"Enabled measurement itemid {itemid} is not clinically approved")
        if not mapping.get("feature_name") or not mapping.get("canonical_unit"):
            raise PreprocessingError(f"Enabled measurement itemid {itemid} needs feature_name and canonical_unit")
        if mapping.get("conversion_rule") != "identity":
            raise PreprocessingError(f"Unsupported conversion_rule for itemid {itemid}: {mapping.get('conversion_rule')!r}")
        if not mapping.get("source_unit"):
            raise PreprocessingError(f"Enabled measurement itemid {itemid} needs source_unit")
        key = (str(mapping["feature_name"]), str(mapping["canonical_unit"]))
        old_source = feature_units.setdefault(key, str(mapping["source_unit"]))
        if old_source != str(mapping["source_unit"]):
            raise PreprocessingError(f"Feature {key[0]!r} maps incompatible units without conversion")
        enabled[itemid] = mapping
    if not enabled:
        raise PreprocessingError("No enabled, approved measurement item mappings were provided")
    return enabled


def _iter_csv(source: str | Path | pd.DataFrame, chunksize: int) -> Iterator[pd.DataFrame]:
    if isinstance(source, pd.DataFrame):
        yield source.copy()
        return
    if chunksize < 1:
        raise PreprocessingError("chunksize must be a positive integer")
    yield from pd.read_csv(source, compression="infer", usecols=list(EVENT_COLUMNS), chunksize=chunksize, low_memory=True)


def _stay_table(stays: str | Path | pd.DataFrame, stay_ids: set[int] | None) -> pd.DataFrame:
    cols = ["stay_id", "intime", "outtime"]
    frame = stays[cols].copy() if isinstance(stays, pd.DataFrame) else pd.read_csv(stays, usecols=cols, compression="infer")
    require_columns(frame, STAY_COLUMNS, "icustays")
    frame["stay_id"] = pd.to_numeric(frame["stay_id"], errors="coerce")
    frame["intime"] = parse_times(frame["intime"])
    frame["outtime"] = parse_times(frame["outtime"])
    frame = frame.dropna(subset=["stay_id", "intime", "outtime"])
    frame = frame.drop_duplicates("stay_id", keep="first").set_index("stay_id")
    if stay_ids is not None:
        frame = frame.loc[frame.index.isin(stay_ids)]
    return frame


def aggregate_measurements(
    events: str | Path | pd.DataFrame,
    stays: str | Path | pd.DataFrame,
    mappings: list[dict[str, Any]],
    chunksize: int = 500_000,
    stay_ids: set[int] | None = None,
    item_dictionary: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Aggregate events to stay-relative elapsed hours; chunks merge by sufficient stats."""
    item_map = validate_measurement_mappings(mappings)
    validate_item_dictionary(item_dictionary, set(item_map))
    stay_frame = _stay_table(stays, stay_ids)
    # key -> [sum, min, max, count, last_time, last_row_order, last_value]
    stats: dict[tuple[Any, ...], list[Any]] = {}
    bad_timestamps = 0
    nonnumeric_values = 0
    unit_mismatches = 0
    row_order = 0
    for chunk in _iter_csv(events, chunksize):
        require_columns(chunk, EVENT_COLUMNS, "chartevents")
        chunk = chunk.copy()
        chunk["itemid"] = pd.to_numeric(chunk["itemid"], errors="coerce")
        chunk = chunk.loc[chunk["itemid"].isin(item_map)].copy()
        if stay_ids is not None:
            chunk["stay_id"] = pd.to_numeric(chunk["stay_id"], errors="coerce")
            chunk = chunk.loc[chunk["stay_id"].isin(stay_ids)]
        if chunk.empty:
            row_order += len(chunk)
            continue
        chunk["_row_order"] = range(row_order, row_order + len(chunk))
        row_order += len(chunk)
        chunk["stay_id"] = pd.to_numeric(chunk["stay_id"], errors="coerce")
        chunk["charttime"] = parse_times(chunk["charttime"])
        chunk["valuenum"] = pd.to_numeric(chunk["valuenum"], errors="coerce")
        bad_timestamps += int(chunk["charttime"].isna().sum())
        nonnumeric_values += int(chunk["valuenum"].isna().sum())
        chunk = chunk.dropna(subset=["stay_id", "charttime", "valuenum"])
        if chunk.empty:
            continue
        chunk["itemid"] = chunk["itemid"].astype(int)
        chunk = chunk.join(stay_frame[["intime", "outtime"]], on="stay_id")
        chunk = chunk.loc[(chunk["charttime"] >= chunk["intime"]) & (chunk["charttime"] < chunk["outtime"])].copy()
        if chunk.empty:
            continue
        chunk["hour"] = ((chunk["charttime"] - chunk["intime"]).dt.total_seconds() // 3600).astype("int64")
        for itemid, mapping in item_map.items():
            mask = chunk["itemid"].eq(itemid)
            mapped = chunk.loc[mask]
            # Source unit is part of the mapping contract. Do not relabel an
            # observed value whose valueuom disagrees with that contract.
            unit_ok = mapped["valueuom"].astype("string").eq(str(mapping["source_unit"]))
            unit_mismatches += int((~unit_ok).sum())
            mapped = mapped.loc[unit_ok]
            for row in mapped[["stay_id", "charttime", "hour", "valuenum", "_row_order"]].itertuples(index=False, name=None):
                sid, timestamp, hour, value, order = row
                key = (int(sid), int(hour), str(mapping["feature_name"]), str(mapping["source_unit"]), str(mapping["canonical_unit"]))
                stat = stats.get(key)
                if stat is None:
                    stats[key] = [float(value), float(value), float(value), 1, timestamp, int(order), float(value)]
                else:
                    stat[0] += float(value)
                    stat[1] = min(stat[1], float(value))
                    stat[2] = max(stat[2], float(value))
                    stat[3] += 1
                    if (timestamp, int(order)) >= (stat[4], stat[5]):
                        stat[4], stat[5], stat[6] = timestamp, int(order), float(value)
    # Diagnostic counts are available to callers through attrs without bloating rows.
    rows = []
    for (sid, hour, feature, source_unit, canonical_unit), (total, minimum, maximum, count, _time, _order, last) in stats.items():
        rows.append({"stay_id": sid, "hour": hour, "feature_name": feature, "source_unit": source_unit,
                     "canonical_unit": canonical_unit, "mean": total / count, "min": minimum, "max": maximum,
                     "last": last, "count": count})
    result = pd.DataFrame(rows, columns=OUTPUT_COLUMNS)
    if not result.empty:
        result = result.sort_values(["stay_id", "hour", "feature_name", "source_unit"], kind="mergesort").reset_index(drop=True)
    result = result.astype({"stay_id": "int64", "hour": "int64", "feature_name": "string", "source_unit": "string",
                            "canonical_unit": "string", "mean": "float64", "min": "float64", "max": "float64",
                            "last": "float64", "count": "int64"})
    result.attrs.update(invalid_timestamps=bad_timestamps, invalid_numeric_values=nonnumeric_values, unit_mismatches=unit_mismatches,
                        stay_boundary_policy="intime inclusive, outtime exclusive; hour is floor((charttime-intime)/1h)",
                        duplicate_policy="Duplicate source rows are retained as observations and contribute to count")
    return result
