"""Independently recompute deterministic sample windows from cached source events."""
import argparse
from collections import defaultdict
from math import isclose, isfinite
from pathlib import Path
from statistics import linear_regression

import duckdb

def verify(con, features, observation_files, drug_file):
    def matches(actual, expected):
        return (actual is None if expected is None else
                actual is not None and isclose(actual, expected, abs_tol=1e-9))

    columns = {
        "heart_rate": ("heart_rate_bpm", "bpm"), "sbp": ("sbp_mmhg", "mmHg"),
        "dbp": ("dbp_mmhg", "mmHg"), "creatinine": ("creatinine_mg_dl", "mg/dL"),
        "urine": ("urine_output_ml", "mL"),
    }
    drugs = {225798: "vancomycin", 225840: "amikacin", 225875: "gentamicin", 225902: "tobramycin"}
    # Sample across the grid plus up to two positive examples for each sparse feature.
    relation = con.read_parquet(str(features))
    relation.create_view("audit_features", replace=True)
    sparse = ["creatinine_slope_mg_dl_per_hour", "urine_hourly_ml_slope",
              *(f"{name}_segments_n" for name in drugs.values())]
    sample_queries = ["(SELECT * FROM audit_features ORDER BY md5(stay_id || ':' || prediction_time), stay_id, prediction_time LIMIT 12)"]
    for column in sparse:
        predicate = f"{column} > 0" if column.endswith("_n") else f"{column} IS NOT NULL"
        sample_queries.append(f"(SELECT * FROM audit_features WHERE {predicate} ORDER BY stay_id, prediction_time LIMIT 2)")
    result = con.execute(" UNION ".join(sample_queries))
    names = [col[0] for col in result.description]
    sample = [dict(zip(names, row)) for row in result.fetchall()]
    for row in sample:
        stay, start, cutoff = (row[k] for k in ("stay_id", "window_start", "prediction_time"))
        eligible = defaultdict(list)
        for kind, item, time, store, value, unit in con.execute("""
            SELECT feature, itemid, charttime, storetime, raw_value, valueuom
            FROM read_parquet(?) WHERE stay_id = ? AND charttime >= ? AND charttime < ?
        """, [[str(p) for p in observation_files], stay, start, cutoff]).fetchall():
            if (store is not None and max(time, store) <= cutoff
                    and value is not None and isfinite(value)
                    and (value > 0 or (kind == "urine" and value == 0))
                    and (unit or "").strip().casefold() == columns[kind][1].casefold()):
                eligible[kind].append((time, max(time, store), -item, value))
        for kind, (column, _) in columns.items():
            events = eligible[kind]
            expected = None
            if events:
                expected = sum(e[3] for e in events) if kind == "urine" else max(events)[3]
            assert matches(row[column], expected), f"Sample mismatch for {column}"
            assert row[f"{kind}_n"] == len(events), f"Sample mismatch for {kind} count"

        creatinine = {}
        for time, _, _, value in sorted(eligible["creatinine"]):
            creatinine[time] = value
        urine = defaultdict(float)
        for time, _, _, value in eligible["urine"]:
            urine[int((time - row["intime"]).total_seconds() // 3600)] += value
        for column, points in (
            ("creatinine_slope_mg_dl_per_hour",
             {(t - cutoff).total_seconds() / 3600: y for t, y in creatinine.items()}),
            ("urine_hourly_ml_slope", urine),
        ):
            expected = linear_regression(list(points), list(points.values())).slope if len(points) >= 2 else None
            assert matches(row[column], expected), f"Sample mismatch for {column}"

        segments = defaultdict(set)
        for item, order, time, store, amount, status in con.execute("""
            SELECT itemid, orderid, charttime, storetime, amount, statusdescription
            FROM read_parquet(?) WHERE stay_id = ? AND charttime >= ? AND charttime < ?
        """, [str(drug_file), stay, start, cutoff]).fetchall():
            if (order is not None and store is not None and max(time, store) <= cutoff
                    and amount is not None and isfinite(amount) and amount > 0
                    and status is not None and status.strip().casefold() not in {"rewritten", "flushed"}):
                segments[item].add(order)
        for item, drug in drugs.items():
            assert row[f"{drug}_segments_n"] == len(segments[item]), f"Sample mismatch for {drug}"
    print(f"Independent Python recomputation passed for {len(sample)} windows from cached source events.")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dir", type=Path)
    args = parser.parse_args()
    def cache(table):
        candidates = list(args.run_dir.glob(f"{table}-*.parquet"))
        if len(candidates) != 1:
            parser.error(f"Expected one current {table} cache in the run directory.")
        return candidates[0]
    with duckdb.connect() as con:
        verify(con, args.run_dir / "features.parquet",
               [cache(table) for table in ("chartevents", "outputevents", "labevents")],
               cache("inputevents"))
