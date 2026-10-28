"""Command line interface for local MIMIC-IV preprocessing."""

from __future__ import annotations

import argparse
from pathlib import Path

import pandas as pd

from .common import PreprocessingError
from .measurements import aggregate_measurements
from .medications import process_medications


def _mapping_csv(path: str) -> list[dict]:
    frame = pd.read_csv(path, keep_default_na=False)
    mappings = frame.to_dict(orient="records")
    for row in mappings:
        row["enabled"] = str(row.get("enabled", "")).strip().lower() in {"true", "1", "yes"}
        if "itemid" in row and row["itemid"] != "":
            row["itemid"] = int(row["itemid"])
    return mappings


def _items(path: str) -> pd.DataFrame:
    return pd.read_csv(path, compression="infer", usecols=["itemid", "label"])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="mimic-preprocess")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("measurements", "medications"):
        command = sub.add_parser(name)
        command.add_argument("--events", required=True, help="chartevents.csv.gz or inputevents.csv.gz")
        command.add_argument("--stays", required=True, help="icustays.csv.gz")
        command.add_argument("--d-items", required=True, help="d_items.csv.gz")
        command.add_argument("--mapping", required=True, help="Reviewed mapping CSV")
        command.add_argument("--output", required=True, help="Local CSV output path")
        command.add_argument("--chunksize", type=int, default=500_000)
        command.add_argument("--stay-id", type=int, action="append", help="Optional stay ID filter; repeat as needed")
    args = parser.parse_args(argv)
    mappings = _mapping_csv(args.mapping)
    dictionary = _items(args.d_items)
    stay_ids = set(args.stay_id) if args.stay_id else None
    destination = Path(args.output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    if args.command == "measurements":
        output = aggregate_measurements(args.events, args.stays, mappings, args.chunksize, stay_ids, dictionary)
        output.to_csv(destination, index=False)
        print(f"Wrote {len(output)} hourly measurement rows to {destination}")
        print(f"Dropped invalid timestamps: {output.attrs['invalid_timestamps']}; nonnumeric values: {output.attrs['invalid_numeric_values']}")
    else:
        records, hourly = process_medications(args.events, args.stays, mappings, args.chunksize, stay_ids, dictionary)
        records_path = destination.with_name(destination.stem + "_records" + destination.suffix)
        records.to_csv(records_path, index=False)
        hourly.to_csv(destination, index=False)
        print(f"Wrote {len(hourly)} hourly exposure rows to {destination}")
        print(f"Wrote {len(records)} traceable source rows to {records_path}")
        print(f"Invalid starts: {hourly.attrs['invalid_start_timestamps']}; invalid infusion intervals: {hourly.attrs['invalid_infusion_intervals']}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except PreprocessingError as exc:
        raise SystemExit(f"Configuration/input error: {exc}") from exc
