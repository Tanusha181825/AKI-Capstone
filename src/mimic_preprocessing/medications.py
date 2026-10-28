"""Traceable inputevents filtering and timestamp-supported exposure intervals."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

from .common import PreprocessingError, mappings_by_item, parse_times, require_columns, validate_item_dictionary

MED_COLUMNS = ["stay_id", "itemid", "starttime", "endtime", "amount", "amountuom", "rate", "rateuom", "orderid", "linkorderid"]
REQUIRED = {"stay_id", "itemid", "starttime", "endtime", "amount", "amountuom", "rate", "rateuom"}
HOURLY_COLUMNS = ["stay_id", "hour", "medication_name", "medication_class", "exposure_indicator", "exposure_count", "source_record_ids"]


def validate_medication_mappings(mappings: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    by_item = mappings_by_item(mappings, "medication")
    out = {}
    for itemid, mapping in by_item.items():
        if not mapping.get("enabled", False):
            continue
        if mapping.get("approval_status") != "approved":
            raise PreprocessingError(f"Enabled medication itemid {itemid} is not approved")
        if not mapping.get("medication_name") or not mapping.get("medication_class"):
            raise PreprocessingError(f"Enabled medication itemid {itemid} needs standardized name and class")
        semantics = mapping.get("administration_semantics")
        if semantics not in {"bolus", "infusion", "pending"}:
            raise PreprocessingError(f"Unsupported administration_semantics for itemid {itemid}: {semantics!r}")
        if semantics == "pending":
            raise PreprocessingError(f"Enabled medication itemid {itemid} has pending administration semantics")
        out[itemid] = mapping
    if not out:
        raise PreprocessingError("No enabled, approved medication item mappings were provided")
    return out


def _read_chunks(source: str | Path | pd.DataFrame, chunksize: int):
    if isinstance(source, pd.DataFrame):
        yield source.copy()
    else:
        if chunksize < 1:
            raise PreprocessingError("chunksize must be a positive integer")
        columns = pd.read_csv(source, compression="infer", nrows=0).columns
        usecols = [column for column in MED_COLUMNS if column in columns]
        yield from pd.read_csv(source, compression="infer", usecols=usecols, chunksize=chunksize, low_memory=True)


def process_medications(
    events: str | Path | pd.DataFrame,
    stays: str | Path | pd.DataFrame,
    mappings: list[dict[str, Any]],
    chunksize: int = 250_000,
    stay_ids: set[int] | None = None,
    item_dictionary: pd.DataFrame | None = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Return filtered source-like records and hourly exposure indicators.

    Infusions cover [starttime, endtime); a bolus occupies only its start hour.
    No amount/rate calculation or dose equivalence is performed.
    """
    item_map = validate_medication_mappings(mappings)
    validate_item_dictionary(item_dictionary, set(item_map))
    stay_cols = ["stay_id", "intime", "outtime"]
    stay_frame = stays[stay_cols].copy() if isinstance(stays, pd.DataFrame) else pd.read_csv(stays, usecols=stay_cols, compression="infer")
    require_columns(stay_frame, {"stay_id", "intime", "outtime"}, "icustays")
    stay_frame["stay_id"] = pd.to_numeric(stay_frame["stay_id"], errors="coerce")
    stay_frame["intime"] = parse_times(stay_frame["intime"])
    stay_frame["outtime"] = parse_times(stay_frame["outtime"])
    stay_frame = stay_frame.dropna(subset=["stay_id", "intime", "outtime"]).drop_duplicates("stay_id").set_index("stay_id")
    if stay_ids is not None:
        stay_frame = stay_frame.loc[stay_frame.index.isin(stay_ids)]
    records: list[pd.DataFrame] = []
    exposures: dict[tuple[int, int, str, str], list[int]] = {}
    invalid_start = invalid_interval = 0
    row_order = 0
    for chunk in _read_chunks(events, chunksize):
        require_columns(chunk, REQUIRED, "inputevents")
        chunk = chunk.copy()
        chunk["itemid"] = pd.to_numeric(chunk["itemid"], errors="coerce")
        chunk = chunk.loc[chunk["itemid"].isin(item_map)].copy()
        if stay_ids is not None:
            chunk["stay_id"] = pd.to_numeric(chunk["stay_id"], errors="coerce")
            chunk = chunk.loc[chunk["stay_id"].isin(stay_ids)]
        if chunk.empty:
            continue
        chunk["_source_record_id"] = range(row_order, row_order + len(chunk))
        row_order += len(chunk)
        chunk["stay_id"] = pd.to_numeric(chunk["stay_id"], errors="coerce")
        chunk["itemid"] = chunk["itemid"].astype(int)
        chunk["starttime"] = parse_times(chunk["starttime"])
        chunk["endtime"] = parse_times(chunk["endtime"])
        invalid_start += int(chunk["starttime"].isna().sum())
        chunk = chunk.dropna(subset=["stay_id"])
        chunk = chunk.join(stay_frame[["intime", "outtime"]], on="stay_id")
        chunk = chunk.dropna(subset=["intime", "outtime"])
        for itemid, mapping in item_map.items():
            subset = chunk.loc[chunk["itemid"].eq(itemid)].copy()
            if subset.empty:
                continue
            subset["medication_name"] = mapping["medication_name"]
            subset["medication_class"] = mapping["medication_class"]
            subset["administration_semantics"] = mapping["administration_semantics"]
            # Keep original inputevents fields plus provenance and mapping labels.
            keep = [c for c in MED_COLUMNS if c in subset.columns]
            records.append(subset[keep + ["_source_record_id", "medication_name", "medication_class", "administration_semantics"]].copy())
            # Preserve mapped source rows (including invalid/out-of-stay times)
            # for review; only rows with an in-stay start may create exposure.
            subset = subset.loc[(subset["starttime"] >= subset["intime"]) & (subset["starttime"] < subset["outtime"])].copy()
            for row in subset[["stay_id", "starttime", "endtime", "_source_record_id"]].itertuples(index=False, name=None):
                sid, start, end, source_record_id = row
                sid = int(sid)
                sid_info = stay_frame.loc[sid]
                first_hour = int((start - sid_info.intime).total_seconds() // 3600)
                if mapping["administration_semantics"] == "bolus":
                    hours = [first_hour]
                else:
                    if pd.isna(end) or end <= start:
                        invalid_interval += 1
                        continue
                    clipped_end = min(end, sid_info.outtime)
                    if clipped_end <= start:
                        invalid_interval += 1
                        continue
                    last_hour = int(((clipped_end - pd.Timedelta(nanoseconds=1)) - sid_info.intime).total_seconds() // 3600)
                    hours = range(first_hour, last_hour + 1)
                for hour in hours:
                    key = (sid, hour, str(mapping["medication_name"]), str(mapping["medication_class"]))
                    exposures.setdefault(key, []).append(int(source_record_id))
    record_frame = pd.concat(records, ignore_index=True) if records else pd.DataFrame(columns=MED_COLUMNS + ["_source_record_id", "medication_name", "medication_class", "administration_semantics"])
    if not record_frame.empty:
        record_frame = record_frame.sort_values("_source_record_id", kind="mergesort").reset_index(drop=True)
    hourly = pd.DataFrame([{"stay_id": sid, "hour": hour, "medication_name": name, "medication_class": cls,
                            "exposure_indicator": 1, "exposure_count": len(ids),
                            "source_record_ids": ";".join(map(str, ids))}
                           for (sid, hour, name, cls), ids in exposures.items()], columns=HOURLY_COLUMNS)
    if not hourly.empty:
        hourly = hourly.sort_values(["stay_id", "hour", "medication_name", "medication_class"], kind="mergesort").reset_index(drop=True)
        hourly = hourly.astype({"stay_id": "int64", "hour": "int64", "medication_name": "string", "medication_class": "string",
                                "exposure_indicator": "int8", "exposure_count": "int64", "source_record_ids": "string"})
    hourly.attrs.update(invalid_start_timestamps=invalid_start, invalid_infusion_intervals=invalid_interval,
                        interval_policy="Infusion [start,end), clipped to ICU outtime; bolus attributed to start hour only",
                        overlap_policy="Each source record contributes once to each hour it overlaps; duplicates remain distinct")
    return record_frame, hourly
