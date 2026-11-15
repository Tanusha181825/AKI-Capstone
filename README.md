# AKI Capstone

Research project for predicting new-onset severe acute kidney injury in ICU patients.
The repository includes a KDIGO labeling pipeline and a structured ICU feature extractor.

## Structure

```text
config/                       # Shared configuration templates
  paths.example.yaml          # Copy to ignored paths.yaml for local data access
docs/
  labeling.md                 # Approved definitions, findings and limitations
  preprocessing.md            # Existing collaborator preprocessing documentation
src/
  features/                   # Feature notebook, runner, and verifier
  labels/
    build_final_labels.py     # Final labeling entry point
tests/
  labels/
    test_final_labels.py      # Synthetic clinical edge cases, no patient data
  test_preprocessing.py       # Existing collaborator tests
reports/
  labeling/
    validation.json           # Aggregate checks, counts and output hashes
data/                         # LOCAL ONLY: interim/, reference/, processed/
pyproject.toml                # Existing package configuration + labeling extras
```

Keep future completed code under `src/cohort/`, `src/features/`, `src/models/`
and `src/evaluation/`, with matching tests and docs. Do not add empty modules or
placeholder implementations. Model training and evaluation remain downstream.
The earlier local `src/cohort/` audit scripts, reference loader, `configs/`, CSV
reports, virtual environment and MIMIC reference checkout are preserved and ignored.
They have not been promoted as a finalized extraction workflow.

## Structured feature extraction

The extractor builds one row per ICU stay and six-hour prediction cutoff using measurements available at that time. See the [setup, validation, and label handoff](docs/EXTRACTION_HANDOFF.md) and the [literature evidence](docs/LITERATURE_EVIDENCE.md).

## Labeling setup

Use Python 3.11 (tested). Install the tested labeling dependencies:

```powershell
python -m pip install "duckdb==1.5.6" "PyYAML==6.0.3"
Copy-Item config/paths.example.yaml config/paths.yaml
```

Edit the ignored `config/paths.yaml` with your authorized local MIMIC-IV directory.
The builder reuses the existing `data/reference/mimic_reference.duckdb` reference
tables and the extraction Parquets documented in [the methodology](docs/labeling.md).
It is a final labeling stage, not a fresh raw-data extractor. A clean clone lacks
those local prerequisites. Do not upload them.

Run the synthetic tests without downloading MIMIC:

```powershell
python -m unittest discover -s tests/labels -p "test_*.py"
```

Generate labels only when approved outputs do not already exist:

```powershell
python src/labels/build_final_labels.py
```

The builder refuses to overwrite existing labels or its validation report.

## Findings

94,458 ICU stays across 65,366 patients; 5,108,160 timeline rows.
26,575 stays (28.1% of all stays) have confirmed severe-AKI evidence;
14,421 (15.3%) meet the observed-incident evidence rule.
67,883 outcomes remain unknown: they are not validated negative controls.
Timing and RRT-indication uncertainty remain explicit. These are algorithmic
evidence labels, not clinician-adjudicated diagnoses or a complete training cohort.

See [labeling definitions and results](docs/labeling.md) and
[aggregate validation](reports/labeling/validation.json).
Patient-level Parquets and the DuckDB database stay local; share code, methodology
and aggregate results only.

## Existing development found during organization

The local directory initially had no Git metadata. This branch was based on the
existing GitHub `main` at `fd0c1cc` without replacing collaborator work.
The repository already contains preprocessing mapping templates, documentation,
tests and package metadata. `feature/person-2-hourly-features` contains its earlier
implementation; that branch is an ancestor of main, not unmerged new work.
On 11 November 2026, main deleted `src/mimic_preprocessing/` and the discovery
results, but retained its tests and CLI entry point. Consequently, the full existing
preprocessing test suite cannot import that missing package. This branch does not
restore or rewrite that separate implementation; run the labeling test command above.

All organization and labeling commits stay on `tanusha`.
No changes are pushed to main or other contributors' branches.
