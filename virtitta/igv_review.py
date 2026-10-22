"""Select one coherent IGV reference/read/annotation bundle; never mix views."""
from virtitta.outputs import VCF_OUTPUT_KEYS


REVIEW_KEYS = {
    "main_fasta": "igv_iupac_fasta",
    "main_fasta_index": "igv_iupac_fasta_index",
    "main_cram": "igv_iupac_cram",
    "main_cram_index": "igv_iupac_cram_index",
}
for _key in ("resistance_gff", "resistance_bed", "resistance_sites_gff3",
             "selected_vadr_gff", "vadr_bed", "coverage_1x_bed"):
    REVIEW_KEYS[_key] = "igv_iupac_"+_key
for _key in sorted(VCF_OUTPUT_KEYS | {"filtered_vcf_m001"}):
    REVIEW_KEYS[_key] = "igv_iupac_"+_key
    REVIEW_KEYS[_key+"_index"] = "igv_iupac_"+_key+"_index"


def select_review(outputs, manifest=None):
    declared = manifest is not None or outputs.get("igv_review_manifest") or any(
        key.startswith("igv_iupac_") for key in outputs)
    if not declared:
        return {"keys": {}, "label": "Legacy scaffold view",
                "notice": "Legacy scaffold view: reads are not the analytical IUPAC resistance-evidence alignment."}
    if (not isinstance(manifest, dict) or manifest.get("schema_version") != "1.0"
            or manifest.get("view") != "analytical_iupac" or not outputs.get("igv_review_manifest")):
        raise ValueError("Incomplete or unsupported IUPAC IGV review bundle")
    for key in REVIEW_KEYS.values():
        if not outputs.get(key) or manifest.get("outputs", {}).get(key) != outputs[key]:
            raise ValueError(f"Incomplete IUPAC IGV review bundle: {key}")
    source = manifest.get("source_reference", {}).get("contig")
    target = manifest.get("display_reference", {}).get("contig")
    if not source or not target:
        raise ValueError("IGV review reference identifiers are missing")
    return {"keys": REVIEW_KEYS, "label": "Analytical 15% IUPAC review",
            "source_contig": source, "display_contig": target,
            "notice": "Analytical 15% IUPAC reads and resistance annotations. Scaffold-derived VCF REF alleles may differ from the displayed IUPAC base; these variants were not recalled against IUPAC."}


def review_locus(locus, manifest):
    if not locus or not manifest:
        return locus
    old = manifest.get("source_reference", {}).get("contig")
    new = manifest.get("display_reference", {}).get("contig")
    if old and new and locus.startswith(old+":"):
        return new+locus[len(old):]
    return locus
