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
- Per-sample detail page must show the imported metrics, comments, review state, rug plot, and IGV launch actions.
- Use compact spacing everywhere unless a larger layout is clearly justified by readability.

## Future-proofing

- Keep the importer/repository boundary narrow so storage can be changed later if needed.
- Keep virus-specific ingest/display adapters separate from the core review app.
- When adding new upstream fields from `virpipa`, prefer additive schema changes and update fixture-backed import tests.

