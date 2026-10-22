# Virtitta Plans And TODO

This file records future work discussed during development. Add new planned work here when it is not implemented in
the same change. Keep entries short enough to revisit and prune.

## Documentation Convention

- When a future feature, deployment idea, or deferred cleanup is discussed, add it here.
- Move implemented items to a completed section or remove them after the implementation is documented elsewhere.
- Prefer concrete next steps over broad intent.

## Open Plans

- Defer BLAST subtyping changes until resistance validation. The implementation plan
  is in the sibling VirPipa repository at `docs/plans/blast_subtyping.md`: summarize
  per-reference segments, group subtype alternatives and render pipeline-provided
  evidence in sample details. Do not infer new typing or resistance calls in Virtitta.

- Consider showing names or other profile information on the user account page; currently it shows username, role,
  and password change only.
- Benchmark realistic sample counts before adding a distance-matrix size warning or hard limit.

## Resistance review (compatibility implemented; interactive review pending)

- Implemented shared sample/tree/GrapeTree resistance summaries: incomplete assessment
  is not a clear negative, and scored substitutions alone are not resistance.
- Next: run the representative-data checklist in VirPipa's `docs/resistance_validation.md`
  before collection-wide reprocessing. BLAST ranking and display remain deferred.
- Future typing: add validated subtype-1a clade I/II classification, explicit unassigned
  results and method/reference provenance; show clade below subtype, not as resistance.
- Implemented IUPAC-targeted desktop/webIGV review bundle selection, scaffold-derived
  variant labels and matching resistance navigation; verify actual rendering on Hopper
  archives before further collection-wide processing.

- Implemented the three-FASTA contract: analytical majority exports/matrix inputs, separate mapping-scaffold IGV reference, JSON provenance downloads, and low-depth resistance warning presentation.
- Future: curator resistance overrides must preserve the automated outcome and record reviewer, reason and date.
- Future: consider coverage-BED deprecation only after archive regeneration and removal of scaffold/historical distance dependencies. Keep BED support now.

Coordinate against VirPipa's `docs/resistance_pipeline.md` and `docs/resistance_reference_audit.md`.
Implement and document the pipeline output contract first; do not reconstruct resistance calls in Virtitta.

- Implemented compatibility: explicit per-drug outcomes and legacy predictions, mutation tooltips,
  coding-integrity findings, compound-rule coverage warnings, JSON/alignment download, and all-site IGV tracks.
  Regional subtype classification is still a pipeline dependency, not inferred in Virtitta.
- Refine scored-substitution baseline parity with online susceptible* reporting after the pipeline audit;
  current distinct outcome display does not establish complete online parity.
- Add sample-detail nucleotide/codon/amino-acid alignments for NS3, NS5A and NS5B, linked to the selected subtype
  reference and H77 numbering. Highlight resistance sites and sequence-integrity problems. Depends on pipeline
  alignment artifacts and coordinate/provenance metadata; Virtitta should render, not independently realign.
- Display mixed-population and linkage limitations separately from coding-integrity warnings. Compound-rule
  matches from an IUPAC consensus must not imply that mutations are proven to coexist in the same strain.
- Extend the implemented combined desktop/webIGV session with richer gene-wide codon/amino-acid navigation
  once corresponding pipeline annotations are available; all-site and integrity features already load.
- Add exploratory frequency cutoffs using pipeline-provided codon evidence and counting parameters. Keep the
  authoritative 15% IUPAC result distinct; do not infer linkage or validated clinical significance from sliders.
- Validate the implemented provenance display and review-preserving re-import on representative Hopper
  archives before collection-wide refresh; local schema-3 re-import regression is covered.
- Explore Orthanq for mixed-population interpretation after pipeline correctness validation; no automatic
  strain separation or assembly repair is part of current resistance calling.
