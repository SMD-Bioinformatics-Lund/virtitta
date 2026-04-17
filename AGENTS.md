# AGENTS.md - Virtitta Development Guidelines

## Overview

Virtitta is an internal analysis and review interface for VirPipa results. It ingests the machine-readable
`*_qc_summary.json` outputs produced by `virpipa`, stores normalized sample records plus app-owned review
state in a relational database, and exposes a compact web UI for triage, comments, QC decisions, and IGV launch.

**Language:** Python  
**Web stack:** FastAPI + Jinja templates  
**Database:** SQLite first, via a thin repository layer  
**Config:** TOML file, required for paths, roots, IGV settings, and UI behavior

## Core principles

- Treat `virpipa` QC JSON as the primary ingest contract.
- Do not rescrape pipeline output trees when the data already exists in the JSON.
- Keep review state app-owned: comments and QC pass/fail are stored in Virtitta, not written back into `virpipa`.
- Keep the UI dense and operational. Avoid large padding, decorative whitespace, and fashion-driven layouts.
- Keep virus-specific logic isolated so HCV is first, not hard-coded forever.

## Environment

- Start with a conda-based local workflow.
- Plan for later deployment in Docker on a different Linux distribution.
- Prefer stdlib or small, explicit dependencies unless a library materially improves maintainability.
- The current upstream data comes from `virpipa` runs on Hopper-derived storage layouts, but Virtitta should only rely on configured roots and path mappings.

## Expected config-first workflow

Keep runtime behavior configurable in `virtitta.toml`:

- database path
- server-side export roots
- results roots
- Linux-to-Windows path mappings for IGV
- enabled features
- visible table columns
- highlight rules
- app host/port/title

Do not hard-code environment-specific paths in application logic.

## Suggested commands

```bash
# Local development server
python -m virtitta.cli serve --config virtitta.toml

# Initialize the SQLite database
python -m virtitta.cli init-db --config virtitta.toml

# Import one completed virpipa run
python -m virtitta.cli import-run --config virtitta.toml --run-dir /path/to/results/<run_name>

# Import every run under configured results roots
python -m virtitta.cli import-root --config virtitta.toml

# Cheap syntax smoke test
python -m compileall virtitta
```

## Data model expectations

- Use `sample_run_id = <sample_id>_<run_name>` as the application identity for a sequenced sample record.
- Keep `run_name`, `sample_id`, and `lid` queryable as separate columns.
- Preserve the imported raw QC JSON in the database for traceability and future schema evolution.
- Flatten the main operational fields into indexed/queryable columns for filtering and sorting.

## UI expectations

- Main page is a dense table of samples.
- Sorting and filtering must be easy and fast.
- Bulk QC assignment must be supported.
- Per-sample detail page must show the imported metrics, comments, review state, rug plot, resistance summary, and IGV actions.
- Use compact spacing everywhere unless a larger layout is clearly justified by readability.
- Prefer presentation-first identifiers in the UI:
  - `LID` should be the primary visible identifier where available.
  - `sample_id` remains the technical identity used for `sample_run_id`, IGV track loading, and file naming unless upstream changes make that unnecessary.

## Current behavior to preserve

- Import from per-sample `virpipa` `results/<run>/<sample>/results/<sample>_qc_summary.json`.
- Preserve Virtitta-owned review state across re-import.
- Main table currently supports:
  - dense filtering and sorting
  - sticky first columns
  - column toggles
  - bulk QC updates
  - comments with hover preview
  - IGV launch
  - LIMS export
  - compact resistance strip
- The resistance strip is intentionally fixed to the full geno2pheno HCV drug set and should not vary in length between samples.
- Native browser `title` tooltips are currently used in the main table; if tooltip timing/formatting changes are needed later, switch deliberately to a custom tooltip implementation rather than partially mixing both.

## Web UI constraints

- Browser downloads can suggest filenames but cannot force a client-side save path.
- If a feature needs files written automatically into a designated directory, be explicit whether that means:
  - client-side download behavior, which is browser-controlled, or
  - server-side export behavior, which can be implemented with a configured server path.
- Current LIMS behavior supports server-side writing when `exports.lims_root` is configured:
  - the default export action writes to the server-side path
  - write under `<lims_root>/<YYYY-MM-DD>/`
  - avoid overwriting existing files by creating a unique filename
  - browser download is a separate explicit alternative, not the default action
  - show user feedback after export so the operator can see that the write completed
- The main table export dropdown also supports clipboard-oriented exports:
  - visible table content
  - selected export FASTA records
  - selected 15% IUPAC FASTA records
  - prefer using the configured `export_*` output paths from imported QC JSON rather than rebuilding FASTA content in the app

## Planned next larger feature

- Add isolate clustering from selected samples.
- Expected first implementation:
  - select samples in the main table
  - derive/export the appropriate FASTA inputs
  - run trimming if needed
  - align with MAFFT
  - infer phylogeny with IQ-TREE 2
  - store outputs in a predictable configured location
  - expose tree viewing/downloading in the UI
- Keep clustering configuration-driven:
  - tool paths or container commands
  - working/output directories
  - enabled/disabled feature flags
  - virus-specific defaults if HCV-specific assumptions are introduced

## Future-proofing

- Keep the importer/repository boundary narrow so storage can be changed later if needed.
- Keep virus-specific ingest/display adapters separate from the core review app.
- When adding new upstream fields from `virpipa`, prefer additive schema changes and update fixture-backed import tests.
