# MIMIC-IV Structured Feature Discovery

This folder contains aggregate local discovery results for MIMIC-IV v3.1 structured ICU measurements and input events. It does not contain the source dataset or patient-level records.

The chart-event scan covers the first 1,000,000 rows; the input-event scan covers the first 500,000 rows. Both are bounded, sequential scans, not complete dataset statistics or random samples. The `d_items` and `icustays` tables were read completely. Counts and unit observations are limited to those scan windows.

Candidate item IDs and medication units are marked `UNVERIFIED - TEAM REVIEW REQUIRED`. They are discovery evidence only and must not be used as approved clinical mappings. Final hourly features and medication exposures were not generated because approved mappings and a cohort CSV were unavailable.

See `validation_report.txt` and `run_summary.json` for the run status and blockers.
