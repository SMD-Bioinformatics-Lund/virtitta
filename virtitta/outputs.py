from __future__ import annotations

from pathlib import Path


VCF_OUTPUT_KEYS = {
    "filtered_vcf_m005",
    "filtered_vcf_m01",
    "filtered_vcf_m015",
    "filtered_vcf_m02",
    "filtered_vcf_m03",
    "filtered_vcf_m04",
}

OUTPUT_FALLBACKS = {
    "export_fasta": ("main_fasta", "export_fasta", "lid_fasta"),
    "export_iupac_fasta": ("iupac_fasta", "export_iupac_fasta", "lid_iupac_fasta"),
}


def effective_output_key(output_key: str, outputs: dict) -> str | None:
    for candidate in OUTPUT_FALLBACKS.get(output_key, (output_key,)):
        if outputs.get(candidate):
            return candidate
    return None


def effective_output_relname(output_key: str, outputs: dict) -> str | None:
    key = effective_output_key(output_key, outputs)
    if key is None:
        return inferred_index_relname(outputs, output_key)
    return outputs.get(key)


def inferred_index_relname(outputs: dict, output_key: str) -> str | None:
    if output_key == "main_fasta_index" and outputs.get("main_fasta"):
        return f"{outputs['main_fasta']}.fai"
    if output_key == "main_cram_index" and outputs.get("main_cram"):
        return f"{outputs['main_cram']}.crai"
    if output_key.endswith("_index"):
        base_output_key = output_key.removesuffix("_index")
        if base_output_key in VCF_OUTPUT_KEYS and outputs.get(base_output_key):
            return f"{outputs[base_output_key]}.csi"
    return None


def required_sidecars(outputs: dict) -> list[tuple[str, str]]:
    sidecars: list[tuple[str, str]] = []
    if outputs.get("main_fasta"):
        sidecars.append(("main_fasta_index", outputs.get("main_fasta_index") or f"{outputs['main_fasta']}.fai"))
    if outputs.get("main_cram"):
        sidecars.append(("main_cram_index", outputs.get("main_cram_index") or f"{outputs['main_cram']}.crai"))
    for output_key in sorted(VCF_OUTPUT_KEYS):
        if outputs.get(output_key):
            index_key = f"{output_key}_index"
            sidecars.append((index_key, outputs.get(index_key) or f"{outputs[output_key]}.csi"))
    return sidecars


def safe_relative_path(base_dir: Path, relname: str) -> Path:
    relpath = Path(relname)
    if relpath.is_absolute() or ".." in relpath.parts:
        raise ValueError("Unsafe file path")
    return base_dir / relpath
