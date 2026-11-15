# Approved KDIGO labels — 14 November 2026

Scope: labels only. No features, prediction windows, models, SHAP, pushes or merges.
The clinical policy below was explicitly approved by the project owner on 14 November 2026.
These are conservative algorithmic evidence labels, not clinician-adjudicated diagnoses.

## Outputs and counts

Exactly two processed files were created:

| File | Rows | Bytes |
|---|---:|---:|
| `data/processed/aki_stay_labels.parquet` | 94,458 | 3,261,773 |
| `data/processed/aki_timeline_labels.parquet` | 5,108,160 | 211,202,831 |

The initial cohort contains 65,366 patients and 94,458 adult ICU stays. All stays are
retained for audit, including short stays and exclusions; no minimum duration is
imposed. Repeated stays are separate and retain subject/admission identifiers.
89,647 stays have no coded ESRD/chronic-dialysis exclusion. Only 14,421 have evidence
supporting incident severe AKI. The latter is an incident-evidence subset, NOT a
complete forecasting cohort or a set of patients known to have timely online labels.

| Maximum assessable non-RRT stage during ICU | Stays |
|---|---:|
| 0 | 43,368 |
| 1 | 15,488 |
| 2 | 18,754 |
| 3 | 7,821 |
| Unknown / excluded | 9,027 |

26,575 stays have confirmed severe evidence; 67,883 have unknown severe outcomes.
There are **zero asserted negative outcomes**. Stage 0/1 means an observed maximum
among assessable measurements, not proven absence of AKI throughout the stay.
Do not fill nullable booleans with false or use stage 0/1 stays as validated controls.

Onset sources: creatinine 6,396; urine 20,175; simultaneous creatinine+urine 4;
RRT alone 0 confirmed acute severe outcomes (RRT remains separately preserved).
5,045 stays have in-ICU RRT evidence, including 2,435 with chronic/ESRD codes.

14,421 stays meet the observed-incident rule. One stay has severe evidence exactly
at ICU admission. 4,572 have severe creatinine evidence before ICU admission;
2,267 of these also have severe evidence during ICU. Pre-ICU history is not proof
that AKI remained present at admission. Admission status is unknown for 80,036
stays, and incident status is unknown for 72,337. 915 stays have no in-ICU timeline
events. There is no attempt to infer an admission state from future measurements.

Exclusion strings (mutually exclusive combinations; counts sum to 94,458):

| Reason string | Stays |
|---|---:|
| negative_outcome_unverified | 62,450 |
| empty: observed incident evidence eligible | 14,421 |
| incident_status_unresolved | 9,887 |
| chronic_dialysis_or_esrd;negative_outcome_unverified | 3,128 |
| preexisting_severe_evidence;incident_status_unresolved | 2,078 |
| chronic_dialysis_or_esrd;preexisting_severe_evidence;negative_outcome_unverified | 1,683 |
| preexisting_severe_evidence;negative_outcome_unverified | 622 |
| preexisting_severe_evidence | 189 |

## Clinical rules and interpretation

The existing seven DuckDB reference tables are attached READ_ONLY and reused.
Official MIT-LCP SQL is untouched. Reference row counts match the prior completed
pipeline. The implementation reuses the extraction and interval methodology in
cohort scripts 21, 33, 36 and 37; it does not rerun the expensive raw extractions.
Only the small diagnosis and procedure CSV sources are additionally scanned.

**Creatinine:** reference prior-only 48-hour/7-day minima; no baseline imputation.
Stage thresholds reproduce the reference/KDIGO logic, including the acute-change
requirement for the >=4 mg/dL stage-3 branch. Baseline-free rows, nonpositive values
or minima, and values outside the existing extraction's >0 to 30 mg/dL provisional
quality range are unassessable, not stage 0. Current and baseline values are
retained. Same-subject prior admissions can contribute, as in the reference;
current-stay assignment is not based on discharge diagnosis. The reference begins
extracting seven days before admission, so baselines for pre-admission events are
left-truncated. No historical outpatient or back-calculated baseline is invented.

**Urine:** reconstruct collection intervals using the existing retained-source
Parquet. First unknown intervals, nonpositive durations, intervals >3 hours,
negative net volumes, and flagged endpoint records do not contribute. Require
100% of each 6/12/24-hour window (floating tolerance 1e-8 hours), replacing the
existing provisional 95% threshold. Allocate volume proportionally over interval
overlaps, using no collection ending after the evaluation time. Thresholds are
unrounded <0.5 mL/kg/h over 6/12h, <0.3 over 24h, and zero volume over 12h.
This avoids treating absent measurements as zero urine. Interval reconstruction
still assumes the recorded volume belongs to the period since the preceding
record; continuous observation is not proven. Preserved coverage and quality
fields allow future clinical review. No severity is extrapolated backward to the
start of an oliguria interval.

**Weight:** reuse existing plausible 20–350 kg weights and unit conversion.
Choose the latest weight available by the evaluation time, requiring nonmissing
storetime >= charttime. Conflicting weights at an identical availability timestamp
are omitted. No admission-weight backfill or future weight is used.

**RRT/chronic dialysis:** active CRRT mode plus system_active=1, or documented
HD/CRRT/peritoneal-dialysis procedures with positive recorded delivery, positive
duration and FinishedRunning/Stopped/Paused status, yield candidate stage-3
evidence. SCUF is omitted because isolated ultrafiltration does not establish AKI.
These records do not establish acute indication or chronicity. `aki_stage_rrt=3`
and `observed_kdigo_stage` retain this evidence, but the primary `aki_stage` and
`severe_aki` require independent non-RRT evidence. Thus RRT-only cases stay unknown,
not negative; acute dialysis-only AKI can be underidentified.

Retrospective admission diagnosis codes ICD-10 N186/Z992 or ICD-9 5856/V4511/V560/
V568 flag ESRD/chronic dialysis (4,811 stays). They suppress primary acute labels
and incident eligibility while preserving observed stages and reference values.
These codes are exclusion evidence, not proof of pre-admission dialysis or online
predictors. Their absence does not rule out chronic dialysis. No treatment record
alone is used to assert new-onset acute disease.

**Combination:** `aki_stage` is the maximum assessable creatinine/urine stage, or
null for coded chronic dialysis/ESRD. It is an observed lower bound when either
modality is missing. `observed_kdigo_stage` additionally includes RRT evidence.
Raw `reference_*` columns are provenance/comparison values, not approved labels.
The official `aki_stage_smoothed` is never used: it partitions by subject and
orders by relative ICU time, mixing repeat stays and potentially future admissions.

**Onset:** `severe_aki_onset` is the earliest qualifying clinical charttime within
the inclusive ICU interval; no stage carry-forward, smoothing, or onset backdating.
`onset_source` records ties. `incident_severe_aki=true` requires earlier in-ICU
nonsevere creatinine AND urine evidence, both available by that clinical onset,
and no severe pre-ICU evidence. This supports first observed onset, not biological
onset. Any pre-ICU severe evidence conservatively precludes incident eligibility,
even if an earlier episode may have recovered. Only exact-admission severe evidence
sets `severe_aki_at_admission=true`; otherwise admission state stays unknown unless
the observed-incident rule supplies earlier nonsevere evidence. All stays have
`onset_timing_uncertain=true`, acknowledging intermittent observation.

## Availability and leakage limits

`charttime` is clinical observation time; it is not the time an online system knew
the result. Criterion-specific `creat_available_at`, `uo_available_at` and
`rrt_available_at` preserve availability where reconstructable. Missing or
storetime-before-charttime values remain uncertain; they are not assumed timely.
Creatinine availability includes all prior reference observations within seven
days and the current aggregate, conservatively delaying the result to the latest
supporting entry. Urine availability includes both collection endpoints and all
overlapping records for the qualifying duration. Weight must already be available.
CRRT chart table lacks storetime; procedure entries can supply it.

`severe_evidence_available_at` is the earliest known availability among qualifying
non-RRT criteria at that row. It may be null even when clinical evidence is severe.
Stay-level `severe_aki_available_onset` is the earliest **known** availability of
any qualifying in-ICU severe evidence, potentially from a later clinical event.
It does not imply the first clinical onset had a known entry time. 14,047 first
clinical severe events have unknown availability; 7,686 severe stays have no known
severe availability anywhere, and 13 first known availability times follow ICU
discharge. The timeline contains 314,286 severe rows with unknown availability.
These are explicit uncertainties; future online evaluation must filter/reconstruct
state using criterion-specific availability, not just clinical time or the
stay-level eligibility flag. Discharge codes and maximum/onset summaries must
never become time-varying predictor inputs. No forecast windows were created.

## Validation

All 19 pipeline checks passed with zero failures: unique stay and timeline keys,
adult/valid ICU intervals, stage ranges, identifier consistency, timeline/stay
maxima and onset agreement, severe classification, onset bounds, availability
bounds, no future weight, full severe-urine coverage, chronic exclusions,
no unproven negatives, supported incident status, nonpositive creatinine handling,
cohort preservation, and persisted Parquet row-count round trips.

Independent reads additionally verified complete stage-1 urine coverage, exact
combined-stage construction, valid severe-onset source evidence, and no severe
availability preceding clinical evidence. All four returned zero violations.
Both output SHA-256 values were independently recomputed and matched the JSON.
Exactly the two requested Parquets are present in data/processed.

Synthetic end-to-end test passed: 16 stays, 15 patients, 19 outcome assertions,
plus all 19 pipeline checks on the fixture. Cases cover 0.3 mg/dL/1.5x/2x/3x and
4 mg/dL boundaries, anuria, exactly 0.5 urine rate, incomplete coverage, unavailable
weights, late results, absent/nonpositive baselines, chronic exclusion, RRT-only
uncertainty, incident and pre-existing evidence, repeated patients and refusing
overwrite. An initial fixture decimal type rounded 2.999 to 3; matching the real
DOUBLE schema corrected the fixture, with no production-label change.

Reference severe evidence within ICU was 43,408 stays before the approved changes;
the approved total is 26,575. These are different methodologies, not a clinical
accuracy estimate. Detailed row-level comparison and counts are in JSON.

SHA-256:

- stay: `8b9dd4671382b8eb010dad667cf5383c0a1b6a71ecb464d1f6a5d41f6d5d0851`
- timeline: `ab1b7a9cbae859bf977a84d84b5591ec408a87a739cb95c2b175348cb6cd57b0`

## Files and reproduction

All six files below are new; no pre-existing data, source, reference SQL, or DuckDB
table was overwritten:

- src/labels/build_final_labels.py
- tests/labels/test_final_labels.py
- data/processed/aki_stay_labels.parquet
- data/processed/aki_timeline_labels.parquet
- reports/labeling/validation.json
- docs/labeling.md

Run tests with `.venv/Scripts/python.exe tests/labels/test_final_labels.py`.
The builder command is `.venv/Scripts/python.exe src/labels/build_final_labels.py`.
It deliberately refuses existing outputs/report. Regeneration requires a separately
approved preservation/replacement plan; do not delete existing results to bypass it.

```python
from pathlib import Path
import pandas as pd

root = Path.cwd()  # run from the repository root
stays = pd.read_parquet(root / 'data/processed/aki_stay_labels.parquet')
timeline = pd.read_parquet(root / 'data/processed/aki_timeline_labels.parquet')
# Preserve null severe_aki and incident_severe_aki values.
```

Clinical reference: KDIGO 2012 AKI guideline, definitions/staging section 2.1 and
Table 2: https://kdigo.org/wp-content/uploads/2016/10/KDIGO-2012-AKI-Guideline-English.pdf

## Repository organization

The labeling test, report and methodology were relocated into tests/labels, reports/labeling and docs. Clinical logic and patient-level output files were not changed. Original data checksums remain authoritative. Configuration now uses config/paths.yaml, with legacy configs/paths.yaml accepted for existing local audit scripts. The four labeling files are committed alongside repository structure/setup files on tanusha; existing collaborators' files are preserved.
