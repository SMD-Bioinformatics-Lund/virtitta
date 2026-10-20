"""Presentation adapter for legacy and versioned VirPipa resistance records.

Do not evaluate mutations or resistance rules here; the pipeline owns calls.
"""
OUTCOME_STATUS = {
    "resistant": "resistant",
    "reduced susceptibility": "warning",
    "susceptible": "clear",
    "susceptible with scored substitutions": "scored",
    "substitution on scored position": "scored",
    "not assessed": "unassessed",
    "not licensed": "unlicensed",
    "not licensed for subtype": "unlicensed",
}


def prediction_status(prediction):
    value = str(prediction or "").strip().lower()
    if value in OUTCOME_STATUS:
        return OUTCOME_STATUS[value]
    if value == "susceptible*":
        return "scored"
    if "resistant" in value:
        return "resistant"
    if any(word in value for word in ("probable", "intermediate", "reduced")):
        return "warning"
    if "possible" in value:
        return "possible"
    return "unknown"


def drug_record(record):
    prediction = record.get("outcome") or record.get("prediction")
    return {
        "status": prediction_status(prediction),
        "prediction": prediction,
        "mutations": [str(item) for item in (record.get("mutations") or record.get("matched_rules") or []) if item],
        "warnings": [str(item) for item in record.get("warnings", []) if item],
        "low_depth_positions": record.get("low_depth_positions", []),
    }


HCV_RESISTANCE_DRUGS = [
    ("Asunaprevir", "ASV"),
    ("Boceprevir", "BOC"),
    ("Glecaprevir", "GLE"),
    ("Grazoprevir", "GZR"),
    ("Paritaprevir", "PTV"),
    ("Simeprevir", "SMV"),
    ("Telaprevir", "TVR"),
    ("Voxilaprevir", "VOX"),
    ("Daclatasvir", "DCV"),
    ("Elbasvir", "EBR"),
    ("Ledipasvir", "LDV"),
    ("Ombitasvir", "OBV"),
    ("Pibrentasvir", "PIB"),
    ("Velpatasvir", "VEL"),
    ("Dasabuvir", "DSV"),
    ("Sofosbuvir", "SOF"),
]


def resistance_status_label(status: str) -> str:
    return {
        "resistant": "Resistance detected",
        "warning": "Probable resistance detected",
        "possible": "Possible resistance detected",
        "clear": "No resistance detected",
        "missing": "No resistance data",
        "scored": "No resistance detected; substitutions at scored positions",
        "unassessed": "Not assessed",
        "unlicensed": "Not licensed",
    }.get(status, "Resistance status unknown")


def build_resistance_cells(raw_sample: dict) -> list[dict]:
    resistance = raw_sample.get("resistance", {}) if isinstance(raw_sample, dict) else {}
    analysis_present = bool(resistance.get("analysis_present"))
    lookup = {
        item.get("drug"): item
        for item in resistance.get("by_drug", [])
        if isinstance(item, dict) and item.get("drug")
    }

    cells = []
    for drug_name, short_label in HCV_RESISTANCE_DRUGS:
        record = lookup.get(drug_name)
        warnings = []
        low_depth = False
        if record:
            adapted = drug_record(record)
            status = adapted["status"]
            mutations = adapted["mutations"]
            prediction = adapted["prediction"] or resistance_status_label(status)
            warnings = adapted["warnings"]
            low_depth = bool(adapted["low_depth_positions"])
        else:
            status = "clear" if analysis_present and not resistance.get("schema_version") else "missing"
            mutations = []
            prediction = resistance_status_label(status)
        title = f"{drug_name}: {prediction}"
        if mutations:
            title = f"{title} ({', '.join(mutations)})"
        if warnings:
            title += "; " + "; ".join(warnings)
        cells.append(
            {
                "drug": drug_name,
                "short": short_label,
                "status": status,
                "prediction": prediction,
                "mutations": mutations,
                "title": title,
                "warnings": warnings,
                "low_depth": low_depth,
            }
        )

    return cells


def resistance_summary_text(raw_sample: dict) -> str:
    resistance = raw_sample.get("resistance", {}) if isinstance(raw_sample, dict) else {}
    if not resistance.get("analysis_present"):
        return "No resistance data"
    calls = []
    for cell in build_resistance_cells(raw_sample):
        if cell["status"] in {"resistant", "warning", "possible"}:
            calls.append(cell["short"])
    if calls:
        return " ".join(calls)
    if any(cell["status"] in {"unassessed", "unknown", "missing"} for cell in build_resistance_cells(raw_sample)):
        return "Resistance not fully assessed"
    if not resistance.get("has_resistance"):
        return "No resistance detected"
    count = resistance.get("mutation_count") or 0
    return f"{count} mutation{'s' if count != 1 else ''}"
