# Structured feature extraction handoff

## Start here

Open the clean notebook to read the pipeline. Run the command below in PowerShell. It reads the existing MIMIC-IV v3.1 release and writes the features and caches under local AppData. Choose a fresh output directory if retaining earlier caches. The executed notebook, source events and patient-level Parquet belong on the local machine.

From the repository root in PowerShell, use Python 3.11+:

~~~powershell
python -m pip install "duckdb==1.4.4"
$python = "python"
$mimic = Read-Host "Enter the path to your local MIMIC-IV v3.1 folder"
$out = Join-Path $env:LOCALAPPDATA "AKI-feature-extraction\output-v2"
& $python -X utf8 -u .\src\features\run_extraction.py --mode pilot --mimic-dir $mimic --output-dir $out
& $python -X utf8 .\src\features\verify_extraction.py "$out\pilot"
~~~

To run the fictional fixture without MIMIC data:

~~~powershell
& $python -X utf8 .\src\features\run_extraction.py --synthetic --output-dir $out
~~~

Use **--mode full** for the full adult ICU grid. Use **--synthetic** to run the fictional fixture. Pass **--mimic-dir** to override the path to the release root. The lookback defaults to six hours. **--lookback-hours 24** selects the Review 2 PPTX's rolling lookback with partial ICU history at early cutoffs. The stride stays at six hours. Use a separate output directory for comparisons because a successful rerun replaces the mode's features.parquet. The source caches can be reused for the same input and cohort.

## Gap audit

The project initially contained a single clean notebook. The separate local working folder held the installed DuckDB 1.4.4 environment and earlier pilot output. The v3.1 release has both ICU and hospital modules. No teammate label file or project drug definition was found in the project or local working folder.

| Requirement | Previous implementation | Gap and priority | Verification |
|---|---|---|---|
| Adult ICU cohort | Notebook ICU spine joined to patients and admissions, age ≥18, first cutoff before discharge | Retain as provisional extraction eligibility. Label-based risk sets are pending. High | Count stays and check unique keys and cutoff grid |
| Hourly alignment and six-hour stride | Notebook ICU-relative hour index, disjoint six-hour windows | Review 2 PDF p. 10 and the current request specify 6 h at a 6 h stride. The alternate Review 2 PPTX FR-06 specifies 24 h at a 6 h stride. Configurable 6 h default. High | Check bounds and cutoff edges |
| Vitals, demographics, creatinine and urine | Notebook core seven features and five counts | Reuse item mappings and ICU assignment. Preserve nulls and recorded zeros. High | Local dictionaries, unit eligibility, fixture and Python audit |
| Renal trends | No slopes in notebook | Add two within-window slopes. High | Exact fictional slopes and independent sample audit |
| Timed drug exposure | No medication extraction in notebook | Add ICU administration segment starts for four local dictionary items. Broader coverage is pending. High | Late, boundary, duplicate and invalid segments in fixture plus real exposure counts |
| Additional labs | No other labs mapped in notebook or specified by FR-06 | BUN, potassium and lactate are follow-up candidates. Medium | Map blood assays and profile valid, available results by cutoff |
| Weight-normalized urine | Notebook records mL | Defer normalization pending point-in-time weight coverage and a rule for timed weights. Medium | Verify kg/lb mapping and weight timestamps before any division |
| Stage 2/3 handoff | Notebook documents join; no target table | Validate handoff key now. Teammate to define risk sets and horizon ascertainment. High | Unique grid now, label key coverage and cardinality once available |
| Reproducible execution | Notebook cells run in order | Added CLI and independent Python audit. High | Synthetic, pilot and full runs |

## Feature dictionary

One row per **(stay_id, prediction_time)**. The file has **18 candidate inputs** and **7 metadata columns**. Metadata are subject_id, hadm_id, stay_id, intime, outtime, window_start and prediction_time. Keep these out of model inputs. Use subject_id for patient grouping.

| Columns | Source and mapping | Point-in-time definition |
|---|---|---|
| age_at_admission, recorded_gender | hosp.patients joined on subject_id and hosp.admissions joined on subject_id/hadm_id | Anchor age plus admission year minus anchor year. Recorded gender as supplied. MIMIC's de-identified anchor age groups older patients. |
| heart_rate_bpm, heart_rate_n | icu.chartevents 220045, bpm | Latest valid value and number of eligible records |
| sbp_mmhg, sbp_n | icu.chartevents 220050 / 220179, mmHg | Latest valid arterial or noninvasive systolic pressure and count |
| dbp_mmhg, dbp_n | icu.chartevents 220051 / 220180, mmHg | Latest valid arterial or noninvasive diastolic pressure and count |
| creatinine_mg_dl, creatinine_n, creatinine_slope_mg_dl_per_hour | hosp.labevents 50912 Blood, mg/dL | Latest valid creatinine, count, and least-squares slope over distinct chart times, using one value per time |
| urine_output_ml, urine_n, urine_hourly_ml_slope | icu.outputevents 226557, 226558, 226559, 226560, 226561, 226563, 226564, 226565, 226567, 226584, mL | Total recorded volume, count and least-squares slope of hourly totals. Irrigant-related items excluded. Missing hours are omitted from the fit. |
| vancomycin_segments_n, amikacin_segments_n, gentamicin_segments_n, tobramycin_segments_n | icu.inputevents 225798, 225840, 225875, 225902 respectively | Distinct order IDs by drug starting in the window and documented by cutoff. Positive finite amount and valid status required. Units retained for audit. No dose or duration estimated. |

ICU tables join on subject_id, hadm_id and stay_id. Labs require subject_id, hadm_id and charttime inside an ICU interval. If intervals overlap, assign each source event to the latest ICU admission then the lowest stay_id before applying the pilot filter. Pre-ICU labs and missing admission IDs are outside the extraction. All dynamic families use the later of measurement/administration start and storetime. Unknown storetime leaves the record in the cache and ineligible for features. Storage time is a proxy for availability. ICU-relative hours are half-open. Prediction times begin at intime + 6 h and end strictly before outtime. The window is [max(intime, cutoff − lookback), cutoff), with documentation allowed through the cutoff itself.

Both slopes are null with fewer than two distinct times or observed hour bins. Recorded zero urine is valid, and absent urine remains null. Counts are zero with no eligible records. A zero drug count means no qualifying ICU segment was recorded by cutoff. Some actual administrations may be in eMAR only. The four drug items therefore measure a limited observable exposure family. Rewritten/flushed input segments are filtered using the available source status, whose revision history cannot be recovered. The notebook does no imputation or scaling.

Follow-up candidates are BUN (51006, mg/dL), potassium (50971, mEq/L), and lactate (50813, mmol/L) from hosp.labevents. Local d_labitems confirms all three are blood assays. Check actual event units before enabling them. Use charttime and storetime as for creatinine. Routine chemistry is expected to have more coverage than indication-driven lactate in short windows. Local coverage for these candidates has not been measured. For broader medication coverage, use hosp.emar charttime and storetime with confirmed administration statuses and map medication names and aliases, then resolve hospital-to-ICU assignment. Expect incomplete eMAR coverage across eras. Orders in prescriptions and POE would require a separate order feature. For future weight normalization, the local dictionary identifies admission weight 226512 (kg) and 226531 (labelled lbs, unit name absent). Only use validated event units or an explicit, checked pounds-to-kg conversion, with weight available by cutoff. [PhysioNet v3.1](https://physionet.org/content/mimiciv/3.1/) · [MIT-LCP chemistry](https://github.com/MIT-LCP/mimic-code/blob/main/mimic-iv/concepts/measurement/chemistry.sql) · [MIT-LCP concepts](https://github.com/MIT-LCP/mimic-code/tree/main/mimic-iv/concepts) · [ICU inputevents](https://mimic.mit.edu/docs/iv/modules/icu/inputevents.html) · [MIMIC eMAR documentation](https://mimic.mit.edu/docs/iv/modules/hosp/emar.html)

## Synthetic walkthrough

Fictional stay 100 enters ICU at 01:15. The first prediction is 07:15, using [01:15, 07:15).

| Event | Effect |
|---|---|
| HR 70 at 01:15, HR 80 at 07:14 stored 07:15 | Both enter. Latest HR 80, count 2 |
| HR 99 at 07:14 stored 07:16, HR 98 without storetime | Both excluded by availability |
| HR 90 at 07:15 and HR 92 at 07:16 | Both enter the next window |
| BP 120/70 at 05:00 | SBP 120 and DBP 70, one record each |
| Creatinine 1.0 at 03:00 and 1.4 at 05:00 | Latest 1.4, count 2, slope (1.4 − 1.0) / 2 = 0.20 mg/dL/h |
| Creatinine 0.6 at 00:45, 1.9 at 07:00 stored 09:00 | First is pre-ICU, second is late |
| Urine 0 at 03:00, 40 at 05:00, 20 at 05:05 | Hour bins 1 and 3 total 0 and 60 mL. Sum 60 mL, count 3, slope (60 − 0) / (3 − 1) = 30 mL per hour of ICU time |
| Vancomycin at 04:00, duplicated order ID, documented 04:05 | One segment, regardless of the end time |
| Gentamicin stored 08:00, amikacin marked Rewritten, tobramycin starts 07:15 | Counts of zero at 07:15. Tobramycin enters the next window. |

The resulting candidate inputs are age 46, gender F, HR 80 (2), SBP 120 (1), DBP 70 (1), creatinine 1.4 (2), urine 60 (3), slopes 0.20 and 30, and drug counts 1/0/0/0 in dictionary order. The second window has no urine records and returns null with count zero.

## Label handoff

The **historical lookback** defines which data enter a feature row. The **future horizon** determines whether the outcome happens in (prediction_time, prediction_time + 6, 12 or 24 h]. For each horizon, join on stay_id, evaluate onset relative to the cutoff, then reduce to one outcome per feature key. Check unmatched stays and key multiplicity. For incident AKI, settle how prevalent AKI and Stage 1 at the cutoff are handled. For Stage 1 progression, require Stage 1 at the cutoff and no prior Stage 2/3 under the agreed episode definition. Decide how ICU discharge, death, and insufficient ascertainment censor windows. Creatinine and urine also define the KDIGO endpoint, so test a lead-time gap or renal-feature ablation after joining labels. The extraction cannot verify clinical lead time until label timestamps are available.

## Pilot run results

MIMIC-IV v3.1, DuckDB 1.4.4, default six-hour lookback and stride. The pilot command above used the same settings as the validation run, which wrote to a local temporary root. Timings for chart, urine, lab and drug scans plus cache writes were 217.85, 3.40, 145.77 and 21.30 seconds respectively. The feature aggregation and write took 0.17 seconds, with 392.41 seconds end to end. Each cold scan reads the compressed source even for a small cohort. The full run starts with the same scan cost, plus a larger join, cache write and aggregation.

The pilot had 200 stays and 2,970 windows, with 18 candidate inputs in a 25-column Parquet. Independent Python recomputation matched the source-cache values for 22 windows. A separate direct query of outputevents.csv.gz matched urine totals and counts for 12 windows. The synthetic run passed all three fixture rows. A synthetic 24-hour run passed key, boundary and availability checks.

| Feature | Missing in pilot | Observed in pilot |
|---|---:|---:|
| Age and recorded gender, each | 0 | 2,970 |
| Heart rate | 52 | 2,918 |
| Systolic and diastolic BP, each | 89 | 2,881 |
| Latest creatinine | 1,956 | 1,014 |
| Urine total | 588 | 2,382 |
| Creatinine slope | 2,935 | 35 |
| Urine slope | 1,064 | 1,906 |
| Five observation counts, each | 0 | 2,970 |

Drug counts have a value for every row. Positive windows were vancomycin 244, amikacin 0, gentamicin 6 and tobramycin 4. Read these as evidence of recorded starts. The pilot cached 68,354 vital/lab/urine observations, of which five chart values failed numeric eligibility. Additional cached events can be outside a prediction window or unavailable by its cutoff. The 98.8% missing creatinine slope is expected under this short lookback and the two-time minimum. Compare longer lookback coverage before relying on it.

A second real 200-stay pilot with a 24-hour lookback and the same cutoffs reused all four caches and finished in 4.99 seconds. Independent recomputation matched 21 windows. Creatinine slope coverage increased from 35 to 1,292 of 2,970 windows (1.2% to 43.5%), and urine slope coverage increased from 1,906 to 2,528 (64.2% to 85.1%). Latest creatinine coverage increased from 1,014 to 2,703 windows. This measures data availability under a different historical lookback. Predictive value is still open.

## Full-cohort run results

The first cold run finished in 549.96 seconds on MIMIC-IV v3.1 and produced 1,324,351 feature windows for 93,224 adult ICU stays. Chart, urine, lab and drug scans plus cache writes took 301.17, 7.42, 180.53 and 26.37 seconds. Cutoff generation took 0.77 seconds, and the feature build and write took 19.43 seconds. All built-in key, boundary, availability and lab uniqueness checks passed. The validation process used a sandbox temporary directory that was removed at exit. The persistent run uses the AppData path above.

| Feature | Missing in full | Observed in full |
|---|---:|---:|
| Age and recorded gender, each | 0 | 1,324,351 |
| Heart rate | 12,928 (1.0%) | 1,311,423 |
| Systolic BP | 51,622 (3.9%) | 1,272,729 |
| Diastolic BP | 51,670 (3.9%) | 1,272,681 |
| Latest creatinine | 878,082 (66.3%) | 446,269 |
| Urine total | 249,965 (18.9%) | 1,074,386 |
| Creatinine slope | 1,313,392 (99.2%) | 10,959 |
| Urine slope | 469,877 (35.5%) | 854,474 |
| Five observation counts, each | 0 | 1,324,351 |

Positive documented starts were seen in 120,571 vancomycin windows, 351 amikacin windows, 2,188 gentamicin windows and 1,651 tobramycin windows. All drug-count fields are populated. There were 30,260,907 cached vital, lab and urine observations. Of those, 4,901 had invalid numeric values under the extractor rules and were ineligible. That count excludes source records filtered before caching by item, stay or interval. Units reported for accepted observations were bpm, mmHg, mg/dL and mL. Drug units varied because the features count segments without summing amounts.

The persistent full run reproduced the same counts in 558.53 seconds. Scan/cache times were 358.95 s chart, 7.90 s urine, 151.86 s labs and 19.14 s drug. Feature aggregation and write took 10.60 s. The independent Python audit matched all 24 sampled full-cohort windows.

~~~powershell
& $python -X utf8 -u .\src\features\run_extraction.py --mode full --mimic-dir $mimic --output-dir $out
& $python -X utf8 .\src\features\verify_extraction.py "$out\full"
~~~

With the commands above, the full output is at %LOCALAPPDATA%\AKI-feature-extraction\output-v2\full\features.parquet. Open this handoff first for aggregate results. Use the local notebook or DuckDB to inspect patient-level data.

## Completion checklist

- Reused the adult stay spine, core measurements, observation counts and six-hour grid.
- Added configurable lookback, creatinine and urine slopes, and four administration start counts.
- Verified fictional boundary and timing cases, real six- and 24-hour pilots, and the full six-hour extraction.
- Pending the teammate's label timestamps, confirm the first-onset versus progression definition, join coverage, censoring and the near-onset renal-feature audit.

See the [synthetic feature output](SYNTHETIC_FEATURE_OUTPUT.md) for all 25 fields across three fictional prediction windows.
