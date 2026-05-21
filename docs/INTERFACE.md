# Virtitta Interface Guide

This guide describes the browser interface. Available controls depend on authentication and role settings.

## Main Table

The main table is the primary review workspace.

Key behavior:

- `LID` is the main visible sample identifier when available.
- Leading columns stay sticky during horizontal scrolling.
- Table filters apply by run, subtype, QC state, categories, manual groups, and numeric thresholds.
- The text search field filters the currently loaded table client-side.
- Column visibility can be changed from the `Columns` panel.
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

## Clustering

When clustering is enabled, `Cluster selected` starts a background job from the selected samples. At least two samples
are required.

The cluster detail page shows job status, warnings, selected sample IDs, and generated artifacts. The metadata file
contains `ID` plus the configured main-table columns and comment count. Completed jobs expose downloads for the raw
FASTA, prepared FASTA, alignment, Newick tree, metadata, command snapshot, and log. Artifact links open inline in the
browser, and the Newick tree and metadata can also be copied to the clipboard. If browser clipboard access is blocked,
the artifact content is shown in a selected text box for manual copy.

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

The FASTA clipboard exports use imported `export_*` paths from the VirPipa QC JSON. When configured and populated,
Virtitta serves these from the local output cache.

## Notifications

Status and warning messages appear as compact header toasts. They do not move the table layout.

- notices dismiss automatically after a few seconds
- warnings stay visible longer
- the close button dismisses the current message manually

## Sample Detail

The detail page shows:

- imported summary values
- current QC state
- comments
- rug/KDE image when available
- resistance summary and mutation links
- result file links
- IGV track file links
- raw imported QC JSON

## IGV Viewing

Virtitta can expose two IGV workflows when configured:

- `IGV` launches a standalone desktop IGV instance through its local HTTP endpoint and uses configured Windows drive
  mappings.
- `webIGV` opens an embedded browser viewer served by Virtitta. This is a fallback for users who cannot access the
  same drive mappings as the desktop IGV setup.

webIGV loads files through imported VirPipa `outputs` JSON paths. It uses the indexed sample FASTA as the reference,
the main CRAM when its index is available, BED/GFF annotation tracks, and VCF tracks when an explicit VCF index output
key was imported or a matching `.csi` sidecar exists next to the imported VCF.

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
- `commenter` users can add comments but cannot delete comments
- comment deletion requires admin permission

With authentication disabled, forms may show optional author fields.

## Role-Based UI

When authentication is enabled, unavailable controls are hidden.

Roles:

- `admin`: all actions
- `reviewer`: QC, categories, groups, comments, read exports, and server-side LIMS export
- `commenter`: view, read exports, and add comments
- `viewer`: view and read exports only

Routes are still protected server-side even when controls are hidden.
