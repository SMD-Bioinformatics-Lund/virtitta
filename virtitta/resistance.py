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
