# Virtitta Interface Guide

This guide describes the browser interface. Available controls depend on authentication and role settings.

## Main Table

The main table is the primary review workspace.

Key behavior:

- `LID` is the main visible sample identifier when available.
- Leading columns stay sticky during horizontal scrolling.
- Table filters apply by run, subtype, QC state, categories, manual groups, and numeric thresholds. The displayed subtype is imported from the VirPipa BLAST top-hit genotype (`typing.main_blast_genotype`).
- The text search field filters the currently loaded table client-side.
- Column visibility can be changed from the `Columns` panel. `Classification` is shown by default when imported from
  Clarity metadata; `Department`, `Sequencing Runs`, and `Sample Submission Signing` are available as optional columns
  and on the sample detail page.
- Native browser hover tooltips show full values for truncated table cells and comments.

## Selection

Selections are sticky in the browser:

- selected samples remain selected when table filters change
- selection is cleared by `Clear`, the page `Reset` link, or a manual page reload
- `Selected only` restricts the visible table to selected samples
- the summary indicators include `Selected N`

Use this to build a collection of samples that cannot be expressed by a single table filter.

## Bulk Actions

Bulk actions operate on selected samples.

Common reviewer actions:

- mark QC as `pass`, `fail`, or `unreviewed`
- assign or clear sample category
- add or remove a manual group
- start a cluster analysis, when enabled
- write server-side LIMS export
- export browser download or clipboard-oriented data
- delete samples, for admin users only

Failing a sample requires a comment.

QC state changes, category assignment, category clearing, and sample deletion are grouped under `Manage...`.
QC and category actions ask for confirmation when more than one sample is selected. Deleting samples always asks for
confirmation and only removes records from Virtitta, not result files on disk. Category actions use the configured
sample category list directly.

Restricted sample categories are hidden from users below reviewer. When a category such as `test` is configured as
restricted, only `admin` and `reviewer` users see those samples, category filter values, and category assignment
actions.

## Clustering

When clustering is enabled, `Cluster` opens actions for the selected samples. `Cluster selected` starts a background job
and keeps duplicate FASTA tree IDs as a blocking warning. `Cluster selected (allow duplicates)` allows duplicated
normalized FASTA IDs by renaming those duplicated IDs to `<ID>-<run_name>`. At least three samples are required.

The cluster detail page shows job status, warnings, selected sample IDs, and generated artifacts. The metadata file
contains `ID` plus the configured main-table columns and comment count. Completed jobs expose downloads for the raw
FASTA, prepared FASTA, alignment, Newick tree, metadata, command snapshot, and log. Artifact links open inline in the
browser, and the Newick tree and metadata can also be copied to the clipboard. If browser clipboard access is blocked,
the artifact content is shown in a selected text box for manual copy.
If MAFFT or IQ-TREE fails, the failure text includes recent command output; command exit codes, elapsed time, and full
tool output remain available in the job's `cluster.log` artifact.

If a GrapeTree URL is configured, completed jobs show `Open GrapeTree`. The link passes a tokenized GrapeTree JSON
payload containing the generated Newick tree and metadata table to the configured standalone GrapeTree instance.

## Exports

The export menu supports:

- visible table content to clipboard
- selected export FASTA records to clipboard
- selected 15% IUPAC FASTA records to clipboard
- browser LIMS download

The default `Export LIMS` action writes server-side files under:

```text
<exports.lims_root>/<YYYY-MM-DD>/
```

Repeated exports create unique filenames instead of overwriting existing files.

The FASTA clipboard exports use canonical `main_fasta` and `iupac_fasta` outputs, falling back to legacy `export_*`
outputs for older imports. The export menu lets the operator choose LID or sample ID headers; LID is the default.
When configured and populated, Virtitta serves these from the local output cache.

## Notifications

Status and warning messages appear as compact header toasts. They do not move the table layout.

- notices dismiss automatically after a few seconds
- warnings stay visible longer
- the close button dismisses the current message manually

## Sample Detail

The detail page shows:

- imported summary values
- imported Clarity metadata values when available
- current QC state
- comments
- rug/KDE image when available
- resistance summary and mutation links
- result file links, including the main BLAST output when imported
- IGV track file links
- raw imported QC JSON

Result files expose browser `View` actions for text-oriented outputs such as FASTA, BLAST, TSV, BED, GFF, and LIMS text
files, plus `Download` actions for saving the file. CRAM files and the rug plot are download-only in this section; the
rug plot is already rendered directly on the detail page.

## IGV Viewing

Virtitta can expose two IGV workflows when configured:

- `IGV` launches a standalone desktop IGV instance through its local HTTP endpoint and uses configured Windows drive
  mappings.
- `webIGV` opens an embedded browser viewer served by Virtitta. This is a fallback for users who cannot access the
  same drive mappings as the desktop IGV setup.

webIGV loads files through imported VirPipa `outputs` JSON paths. It uses the indexed sample FASTA as the reference,
the main CRAM when its index is available, BED/GFF annotation tracks, and VCF tracks. FASTA, CRAM, and VCF indexes can
be explicit in QC JSON or inferred from standard `.fai`, `.crai`, and `.csi` sidecar filenames.

## Manual Metadata Overrides

Admins can use `Edit metadata` on the sample detail page to override narrow display/review fields:

- `LID`
- `Date`
- `CT`
- `Lib Conc`
- `Subtype`

Imported values remain stored unchanged. Overridden values are shown in italic and each change creates an automatic
comment for traceability. Re-importing a run preserves Virtitta-owned overrides.

## Comments

Comments are shown newest first.

With authentication enabled:

- new comments use the logged-in user name
- `reviewer` and `commenter` users can delete their own comments
- deleting another user's comment requires admin permission

With authentication disabled, forms may show optional author fields.

## Role-Based UI

When authentication is enabled, unavailable controls are hidden.

Roles:

- `admin`: all actions
- `reviewer`: QC, categories, groups, comments, own comment deletion, read exports, and server-side LIMS export
- `commenter`: view, groups, read exports, add comments, and delete own comments
- `viewer`: view and read exports only

Routes are still protected server-side even when controls are hidden.
