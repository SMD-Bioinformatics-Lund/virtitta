"""Read pipeline-provided typing evidence without making new assignments."""
import math
import re


STATUS_LABELS = {"assigned": "Assigned", "genotype_only": "Genotype only",
                 "ambiguous": "Ambiguous", "unassigned": "Unassigned"}


def typing_summary(typing):
    if "evidence" not in typing:
        return None
    evidence = typing["evidence"]
    if not isinstance(evidence, dict) or evidence.get("schema_version") != "1.0":
        raise ValueError("Unsupported typing evidence schema")
    source = evidence.get("input")
    if not isinstance(source, dict) or source.get("role") != "analytical_15pct_iupac":
        raise ValueError("Typing evidence must identify analytical 15% IUPAC input")
    status, subtype = evidence.get("status"), evidence.get("subtype")
    if status not in STATUS_LABELS:
        raise ValueError("Invalid typing status")
    if status in ("assigned", "genotype_only"):
        if (not isinstance(subtype, str) or not re.fullmatch(r"[1-9][0-9]*[a-z]*", subtype)
                or subtype.isdigit() != (status == "genotype_only")):
            raise ValueError("Invalid assigned subtype")
    elif subtype is not None or evidence.get("selected_reference") is not None:
        raise ValueError("Uncertain typing must not declare a selected subtype/reference")
    groups = evidence.get("groups")
    if not isinstance(groups, list):
        raise ValueError("Missing typing alternatives")
    selected = None
    for group in groups:
        if not isinstance(group, dict) or not isinstance(group.get("references"), list) or not group["references"]:
            raise ValueError("Invalid typing alternative group")
        for reference in group["references"]:
            if not isinstance(reference, dict) or not isinstance(reference.get("reference"), str):
                raise ValueError("Invalid typing reference")
            for key in ("identity", "aligned_bases", "query_coverage_pct", "bit_score", "observed_coverage_pct"):
                value = reference.get(key)
                if value is None and key == "observed_coverage_pct":
                    continue
                if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                    raise ValueError(f"Invalid typing metric: {key}")
            if group.get("subtype") == subtype and reference["reference"] == evidence.get("selected_reference"):
                selected = reference
    if status in ("assigned", "genotype_only") and selected is None:
        raise ValueError("Selected typing reference is absent from its subtype group")
    return {"evidence": evidence, "selected": selected, "status_label": STATUS_LABELS[status]}
