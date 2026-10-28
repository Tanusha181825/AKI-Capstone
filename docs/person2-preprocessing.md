structured measurements and medication preprocessing

## Scope and sources

This package filters local MIMIC-IV v3.1 files by a team-reviewed, explicit `itemid` allowlist. It reads:

* `icu/chartevents.csv.gz`: `stay_id`, `charttime`, `itemid`, `valuenum`, `valueuom`.
* `icu/inputevents.csv.gz`: `stay_id`, `itemid`, `starttime`, `endtime`, `amount`, `amountuom`, `rate`, `rateuom`, and (when available) `orderid`, `linkorderid`.
* `icu/d_items.csv.gz`: `itemid`, `label` (used to validate mapped IDs exist; descriptions do not approve mappings).
* `icu/icustays.csv.gz`: `stay_id`, `intime`, `outtime`.

The pipeline does not modify Person 1's cohort or KDIGO labels and does not implement predictions, ML, clinical-note preparation, or eICU harmonization.

## Installation and tests

Python 3.10+ and pandas 2+ are required. From the repository root:

```powershell
python -m pip install -e ".[test]"
python -m pytest
```

## Mapping review

Start from `config/measurement_mapping.template.csv` and `config/medication_mapping.template.csv`. These contain no item IDs. Review MIMIC's local `d_items.csv.gz` labels and the corresponding source records, then have the project clinical reviewer verify the chosen IDs, units, definitions, routes, and administration semantics. Record the decision in the mapping `notes` field and set `enabled=true,approval_status=approved` only after approval. Empty, disabled or pending rows are not selected. The pipeline rejects enabled but unapproved mappings.

Measurement columns are `itemid`, `feature_name`, `source_unit`, `canonical_unit`, `conversion_rule`, `enabled`, `approval_status`, and `notes`. The implementation currently accepts only `conversion_rule=identity`, and only when each raw `valueuom` exactly matches its declared `source_unit`. Unsupported conversions and unit mismatches do not get silently combined; mismatching records are omitted and counted in the returned frame's `unit_mismatches` diagnostic attribute. A future conversion should be added only with a reviewed, explicit rule and tests.

Medication columns are `itemid`, `medication_name`, `medication_class`, `required_unit`, `required_route`, `administration_semantics`, `enabled`, `approval_status`, and `notes`. The semantics must be explicitly approved as `bolus` or `infusion`; `pending` cannot be enabled. `d_items` labels are reference text only, not validation of medication class or nephrotoxicity.

## Paths and sample run

Use explicit local paths; no Windows path is hardcoded. Test a handful of stay IDs first and write outputs outside the repository (the ignore rules also exclude common patient-data files):

```powershell
mimic-preprocess measurements `
  --events "D:\mimic\icu\chartevents.csv.gz" `
  --stays "D:\mimic\icu\icustays.csv.gz" `
  --d-items "D:\mimic\icu\d_items.csv.gz" `
  --mapping ".\config\my_measurements.csv" `
  --stay-id 30000123 --stay-id 30000456 `
  --chunksize 100000 --output "D:\mimic-work\measurements_sample.csv"

mimic-preprocess medications `
  --events "D:\mimic\icu\inputevents.csv.gz" `
  --stays "D:\mimic\icu\icustays.csv.gz" `
  --d-items "D:\mimic\icu\d_items.csv.gz" `
  --mapping ".\config\my_medications.csv" `
  --stay-id 30000123 --stay-id 30000456 `
  --chunksize 50000 --output "D:\mimic-work\medication_hourly_sample.csv"
```

Install the package first with `python -m pip install -e .` to expose `mimic-preprocess`; alternatively invoke `python -m mimic_preprocessing.cli` from an installed environment. Increase `--chunksize` only when memory permits. Pandas holds one input chunk plus the compact stay table and accumulated output statistics in memory; measurement aggregation retains one state per observed `(stay, hour, feature, unit)` group, not raw chart events. Medication mode holds filtered source records in memory to preserve a traceable companion output, so its footprint scales with mapped administrations. For very large medication selections, run one or a few mapped classes/stay batches at a time. CSV output is convenient but larger than Parquet; no Parquet dependency is added.

## Output contracts and time behavior

Hourly measurement CSV columns are `stay_id` (int64), `hour` (int64 elapsed whole hour from ICU `intime`), `feature_name`, `source_unit`, `canonical_unit`, `mean`, `min`, `max`, `last` (float64), and `count` (int64). One row exists only for an observed, valid feature/hour. Hour calculation is `floor((charttime-intime)/1 hour)`, with `intime` included and `outtime` excluded. Invalid timestamps and nonnumeric values are excluded and counted in DataFrame diagnostic attributes (printed by the CLI for timestamp/numeric counts). Duplicate chart rows remain distinct observations and contribute to count. For tied timestamps, last uses stable source order across chunks. There is no forward/back fill or other imputation.

Medication mode writes the requested hourly exposure CSV and a companion `<stem>_records.csv`. The source-record table retains the available `inputevents` fields, a deterministic filtered-source `_source_record_id`, standardized mapping labels, and the approved semantics. Hourly exposure columns are `stay_id`, elapsed `hour`, `medication_name`, `medication_class`, `exposure_indicator` (1), `exposure_count`, and `source_record_ids`. These are exposure indicators/counts of mapped source records, not dose or infusion-rate features. Amount/rate values and units are retained only in the companion records; no cumulative dose, dose equivalence, or class toxicity is inferred.

Approved boluses contribute to exactly the hour containing their valid `starttime`; their end time does not make them continuous. An approved infusion requires a valid `endtime > starttime` and contributes once to every overlapped hour in the half-open interval `[starttime,endtime)`. It is clipped at ICU `outtime`; missing/invalid starts produce no exposure, and missing/nonpositive infusion intervals produce no exposure. Overlapping records each count separately; exact duplicate records are preserved as separate input rows. The mapping's `required_unit` and `required_route` are review metadata and are not dose conversion logic.

## Leakage and limitations

All output hours preserve ICU-relative timing so downstream cohort/prediction-time code can enforce its horizon. The package does not have Person 1's cohort labels or prediction cutoffs, so it cannot perform a target-aware leakage audit. It never fills earlier hours from later values and does not itself trim records to a prediction cutoff. Downstream integration must apply the approved prediction time/horizon before using features. Clinical item selection, unit definitions/conversions, medication class labels, routes, and bolus/infusion semantics still require team and clinical review. MIMIC duplicate rows are treated as observed records; determining whether a particular duplicate is erroneous is outside this package.
