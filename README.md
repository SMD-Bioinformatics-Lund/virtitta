# Virtitta

Virtitta is a compact internal web interface for reviewing `virpipa` results.

<img src="https://upload.wikimedia.org/wikipedia/commons/thumb/9/94/Birdy.svg/250px-Birdy.svg.png" alt="Virtitta oculobservis">\
_**Virtitta oculobservis**_

It imports per-sample QC summaries from completed `virpipa` runs into a local SQLite database and provides:

- a dense main table for all imported samples
- sorting, filtering, and configurable visible columns
- manual QC assignment (`pass`, `fail`, `unreviewed`)
- comments on individual samples
- bulk QC actions across selected samples
- IGV launch links for local desktop IGV
- LIMS export based on the imported `2limsrs` files plus Virtitta QC state

The interface is designed for efficient day-to-day review rather than presentation-heavy dashboards.

## Data Source

Virtitta does not scrape raw pipeline outputs directly. It imports the structured run summaries produced by `virpipa`:

- `results/<run_name>/<sample_id>/results/<sample_id>_qc_summary.json`

Each per-sample JSON includes the QC values shown in the table plus relative paths to important result files.

## Configuration

Configuration lives in `virtitta.toml`. Start from `virtitta.example.toml`.

Important settings:

- `database.path`
  - where the SQLite database is stored
- `exports.lims_root`
  - optional server-side export root for LIMS files
- `results_roots`
  - one or more result roots that contain imported `virpipa` runs
- `igv.base_url`
  - usually `http://localhost:60151/load`
- `results_roots[].windows_path`
  - Windows-visible root path used when constructing IGV URLs
- `ui.visible_columns`
  - default columns shown in the main table
- `ui.highlight_rules`
  - numeric threshold coloring rules

Example:

```toml
[database]
path = "data/virtitta.sqlite3"

[exports]
lims_root = "data/lims_exports"

[igv]
enabled = true
base_url = "http://localhost:60151/load"

[[results_roots]]
name = "default"
linux_path = "/fs1/jonas/hcv/test_results"
windows_path = "Q:/jonas/hcv/test_results"
```

## Initialize

Create or activate the environment first:

```bash
source ~/miniforge3/etc/profile.d/conda.sh
conda activate virtitta
cd ~/git/virtitta
```

Initialize the SQLite database:

```bash
PYTHONPATH=$PWD python -m virtitta.cli init-db --config virtitta.toml
```

This creates the database file configured under `database.path`.

## Start The Interface

Run the development server:

```bash
PYTHONPATH=$PWD python -m virtitta.cli serve --config virtitta.toml
```

Then open the shown URL in a browser, normally:

```text
http://127.0.0.1:8000
```

You can override host and port if needed:

```bash
PYTHONPATH=$PWD python -m virtitta.cli serve --config virtitta.toml --host 0.0.0.0 --port 8001
```

## Add Samples From A New Run

After a `virpipa` run finishes and contains per-sample `*_qc_summary.json` files under each sample `results/` directory, import it into Virtitta:

```bash
PYTHONPATH=$PWD python -m virtitta.cli import-run \
  --config virtitta.toml \
  --run-dir /fs1/jonas/hcv/test_results/260317_A00681_1225_AHJMKLDRX7
```

This imports or updates all samples from that run in the database.

Re-importing the same run is safe:

- sample QC metrics and file references are updated from the new `virpipa` JSON
- Virtitta-owned review state such as comments and QC decisions is preserved

If you want to import every run under the configured roots:

```bash
PYTHONPATH=$PWD python -m virtitta.cli import-root --config virtitta.toml
```

## Basic Workflow

Typical use:

1. Run `virpipa` on a sequencing run.
2. Import the completed run into Virtitta with `import-run`.
3. Open Virtitta in the browser.
4. Review samples in the main table.
5. Assign QC status and comments.
6. Open individual samples for more detail, rug plot, file links, and IGV launch.
7. Export selected reviewed samples to the LIMS format.

## Interface Overview

### Main Table

The main table is the primary work area. It includes:

- `LID` as the presentation-first identifier
- `Sample ID`
- subtype and BLAST identity
- read and host/human filtering metrics
- QC coverage and depth metrics
- sample metadata such as CT and library values
- `Run`
- comment summary
- row actions such as `IGV`, `LIMS`, and `Open`

Features:

- sortable columns
- filtering on run, subtype, QC state, and selected numeric thresholds
- sticky leading columns during horizontal scroll
- client-side show/hide for columns
- bulk selection and bulk QC actions

### Sample Detail

Each sample detail page shows:

- imported summary values
- current QC status
- comments
- rug KDE plot
- result-file download links
- IGV track-file links
- LIMS export for that sample

### QC And Comments

- samples can be marked `pass`, `fail`, or `unreviewed`
- failing a sample requires a comment
- QC comments can be added during the QC action itself
- comments can be deleted

### LIMS Export

Virtitta can export:

- one sample
- multiple selected samples into one file

Export is blocked if any selected sample is still `unreviewed`.

Save location behavior:

- the default `Export LIMS` action writes the export on the server under:
  - `<lims_root>/<YYYY-MM-DD>/`
- repeated exports on the same day keep unique filenames instead of overwriting earlier ones
- browser download remains available as an explicit alternative from the export dropdown
- if `exports.lims_root` is not configured, the default export action reports that as a warning instead of silently failing
- the main table export dropdown also supports clipboard export of:
  - the currently visible main table
  - selected export FASTA records
  - selected 15% IUPAC FASTA records

## Next Work

Planned next larger feature:

- clustering of selected isolates from within Virtitta
  - run trimming
  - align with MAFFT
  - infer a tree with IQ-TREE 2
  - render or serve the resulting tree in the interface
  - keep this config-driven so tool paths, output locations, and enabled clustering behavior can be adjusted without code changes

## Notes

- sample deletion removes the sample from the Virtitta database only
- deleting a sample does not remove any result files from disk
- IGV launch assumes local desktop IGV is already running and listening on the configured port
- Virtitta currently targets single-user internal use
