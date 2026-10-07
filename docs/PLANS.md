# Virtitta Plans And TODO

This file records future work discussed during development. Add new planned work here when it is not implemented in
the same change. Keep entries short enough to revisit and prune.

## Documentation Convention

- When a future feature, deployment idea, or deferred cleanup is discussed, add it here.
- Move implemented items to a completed section or remove them after the implementation is documented elsewhere.
- Prefer concrete next steps over broad intent.

## Open Plans

- Consider showing names or other profile information on the user account page; currently it shows username, role,
  and password change only.
- Benchmark realistic sample counts before adding a distance-matrix size warning or hard limit.

## Resistance review (pending VirPipa outputs)

Coordinate against VirPipa's `docs/resistance_pipeline.md` and `docs/resistance_reference_audit.md`.
Implement and document the pipeline output contract first; do not reconstruct resistance calls in Virtitta.

- Import the versioned resistance JSON through the QC JSON output contract, including all drugs, reference/rule
  provenance, per-gene subtype and assessment status. Preserve compatibility with historical archives.
- Show susceptible* / substitutions at scored positions separately from resistant and reduced-susceptibility
  results. Include per-drug scored substitutions; regional mutation summaries alone are insufficient.
- Surface coding-sequence integrity warnings per gene and affected resistance site: non-triplet indels, definite
  versus possible IUPAC stop codons, uncertain/truncated boundaries, and unreliable codon mapping. Do not label
  unassessable sites susceptible or imply that a disruption is necessarily a pipeline-introduced error.
- Add sample-detail nucleotide/codon/amino-acid alignments for NS3, NS5A and NS5B, linked to the selected subtype
  reference and H77 numbering. Highlight resistance sites and sequence-integrity problems. Depends on pipeline
  alignment artifacts and coordinate/provenance metadata; Virtitta should render, not independently realign.
- Display mixed-population and linkage limitations separately from coding-integrity warnings. Compound-rule
  matches from an IUPAC consensus must not imply that mutations are proven to coexist in the same strain.
- Combine gene annotations, all scored resistance positions, called substitutions and coding-integrity warnings
  in a coherent IGV session for the matching FASTA/CRAM coordinate system. Use concise labels and informative
  popups with gene/H77 codon numbers, amino acids, nucleotide changes and compound-rule context; avoid `site:`
  label prefixes. Apply consistently to desktop IGV and webIGV.
- Add exploratory frequency cutoffs using pipeline-provided codon evidence and counting parameters. Keep the
  authoritative 15% IUPAC result distinct; do not infer linkage or validated clinical significance from sliders.
- Support refreshing recalled resistance results without losing comments or QC review state. Make old versus
  refreshed provenance visible and test re-import of historical archives after pipeline recall/full reruns.
