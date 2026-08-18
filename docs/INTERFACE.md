# Virtitta Interface Guide

This guide describes the browser interface. Available controls depend on authentication and role settings.
The upper-left subtitle includes the running Virtitta image version, for example `v0.9.0-1-gccd14b3`.

## In-App Help

The **Help** link beside **Samples** opens a compact browser manual covering the configured table columns, filters,
selection and action controls, sample details, and enabled analysis tools. When logged in, the page includes only
functions available to the current role. Anonymous visitors see basic viewer-level help; deployments with
authentication disabled show all available functions.

The clustering section explains the MAFFT and IQ-TREE maximum-likelihood workflow. Distance matrices are documented
separately because they use MAFFT and positional read coverage but do not run IQ-TREE or GrapeTree.

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
- When authentication is enabled, the selector beside `Columns` applies the configured default or a column preset saved
  to the current user account. Save and delete controls are inside the `Columns` panel. Changing an individual column
  marks the current combination as `Custom`; it does not modify a saved preset until the user explicitly saves and
  confirms any overwrite.
- The most recent column combination remains browser-local and is restored independently of saved account presets.
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
When configured and populated, Virtitta serves these from the local output cache after a cheap source freshness check.
Changed sources refresh the cache automatically. If result storage is temporarily unavailable, existing cached copies
remain downloadable; clipboard exports show a warning that the content is an unverified cached snapshot.

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
## Pairwise distance matrices

When clustering is enabled, select at least two samples and use **Distance...** to create pairwise matrices. This is separate from clustering: it runs MAFFT once to establish a shared gap pattern and does not run IQ-TREE or GrapeTree. Both the 15% IUPAC and majority-consensus FASTAs are required.

The result page starts with the **15% IUPAC** matrix. Switch between that and **Majority consensus**, and between compact event counts and detailed cells showing substitutions, indel events, and the pair-specific number of compared bases. "Bases compared" is the pair-specific intersection of positions with read coverage ≥1× and valid IUPAC bases in both samples. Internal indel blocks are counted only when the opposite bases and both flanks are coverage-supported and valid; terminal gaps are ignored. Both matrices share one heatmap scale; zero and diagonal cells remain neutral. These values are coverage-supported event-like minimum difference counts, not phylogenetic or transmission distances.

The **Order** control keeps the selected/main-table order by default or groups each FASTA mode independently with
UPGMA average linkage over total event counts. This ordering is only a visual evaluation aid, not a phylogenetic
analysis. Pairs with no bases compared are shown as `-1` on grey cells and as “No bases compared” in detailed mode;
UPGMA gives those pairs a temporary penalty one above the largest available event count so missing evidence does not
look like identity.

FASTA case is ignored and unknown bases are excluded from comparisons. Overlapping IUPAC alleles match, internal same-direction gap blocks count as one indel event when coverage and flank requirements are met, and terminal gaps are ignored. Duplicate visible identifiers are blocked unless the explicit allow-duplicates action is used, which adds run-name suffixes.
