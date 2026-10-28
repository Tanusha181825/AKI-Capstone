import pandas as pd
import pytest

from mimic_preprocessing.common import PreprocessingError
from mimic_preprocessing.measurements import aggregate_measurements, OUTPUT_COLUMNS, validate_measurement_mappings
from mimic_preprocessing.medications import process_medications, HOURLY_COLUMNS


@pytest.fixture
def stays():
    return pd.DataFrame({"stay_id": [10, 20], "intime": ["2020-01-01 00:00:00"] * 2,
                         "outtime": ["2020-01-01 04:00:00"] * 2})


@pytest.fixture
def measurement_map():
    return [{"itemid": 1, "feature_name": "demo", "source_unit": "u", "canonical_unit": "u",
             "conversion_rule": "identity", "enabled": True, "approval_status": "approved"},
            {"itemid": 2, "feature_name": "other", "source_unit": "u", "canonical_unit": "u",
             "conversion_rule": "identity", "enabled": True, "approval_status": "approved"}]


def chart(rows):
    return pd.DataFrame(rows, columns=["stay_id", "charttime", "itemid", "valuenum", "valueuom"])


def test_hourly_aggregation_and_stay_feature_separation(stays, measurement_map):
    data = chart([(10, "2020-01-01 00:01", 1, 2, "u"), (10, "2020-01-01 00:59", 1, 4, "u"),
                  (10, "2020-01-01 01:00", 1, 8, "u"), (20, "2020-01-01 00:01", 1, 100, "u"),
                  (10, "2020-01-01 00:02", 2, 9, "u")])
    result = aggregate_measurements(data, stays, measurement_map)
    h0 = result[(result.stay_id == 10) & (result.hour == 0) & (result.feature_name == "demo")].iloc[0]
    assert (h0["mean"], h0["min"], h0["max"], h0["last"], h0["count"]) == (3.0, 2.0, 4.0, 4.0, 2)
    assert len(result) == 4
    assert result.columns.tolist() == OUTPUT_COLUMNS


def test_last_is_deterministic_and_chunk_boundaries_merge(tmp_path, stays, measurement_map):
    rows = [(10, "2020-01-01 00:30", 1, 2, "u"), (10, "2020-01-01 00:10", 1, 1, "u"),
            (10, "2020-01-01 00:30", 1, 3, "u")]
    events = tmp_path / "events.csv"
    chart(rows).to_csv(events, index=False)
    chunked = aggregate_measurements(events, stays, measurement_map, chunksize=1)
    together = aggregate_measurements(events, stays, measurement_map, chunksize=100)
    pd.testing.assert_frame_equal(chunked, together)
    assert chunked.iloc[0]["last"] == 3
    assert chunked.iloc[0]["count"] == 3


def test_invalid_timestamp_nonnumeric_and_unit_mismatch_are_excluded(stays, measurement_map):
    data = chart([(10, "not-a-time", 1, 2, "u"), (10, "2020-01-01 00:01", 1, "bad", "u"),
                  (10, "2020-01-01 00:02", 1, 3, "other")])
    result = aggregate_measurements(data, stays, measurement_map)
    assert result.empty
    assert result.attrs["invalid_timestamps"] == 1
    assert result.attrs["invalid_numeric_values"] == 1
    assert result.attrs["unit_mismatches"] == 1
    assert result.dtypes["hour"] == "int64"


def test_stay_boundaries_and_no_future_fill(stays, measurement_map):
    data = chart([(10, "2019-12-31 23:59", 1, 0, "u"), (10, "2020-01-01 00:00", 1, 1, "u"),
                  (10, "2020-01-01 03:00", 1, 7, "u"), (10, "2020-01-01 04:00", 1, 9, "u")])
    result = aggregate_measurements(data, stays, measurement_map)
    assert result[["hour", "last"]].values.tolist() == [[0, 1.0], [3, 7.0]]
    assert 1 not in result.hour.tolist() and 2 not in result.hour.tolist()


def test_mapping_rejects_unapproved_or_unsupported_conversion(measurement_map):
    measurement_map[0]["approval_status"] = "pending"
    with pytest.raises(PreprocessingError, match="not clinically approved"):
        validate_measurement_mappings(measurement_map)
    measurement_map[0]["approval_status"] = "approved"
    measurement_map[0]["conversion_rule"] = "convert-magically"
    with pytest.raises(PreprocessingError, match="Unsupported conversion_rule"):
        validate_measurement_mappings(measurement_map)
    with pytest.raises(PreprocessingError, match="No enabled"):
        validate_measurement_mappings([{"itemid": "", "enabled": False}])


def test_bad_schemas_and_empty_inputs(stays, measurement_map):
    with pytest.raises(PreprocessingError, match="missing required columns"):
        aggregate_measurements(pd.DataFrame({"x": []}), stays, measurement_map)
    empty = chart([])
    result = aggregate_measurements(empty, stays, measurement_map)
    assert result.empty
    assert result.columns.tolist() == OUTPUT_COLUMNS
    assert result.dtypes["stay_id"] == "int64"


@pytest.fixture
def medication_map():
    return [{"itemid": 3, "medication_name": "example bolus", "medication_class": "pending class",
             "administration_semantics": "bolus", "enabled": True, "approval_status": "approved"},
            {"itemid": 4, "medication_name": "example infusion", "medication_class": "pending class",
             "administration_semantics": "infusion", "enabled": True, "approval_status": "approved"}]


def inputevents(rows):
    columns = ["stay_id", "itemid", "starttime", "endtime", "amount", "amountuom", "rate", "rateuom", "orderid", "linkorderid"]
    return pd.DataFrame(rows, columns=columns)


def test_bolus_infusion_intervals_missing_times_and_hour_boundaries(stays, medication_map):
    data = inputevents([(10, 3, "2020-01-01 01:00", None, 5, "mg", None, None, 7, 7),
                        (10, 4, "2020-01-01 00:30", "2020-01-01 02:00", 1, "mg", 2, "mg/h", 8, 8),
                        (10, 4, "2020-01-01 02:00", None, 1, "mg", 2, "mg/h", 9, 9),
                        (10, 4, None, "2020-01-01 03:00", 1, "mg", 1, "mg/h", 10, 10)])
    records, hourly = process_medications(data, stays, medication_map)
    bolus = hourly[hourly.medication_name == "example bolus"]
    infusion = hourly[hourly.medication_name == "example infusion"]
    assert bolus[["hour", "exposure_indicator"]].values.tolist() == [[1, 1]]
    assert infusion.hour.tolist() == [0, 1]
    assert infusion.exposure_count.tolist() == [1, 1]
    assert len(records) == 4  # invalid start remains traceable in source-record output
    assert hourly.columns.tolist() == HOURLY_COLUMNS
    assert hourly.dtypes["exposure_indicator"] == "int8"
    assert hourly.attrs["invalid_infusion_intervals"] == 1


def test_overlapping_and_duplicate_medication_records(stays, medication_map):
    record = (10, 4, "2020-01-01 00:00", "2020-01-01 02:00", 1, "mg", 1, "mg/h", 11, 11)
    records, hourly = process_medications(inputevents([record, record]), stays, medication_map)
    assert records["_source_record_id"].tolist() == [0, 1]
    assert hourly.exposure_count.tolist() == [2, 2]
    assert hourly.iloc[0].source_record_ids == "0;1"


def test_invalid_input_interval_and_missing_schema(stays, medication_map):
    invalid = inputevents([(10, 4, "2020-01-01 03:00", "2020-01-01 02:00", 1, "mg", 1, "mg/h", 1, 1)])
    _, hourly = process_medications(invalid, stays, medication_map)
    assert hourly.empty
    assert hourly.columns.tolist() == HOURLY_COLUMNS
    with pytest.raises(PreprocessingError, match="missing required columns"):
        process_medications(pd.DataFrame({"bad": []}), stays, medication_map)
