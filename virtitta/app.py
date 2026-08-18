from __future__ import annotations

import hashlib
import json
import secrets
from contextlib import asynccontextmanager
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path, PureWindowsPath
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from virtitta.artifact_cache import CACHE_OFFLINE_UNVERIFIED, resolve_cached_output
from virtitta.auth import (
    PERMISSION_CATEGORY_UPDATE,
    PERMISSION_COMMENT_ADD,
    PERMISSION_COMMENT_DELETE,
    PERMISSION_EXPORT_LIMS,
    PERMISSION_EXPORT_READ,
    PERMISSION_GROUP_UPDATE,
    PERMISSION_METADATA_OVERRIDE,
    PERMISSION_QC_UPDATE,
    PERMISSION_RUN_REFRESH,
    PERMISSION_SAMPLE_DELETE,
    PERMISSION_VIEW,
    ROLE_ADMIN,
    ROLE_COMMENTER,
    ROLE_REVIEWER,
    ROLE_VIEWER,
    authenticate_local_user,
    create_login_session,
    disabled_auth_user,
    get_user_from_cookie,
    logout_session,
)
from virtitta.cluster import (
    ARTIFACT_GRAPETREE_JSON,
    CLUSTER_ARTIFACT_ALIASES,
    MIN_CLUSTER_SAMPLES,
    PUBLIC_CLUSTER_ARTIFACTS,
    ClusterError,
    artifact_path,
    command_config_snapshot,
    cluster_artifacts,
    ensure_grapetree_json,
    prepare_cluster_files,
    run_cluster_job,
    write_command_snapshot,
)
from virtitta.config import DEFAULT_COLUMN_LABELS, QC_STATUS_OPTIONS, Config, load_config
from virtitta.distance import (
    ANALYSIS_TYPE as DISTANCE_ANALYSIS_TYPE,
    ARTIFACT_RESULT as DISTANCE_RESULT_ARTIFACT,
    MIN_DISTANCE_SAMPLES,
    distance_artifact_path,
    distance_artifacts,
    distance_config_snapshot,
    ensure_result_ordering,
    prepare_distance_files,
    run_distance_job,
)
from virtitta.outputs import (
    effective_output_key,
    effective_output_relname,
    inferred_index_relname,
    safe_relative_path,
)
from virtitta.repository import (
    add_comment,
    add_samples_to_group,
    connect,
    create_cluster_job,
    delete_comment,
    delete_samples,
    delete_user_column_preset,
    get_cluster_job,
    get_cluster_job_by_public_token,
    get_cluster_job_samples,
    get_comments,
    get_run,
    get_sample,
    init_db,
    list_manual_groups,
    list_runs,
    list_samples,
    list_stored_sample_categories,
    list_subtypes,
    list_user_column_presets,
    mark_stale_cluster_jobs_failed,
    raw_json_for_sample,
    remove_samples_from_group,
    save_user_column_preset,
    set_sample_category,
    set_sample_field_overrides,
    update_qc_status,
    utc_now,
)


TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

DETAIL_FILE_LINKS = [
    ("Main FASTA", "main_fasta"),
    ("Main BLAST", "main_blast"),
    ("Main CRAM", "main_cram"),
    ("0.15 IUPAC FASTA", "iupac_fasta"),
    ("0.15 IUPAC CRAM", "iupac_cram"),
    ("Coverage TSV", "coverage_tsv"),
    ("Resistance TSV", "resistance_tsv"),
    ("Resistance GFF", "resistance_gff"),
    ("VADR BED", "vadr_bed"),
    ("Selected VADR GFF", "selected_vadr_gff"),
    ("Display Rug Plot", "display_rug_kde_plot"),
]

VIEWABLE_DETAIL_OUTPUT_KEYS = {
    "main_fasta",
    "main_blast",
    "iupac_fasta",
    "coverage_tsv",
    "resistance_tsv",
    "resistance_gff",
    "vadr_bed",
    "selected_vadr_gff",
}
DETAIL_VIEW_MEDIA_TYPE = "text/plain; charset=utf-8"

IGV_TRACK_LINKS = [
    ("Genome FASTA", "main_fasta"),
    ("Main CRAM", "main_cram"),
    ("VCF m0.05", "filtered_vcf_m005"),
    ("VCF m0.1", "filtered_vcf_m01"),
    ("VCF m0.15", "filtered_vcf_m015"),
    ("VCF m0.2", "filtered_vcf_m02"),
    ("VCF m0.3", "filtered_vcf_m03"),
    ("VCF m0.4", "filtered_vcf_m04"),
    ("VADR BED", "vadr_bed"),
    ("Resistance GFF", "resistance_gff"),
    ("Selected VADR GFF", "selected_vadr_gff"),
]

WEBIGV_ANNOTATION_TRACKS = [
    ("VADR BED", "vadr_bed", "bed"),
    ("Resistance GFF", "resistance_gff", "gff3"),
    ("Selected VADR GFF", "selected_vadr_gff", "gff3"),
]
WEBIGV_VCF_TRACKS = [
    ("VCF m0.05", "filtered_vcf_m005"),
    ("VCF m0.1", "filtered_vcf_m01"),
    ("VCF m0.15", "filtered_vcf_m015"),
    ("VCF m0.2", "filtered_vcf_m02"),
    ("VCF m0.3", "filtered_vcf_m03"),
    ("VCF m0.4", "filtered_vcf_m04"),
]
WEBIGV_ALLOWED_OUTPUT_KEYS = {
    "main_fasta",
    "main_fasta_index",
    "main_cram",
    "main_cram_index",
    "vadr_bed",
    "resistance_gff",
    "selected_vadr_gff",
}
for _label, _key in WEBIGV_VCF_TRACKS:
    WEBIGV_ALLOWED_OUTPUT_KEYS.add(_key)
    WEBIGV_ALLOWED_OUTPUT_KEYS.add(f"{_key}_index")

LIMS_EXPORT_HEADER = "sample_id\tparameter_name\tparameter_value\tcomment"
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

CATEGORY_UNASSIGNED = "__unassigned__"
SAMPLE_OVERRIDE_LABELS = {
    "lid": "LID",
    "sequencing_date": "Date",
    "sample_metadata_ct": "CT",
    "sample_metadata_library_concentration_ng_ul": "Lib Conc",
    "typing_report_subtype": "Subtype",
}

HELP_COLUMN_DESCRIPTIONS = {
    "lid": "Primary laboratory identifier shown for the sample when available.",
    "sample_id": "Technical sample identifier used by VirPipa and in result filenames.",
    "sequencing_date": "Sequencing date derived from the run name, with the imported date as fallback.",
    "generated_date": "Date recorded when the VirPipa QC summary was generated.",
    "sample_category": "Review category assigned in Virtitta, such as production or test.",
    "sample_metadata_classification": "Classification imported from the sample metadata source.",
    "qc_status": "Virtitta review decision: unreviewed, pass, or fail.",
    "manual_groups": "User-defined groups containing the sample.",
    "typing_report_subtype": "HCV subtype reported from the main VirPipa BLAST result.",
    "typing_main_blast_identity": "Percentage identity of the main BLAST typing match.",
    "resistance_summary": "Compact geno2pheno HCV drug-resistance calls; hover for detected mutations.",
    "host_filter_reads_in": "Number of reads entering host/human read filtering.",
    "host_filter_reads_removed_proportion": "Percentage of input reads removed by host/human filtering.",
    "qc_coverage_pct": "Overall consensus coverage percentage reported by VirPipa.",
    "qc_mean_depth": "Mean read depth across the consensus sequence.",
    "qc_coverage_1x_pct": "Percentage of consensus positions covered by at least 1 read.",
    "qc_coverage_10x_pct": "Percentage of consensus positions covered by at least 10 reads.",
    "qc_coverage_100x_pct": "Percentage of consensus positions covered by at least 100 reads.",
    "qc_coverage_1000x_pct": "Percentage of consensus positions covered by at least 1,000 reads.",
    "variant_af_count_005": "Number of variant calls at the 0.05 allele-frequency threshold.",
    "variant_af_count_01": "Number of variant calls at the 0.1 allele-frequency threshold.",
    "variant_af_count_015": "Number of variant calls at the 0.15 allele-frequency threshold.",
    "variant_af_count_02": "Number of variant calls at the 0.2 allele-frequency threshold.",
    "variant_af_count_03": "Number of variant calls at the 0.3 allele-frequency threshold.",
    "variant_af_count_04": "Number of variant calls at the 0.4 allele-frequency threshold.",
    "sample_metadata_ct": "Diagnostic cycle-threshold value imported from sample metadata.",
    "sample_metadata_library_concentration_ng_ul": "Library concentration in ng/µl from sample metadata.",
    "sample_metadata_library_fragment_length_bp": "Library fragment length in base pairs from sample metadata.",
    "sample_metadata_department": "Submitting department imported from sample metadata.",
    "sample_metadata_sequencing_runs": "Sequencing-run information imported from sample metadata.",
    "sample_metadata_sample_submission_signing": "Sample-submission signing information imported from metadata.",
    "run_name": "VirPipa run from which the sample was imported.",
    "comment_count": "Number of comments; hover for a preview or open the sample to read them.",
    "actions": "Available shortcuts for opening the sample, IGV, webIGV, or LIMS export.",
}


def format_value(value: object, column: str | None = None) -> str:
    if value is None:
        return ""
    if column == "host_filter_reads_in":
        try:
            return f"{int(value):,}".replace(",", " ")
        except (TypeError, ValueError):
            return str(value)
    if column == "host_filter_reads_removed_proportion":
        return f"{float(value) * 100:.1f}%"
    if column == "typing_main_blast_identity":
        return f"{float(value):.1f}"
    if column == "qc_coverage_pct":
        return f"{float(value):.2f}"
    if column == "qc_mean_depth":
        return f"{float(value):.0f}"
    if isinstance(value, float):
        return f"{value:.4f}".rstrip("0").rstrip(".")
    return str(value)


def display_identifier(row: dict) -> str:
    return row.get("lid") or row.get("sample_id") or ""


def parse_optional_float(value: str | None) -> float | None:
    if value is None:
        return None
    if not isinstance(value, str):
        value = str(value)
    stripped = value.strip()
    if not stripped:
        return None
    return float(stripped)


def parse_optional_date(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    if not stripped:
        return None
    datetime.strptime(stripped, "%Y-%m-%d")
    return stripped


def override_comment_text(change: dict) -> str:
    field_name = change["field_name"]
    label = SAMPLE_OVERRIDE_LABELS.get(field_name, field_name)
    old_value = format_value(change.get("old_value"), field_name) or "blank"
    new_value = format_value(change.get("new_value"), field_name) or "blank"
    if change.get("cleared"):
        return f"Manual override cleared: {label} now uses imported value {new_value}."
    return f"Manual override: {label} changed from {old_value} to {new_value}."


def unique_strings(values: object) -> list[str]:
    if not isinstance(values, (list, tuple, set)):
        return []
    normalized: list[str] = []
    seen: set[str] = set()
    for value in values:
        item = str(value).strip()
        if not item or item in seen:
            continue
        seen.add(item)
        normalized.append(item)
    return normalized


def table_columns(config: Config) -> list[str]:
    return list(config.ui.table_columns)


def is_public_request_path(path: str) -> bool:
    return path in {"/help", "/login"} or path.startswith("/static/") or path.startswith("/clusters/public/")


def request_path_without_root_path(path: str, root_path: str) -> str:
    if root_path and (path == root_path or path.startswith(f"{root_path}/")):
        return path[len(root_path) :] or "/"
    return path


def column_visibility_storage_key(config: Config) -> str:
    payload = {
        "table_columns": config.ui.table_columns,
        "visible_columns": config.ui.visible_columns,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    digest = hashlib.sha1(encoded).hexdigest()[:12]
    return f"virtitta.columnVisibility.v2.{digest}"


def available_preset_columns(config: Config) -> list[str]:
    return [*config.ui.table_columns, "comment_count", "actions"]


def configured_category_options(config: Config, stored_categories: list[str], selected_categories: list[str]) -> list[str]:
    options = list(config.annotations.sample_categories)
    for category in list(stored_categories) + list(selected_categories):
        if category == CATEGORY_UNASSIGNED or category in options:
            continue
        options.append(category)
    return options


def restricted_sample_categories(config: Config, request: Request) -> list[str]:
    if not config.auth.enabled:
        return []
    current_user = getattr(request.state, "current_user", None)
    if current_user is not None and current_user.can(PERMISSION_CATEGORY_UPDATE):
        return []
    return list(config.annotations.restricted_sample_categories)


def visible_category_options(config: Config, request: Request, categories: list[str]) -> list[str]:
    restricted = set(restricted_sample_categories(config, request))
    if not restricted:
        return categories
    return [category for category in categories if category not in restricted]


def sample_visible_to_request(config: Config, request: Request, sample_row: dict) -> bool:
    category = sample_row.get("sample_category")
    return not category or category not in restricted_sample_categories(config, request)


def require_visible_sample(config: Config, request: Request, sample_row: dict | None) -> dict:
    if sample_row is None or not sample_visible_to_request(config, request, sample_row):
        raise HTTPException(status_code=404, detail="Sample not found")
    return sample_row


def bool_query_value(value: bool) -> str:
    return "true" if value else "false"


def replace_query_params(url: str, **updates: object) -> str:
    parts = urlsplit(url)
    query_items = parse_qsl(parts.query, keep_blank_values=True)
    replaced_keys = set(updates)
    query_items = [(key, value) for key, value in query_items if key not in replaced_keys]

    for key, value in updates.items():
        if isinstance(value, (list, tuple)):
            for item in value:
                item_text = str(item).strip()
                if item_text:
                    query_items.append((key, item_text))
            continue
        if value is None:
            continue
        value_text = str(value).strip()
        if value_text:
            query_items.append((key, value_text))

    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query_items), parts.fragment))


def cell_class(config: Config, column: str, value: object) -> str:
    rules = config.ui.highlight_rules.get(column)
    if not rules or value in ("", None):
        return ""
    try:
        numeric_value = float(value)
    except (TypeError, ValueError):
        return ""

    if "danger_under" in rules and numeric_value < rules["danger_under"]:
        return "cell-danger"
    if "warn_under" in rules and numeric_value < rules["warn_under"]:
        return "cell-warn"
    if "danger_over" in rules and numeric_value > rules["danger_over"]:
        return "cell-danger"
    if "warn_over" in rules and numeric_value > rules["warn_over"]:
        return "cell-warn"
    return ""


def row_class(row: dict) -> str:
    return ""


def cell_display_class(column: str, row: dict) -> str:
    if column == "qc_status":
        qc_status = row.get("qc_status", "unreviewed")
        if qc_status == "pass":
            return "cell-qc-pass"
        if qc_status == "fail":
            return "cell-qc-fail"
    return ""


def cell_style(column: str, value: object) -> str:
    if column != "host_filter_reads_removed_proportion" or value in ("", None):
        return ""
    try:
        percent = max(0.0, min(100.0, float(value) * 100.0))
    except (TypeError, ValueError):
        return ""
    return f"--data-bar-width:{percent:.3f}%;"


def column_style(config: Config, column: str) -> str:
    max_width = config.ui.column_max_widths.get(column, "")
    if not max_width:
        return ""
    return f"--column-max-width:{max_width};"


def comment_link_label(row: dict) -> str:
    count = int(row.get("comment_count") or 0)
    if count <= 0:
        return "None"

    preview = str(row.get("comment_preview") or "").split("\n---\n", 1)[0].strip()
    if ": " in preview:
        preview = preview.split(": ", 1)[1].strip()
    if not preview:
        return str(count)

    if len(preview) > 11:
        preview = f"{preview[:11]}..."
    return f"{count} - {preview}"


def resistance_prediction_status(prediction: str | None) -> str:
    value = str(prediction or "").strip().lower()
    if not value:
        return "unknown"
    if "resistant" in value:
        return "resistant"
    if "probable" in value or "intermediate" in value or "reduced" in value:
        return "warning"
    if "possible" in value:
        return "possible"
    return "warning"


def resistance_status_label(status: str) -> str:
    return {
        "resistant": "Resistance detected",
        "warning": "Probable resistance detected",
        "possible": "Possible resistance detected",
        "clear": "No resistance detected",
        "missing": "No resistance data",
    }.get(status, "Resistance status unknown")


def resistance_sort_key(raw_sample: dict) -> tuple:
    severity_order = {
        "missing": -1,
        "clear": 0,
        "possible": 1,
        "warning": 2,
        "resistant": 3,
    }
    cells = build_resistance_cells(raw_sample)
    severities = [severity_order.get(cell["status"], -1) for cell in cells]
    mutation_count = int(raw_sample.get("resistance", {}).get("mutation_count") or 0)
    return (
        max(severities, default=-1),
        sum(severities),
        mutation_count,
        tuple(severities),
    )


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
        if record:
            status = resistance_prediction_status(record.get("prediction"))
            mutations = [item for item in record.get("mutations", []) if item]
            prediction = record.get("prediction") or resistance_status_label(status)
        else:
            status = "clear" if analysis_present else "missing"
            mutations = []
            prediction = resistance_status_label(status)
        title = f"{drug_name}: {prediction}"
        if mutations:
            title = f"{title} ({', '.join(mutations)})"
        cells.append(
            {
                "drug": drug_name,
                "short": short_label,
                "status": status,
                "prediction": prediction,
                "mutations": mutations,
                "title": title,
            }
        )

    return cells


def resistance_tooltip_text(raw_sample: dict) -> str:
    resistance = raw_sample.get("resistance", {}) if isinstance(raw_sample, dict) else {}
    if not resistance.get("analysis_present"):
        return "No resistance data"

    lines: list[str] = []
    for cell in build_resistance_cells(raw_sample):
        if cell["status"] not in {"resistant", "warning", "possible"}:
            continue
        if cell["mutations"]:
            lines.append(f"{cell['short']}: {', '.join(cell['mutations'])}")
        else:
            lines.append(f"{cell['short']}: {cell['prediction']}")

    if not lines:
        return "No resistance detected"
    return "\n".join(lines)


def resistance_summary_text(raw_sample: dict) -> str:
    resistance = raw_sample.get("resistance", {}) if isinstance(raw_sample, dict) else {}
    if not resistance.get("analysis_present"):
        return "No resistance data"
    if not resistance.get("has_resistance"):
        return "No resistance detected"
    calls = []
    for cell in build_resistance_cells(raw_sample):
        if cell["status"] in {"resistant", "warning", "possible"}:
            calls.append(cell["short"])
    if calls:
        return " ".join(calls)
    count = resistance.get("mutation_count") or 0
    return f"{count} mutation{'s' if count != 1 else ''}"


def build_resistance_mutations(raw_sample: dict, sample_id: str) -> list[dict]:
    resistance = raw_sample.get("resistance", {}) if isinstance(raw_sample, dict) else {}
    mutations = []
    for index, mutation in enumerate(resistance.get("mutations", [])):
        if not isinstance(mutation, dict):
            continue
        start = mutation.get("genomic_start")
        end = mutation.get("genomic_end")
        locus = None
        if start and end:
            locus = f"{sample_id}:{start}-{end}"
        item = dict(mutation)
        item["index"] = index
        item["drugs_text"] = ", ".join(item.get("drugs", []) or [])
        item["locus"] = locus
        mutations.append(item)
    return mutations


def safe_output_path(sample_dir: Path, relname: str) -> Path:
    try:
        return safe_relative_path(sample_dir, relname)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="Unsafe file path") from exc


def effective_outputs(config: Config, sample_row, raw_sample: dict | None = None) -> dict:
    raw = raw_sample if raw_sample is not None else raw_json_for_sample(sample_row)
    return dict(raw.get("outputs", {}))


def output_links(outputs: dict, link_specs: list[tuple[str, str]]) -> list[dict]:
    links: list[dict] = []
    for label, key in link_specs:
        relname = outputs.get(key)
        if relname:
            links.append(
                {
                    "label": label,
                    "key": key,
                    "filename": relname,
                    "can_view": key in VIEWABLE_DETAIL_OUTPUT_KEYS,
                }
            )
    return links


def resolve_sample_results_dir(config: Config, sample_row) -> Path:
    root_name = sample_row["source_root_name"]
    root = config.get_root(root_name)
    if root is None:
        raise HTTPException(status_code=500, detail=f"Configured results root not found: {root_name}")
    return (root.linux_path / sample_row["sample_results_relpath"]).resolve()


def resolve_output_file(config: Config, sample_row, output_key: str) -> tuple[Path, str]:
    outputs = effective_outputs(config, sample_row)
    relname = effective_output_relname(output_key, outputs)
    if not relname:
        raise HTTPException(status_code=404, detail=f"Output not available: {output_key}")

    sample_dir = resolve_sample_results_dir(config, sample_row)
    candidate = safe_output_path(sample_dir, relname)
    if not candidate.exists():
        raise HTTPException(status_code=404, detail=f"Missing file on disk: {candidate}")
    return candidate, relname


def inferred_webigv_index_relname(outputs: dict, output_key: str) -> str | None:
    return inferred_index_relname(outputs, output_key)


def resolve_webigv_output_file(config: Config, sample_row, output_key: str) -> tuple[Path, str]:
    outputs = effective_outputs(config, sample_row)
    relname = effective_output_relname(output_key, outputs) or inferred_webigv_index_relname(outputs, output_key)
    if not relname:
        raise HTTPException(status_code=404, detail=f"Output not available: {output_key}")

    sample_dir = resolve_sample_results_dir(config, sample_row)
    candidate = safe_output_path(sample_dir, relname)
    if not candidate.exists():
        raise HTTPException(status_code=404, detail=f"Missing file on disk: {candidate}")
    return candidate, relname


def webigv_output_exists(config: Config, sample_row, outputs: dict, output_key: str) -> bool:
    relname = effective_output_relname(output_key, outputs) or inferred_webigv_index_relname(outputs, output_key)
    if not relname:
        return False

    sample_dir = resolve_sample_results_dir(config, sample_row)
    return safe_output_path(sample_dir, relname).exists()


def windows_path_to_igv_path(path_str: str) -> str:
    path_str = path_str.replace("\\", "/")
    if len(path_str) >= 3 and path_str[1:3] == ":/":
        return f"/{path_str[:2]}{path_str[3:]}"
    return path_str


def lims_qc_value(qc_status: str | None) -> str:
    return "Passed" if qc_status == "pass" else "Failed"


def normalize_lims_row(line: str) -> str:
    parts = line.rstrip("\n\r").split("\t")
    if len(parts) < 4:
        parts.extend([""] * (4 - len(parts)))
    return "\t".join(parts[:4])


def lims_export_lid(sample_row: dict) -> str:
    return str(sample_row.get("lid") or sample_row.get("sample_id") or "")


def lims_export_hcvtyp_value(sample_row: dict) -> str:
    subtype = sample_row.get("typing_report_subtype") or ""
    if not subtype:
        return ""
    return f"HCV genotyp {subtype}"


LIMS_EXPORT_PARAMETERS = [
    ("hcvtyp", lims_export_hcvtyp_value),
    ("hcvqc", lambda sample_row: lims_qc_value(sample_row.get("qc_status"))),
]


def build_lims_export_rows(config: Config, sample_row: dict) -> list[str]:
    lid = lims_export_lid(sample_row)
    rows: list[str] = []
    for parameter_name, value_builder in LIMS_EXPORT_PARAMETERS:
        parameter_value = value_builder(sample_row)
        if not parameter_value:
            continue
        rows.append(normalize_lims_row(f"{lid}\t{parameter_name}\t{parameter_value}\t"))
    return rows


def build_lims_export_content(config: Config, sample_rows: list[dict]) -> str:
    lines = [LIMS_EXPORT_HEADER]
    for sample_row in sample_rows:
        lines.extend(build_lims_export_rows(config, sample_row))
    return "\n".join(lines) + "\n"


def load_sample_rows(config: Config, sample_run_ids: list[str], request: Request | None = None) -> list[dict]:
    connection = connect(config.database.path)
    try:
        sample_rows = []
        for item in sample_run_ids:
            sample_row = get_sample(connection, item)
            if sample_row is not None and (request is None or sample_visible_to_request(config, request, sample_row)):
                sample_rows.append(sample_row)
    finally:
        connection.close()
    return sample_rows


def cluster_job_response(job: dict) -> dict:
    return {
        "id": job["id"],
        "status": job["status"],
        "selected_count": job["selected_count"],
        "warning_text": job.get("warning_text") or "",
        "error_text": job.get("error_text") or "",
        "created_at": job.get("created_at"),
        "started_at": job.get("started_at"),
        "completed_at": job.get("completed_at"),
    }


def job_analysis_type(job: dict) -> str:
    return json.loads(job.get("config_json") or "{}").get("analysis_type", "cluster")


def build_grapetree_url(config: Config, request: Request, job: dict) -> str:
    if not config.cluster.grapetree_url:
        return ""
    token = job["public_token"]
    generated_url = str(request.url_for("cluster_public_artifact", public_token=token, artifact_key="grapetree.json"))
    if config.cluster.public_base_url:
        generated_path = urlsplit(generated_url).path
        if config.app.root_path and (
            generated_path == config.app.root_path
            or generated_path.startswith(f"{config.app.root_path}/")
        ):
            generated_path = generated_path[len(config.app.root_path) :] or "/"
        tree_url = f"{config.cluster.public_base_url}{generated_path}"
    else:
        tree_url = generated_url
    return f"{config.cluster.grapetree_url}?{urlencode({'tree': tree_url})}"


def fasta_export_header(sample_row: dict, header_id: str, output_key: str) -> str:
    if header_id == "sample_id":
        identifier = sample_row.get("sample_id") or sample_row.get("lid") or sample_row.get("sample_run_id")
    else:
        identifier = sample_row.get("lid") or sample_row.get("sample_id") or sample_row.get("sample_run_id")
    header = str(identifier or "sample")
    if output_key == "export_iupac_fasta":
        header = f"{header}-0.15-iupac"
    return header


def rewrite_fasta_headers(text: str, header: str) -> str:
    lines = text.splitlines()
    rewritten: list[str] = []
    record_count = 0
    for line in lines:
        if line.startswith(">"):
            record_count += 1
            record_header = header if record_count == 1 else f"{header}_{record_count}"
            rewritten.append(f">{record_header}")
        else:
            rewritten.append(line)
    result = "\n".join(rewritten)
    if result and not result.endswith("\n"):
        result = f"{result}\n"
    return result


def build_fasta_clipboard_content(
    config: Config,
    sample_rows: list[dict],
    output_key: str,
    header_id: str = "lid",
    artifact_sources: set[str] | None = None,
) -> str:
    chunks: list[str] = []
    connection = connect(config.database.path)
    try:
        for sample_row in sample_rows:
            source_key = effective_output_key(output_key, effective_outputs(config, sample_row)) or output_key
            resolved = resolve_cached_output(config, connection, sample_row, source_key)
            if resolved is None:
                file_path, _ = resolve_output_file(config, sample_row, output_key)
                source = "live"
            else:
                file_path = resolved.path
                source = resolved.source
            if artifact_sources is not None:
                artifact_sources.add(source)
            text = file_path.read_text(encoding="utf-8")
            chunks.append(rewrite_fasta_headers(text, fasta_export_header(sample_row, header_id, output_key)))
    finally:
        connection.close()
    return "".join(chunks)


def artifact_response_headers(sources: set[str]) -> dict[str, str]:
    value = ",".join(sorted(sources or {"live"}))
    headers = {"X-Virtitta-Artifact-Source": value}
    if CACHE_OFFLINE_UNVERIFIED in sources:
        headers["Warning"] = '110 Virtitta "Result root unavailable; serving an unverified cached copy"'
    return headers


def lims_export_filename(sample_rows: list[dict]) -> str:
    if len(sample_rows) == 1:
        identifier = display_identifier(sample_rows[0]) or sample_rows[0].get("sample_id") or "sample"
        safe_identifier = str(identifier).replace("/", "_").replace(" ", "_")
        return f"{safe_identifier}-2limsrs.txt"
    return "virtitta-2limsrs.txt"


def timestamped_lims_export_filename(sample_rows: list[dict], timestamp: str) -> str:
    filename = lims_export_filename(sample_rows)
    stem = Path(filename).stem
    suffix = Path(filename).suffix
    return f"{stem}-{timestamp}{suffix}"


def write_server_lims_export(config: Config, sample_rows: list[dict], content: str) -> Path | None:
    if config.exports.lims_root is None:
        return None

    now = datetime.now()
    export_dir = config.exports.lims_root / now.date().isoformat()
    export_dir.mkdir(parents=True, exist_ok=True)

    base_name = timestamped_lims_export_filename(sample_rows, now.strftime("%Y%m%dT%H%M%S%f"))
    candidate = export_dir / base_name
    stem = candidate.stem
    suffix = candidate.suffix
    counter = 2
    while candidate.exists():
        candidate = export_dir / f"{stem}-{counter}{suffix}"
        counter += 1

    candidate.write_text(content, encoding="utf-8")
    return candidate


def append_warning(url: str, message: str) -> str:
    return append_message(url, "warning", message)


def append_notice(url: str, message: str) -> str:
    return append_message(url, "notice", message)


def append_message(url: str, key: str, message: str) -> str:
    parts = urlsplit(url)
    query_items = parse_qsl(parts.query, keep_blank_values=True)
    query_items = [(item_key, value) for item_key, value in query_items if item_key != key]
    query_items.append((key, message))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query_items), parts.fragment))


def request_url_without_messages(url: str) -> str:
    parts = urlsplit(url)
    query_items = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key not in {"warning", "notice"}
    ]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query_items), parts.fragment))


def safe_local_redirect(url: str | None, default: str = "/") -> str:
    if not url:
        return default
    parts = urlsplit(url)
    if parts.scheme or parts.netloc or not parts.path.startswith("/"):
        return default
    return urlunsplit(("", "", parts.path, parts.query, parts.fragment))


def build_igv_url(config: Config, sample_row, outputs: dict | None = None) -> str:
    if not config.features.igv or not config.igv.enabled:
        raise HTTPException(status_code=404, detail="IGV integration is disabled")

    resolved_outputs = outputs if outputs is not None else effective_outputs(config, sample_row)
    root = config.get_root(sample_row["source_root_name"])
    if root is None:
        raise HTTPException(status_code=500, detail="Missing results root mapping")

    sample_rel = Path(sample_row["sample_results_relpath"])
    sample_windows_root = PureWindowsPath(root.windows_path)

    def convert_output(key: str) -> str | None:
        relname = effective_output_relname(key, resolved_outputs)
        if not relname:
            return None
        windows_path = sample_windows_root.joinpath(PureWindowsPath(sample_rel.as_posix())).joinpath(relname)
        return windows_path_to_igv_path(str(windows_path))

    genome = convert_output("main_fasta")
    if not genome:
        raise HTTPException(status_code=404, detail="No genome FASTA available for IGV")

    file_keys = [
        "main_cram",
        "filtered_vcf_m005",
        "filtered_vcf_m01",
        "filtered_vcf_m015",
        "filtered_vcf_m02",
        "filtered_vcf_m03",
        "filtered_vcf_m04",
        "vadr_bed",
        "resistance_gff",
        "selected_vadr_gff",
    ]
    files: list[str] = []
    for key in file_keys:
        converted = convert_output(key)
        if converted:
            files.append(converted)
    query_items: list[tuple[str, str]] = [("genome", genome)]
    if files:
        query_items.append(("file", ",".join(files)))
    query_items.append(("merge", "false"))
    return f"{config.igv.base_url}?{urlencode(query_items)}"


def build_igv_goto_url(config: Config, locus: str) -> str:
    if not config.features.igv or not config.igv.enabled:
        raise HTTPException(status_code=404, detail="IGV integration is disabled")
    parts = urlsplit(config.igv.base_url)
    goto_path = "/goto"
    if parts.path:
        base_parts = parts.path.rstrip("/").split("/")
        if len(base_parts) > 1:
            goto_path = "/".join(base_parts[:-1] + ["goto"])
        else:
            goto_path = "/goto"
    return urlunsplit((parts.scheme, parts.netloc, goto_path, urlencode({"locus": locus}), ""))


def webigv_enabled(config: Config) -> bool:
    return config.features.igv and config.webigv.enabled


def webigv_available(outputs: dict) -> bool:
    return bool(effective_output_relname("main_fasta", outputs) and effective_output_relname("main_fasta_index", outputs))


def webigv_track_url(request: Request, sample_run_id: str, output_key: str) -> str:
    return str(request.url_for("sample_webigv_file", sample_run_id=sample_run_id, output_key=output_key))


def webigv_named_track_url(request: Request, sample_run_id: str, output_key: str, outputs: dict) -> str:
    relname = effective_output_relname(output_key, outputs) or inferred_webigv_index_relname(outputs, output_key) or output_key
    filename = Path(relname).name
    return str(
        request.url_for(
            "sample_webigv_named_file",
            sample_run_id=sample_run_id,
            output_key=output_key,
            filename=filename,
        )
    )


def build_webigv_browser_config(
    config: Config,
    request: Request,
    sample_row,
    outputs: dict | None = None,
    locus: str | None = None,
) -> dict:
    if not webigv_enabled(config):
        raise HTTPException(status_code=404, detail="webIGV integration is disabled")

    resolved_outputs = outputs if outputs is not None else effective_outputs(config, sample_row)
    if not webigv_available(resolved_outputs):
        raise HTTPException(status_code=404, detail="No indexed genome FASTA available for webIGV")

    sample_run_id = sample_row["sample_run_id"]
    browser_config = {
        "reference": {
            "id": sample_run_id,
            "name": display_identifier(sample_row) or sample_row["sample_id"],
            "fastaURL": webigv_named_track_url(request, sample_run_id, "main_fasta", resolved_outputs),
            "indexURL": webigv_named_track_url(request, sample_run_id, "main_fasta_index", resolved_outputs),
        },
        "tracks": [],
    }
    if locus:
        browser_config["locus"] = locus

    if effective_output_relname("main_cram", resolved_outputs) and webigv_output_exists(config, sample_row, resolved_outputs, "main_cram_index"):
        browser_config["tracks"].append(
            {
                "name": "Main CRAM",
                "type": "alignment",
                "format": "cram",
                "url": webigv_named_track_url(request, sample_run_id, "main_cram", resolved_outputs),
                "indexURL": webigv_named_track_url(request, sample_run_id, "main_cram_index", resolved_outputs),
                "checkSequenceMD5": False,
                "showSoftClips": True,
                "displayMode": "SQUISHED",
            }
        )

    for label, output_key in WEBIGV_VCF_TRACKS:
        index_key = f"{output_key}_index"
        if effective_output_relname(output_key, resolved_outputs) and webigv_output_exists(config, sample_row, resolved_outputs, index_key):
            browser_config["tracks"].append(
                {
                    "name": label,
                    "type": "variant",
                    "format": "vcf",
                    "url": webigv_named_track_url(request, sample_run_id, output_key, resolved_outputs),
                    "indexURL": webigv_named_track_url(request, sample_run_id, index_key, resolved_outputs),
                }
            )

    for label, output_key, file_format in WEBIGV_ANNOTATION_TRACKS:
        if effective_output_relname(output_key, resolved_outputs):
            browser_config["tracks"].append(
                {
                    "name": label,
                    "type": "annotation",
                    "format": file_format,
                    "url": webigv_named_track_url(request, sample_run_id, output_key, resolved_outputs),
                }
            )

    return browser_config


def create_app(config_path: str | Path | None = None) -> FastAPI:
    config = load_config(config_path)
    from virtitta.importer import import_run_with_report as import_run_dir
    connection = connect(config.database.path)
    try:
        init_db(connection)
        mark_stale_cluster_jobs_failed(connection)
    finally:
        connection.close()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        app.state.cluster_executor.shutdown(wait=False, cancel_futures=False)

    app = FastAPI(title=config.app.title, root_path=config.app.root_path, lifespan=lifespan)
    app.state.cluster_executor = ThreadPoolExecutor(max_workers=config.cluster.max_concurrent_jobs)

    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    app.state.config = config

    def permission_allowed(request: Request, permission: str) -> bool:
        if not config.auth.enabled:
            return True
        current_user = getattr(request.state, "current_user", None)
        return bool(current_user and current_user.can(permission))

    def require_permission(request: Request, permission: str) -> None:
        if permission_allowed(request, permission):
            return
        raise HTTPException(status_code=403, detail="Forbidden")

    def require_csrf(request: Request, csrf_token: str) -> None:
        if not config.auth.enabled:
            return
        current_user = getattr(request.state, "current_user", None)
        if current_user is None or not current_user.authenticated:
            raise HTTPException(status_code=401, detail="Authentication required")
        if not csrf_token or not secrets.compare_digest(csrf_token, current_user.csrf_token):
            raise HTTPException(status_code=403, detail="Invalid CSRF token")

    def require_authenticated_user(request: Request):
        current_user = getattr(request.state, "current_user", None)
        if not config.auth.enabled or current_user is None or not current_user.authenticated:
            raise HTTPException(status_code=401, detail="Authentication required")
        return current_user

    def can_delete_comment(request: Request, comment: dict) -> bool:
        if permission_allowed(request, PERMISSION_COMMENT_DELETE):
            return True
        current_user = getattr(request.state, "current_user", None)
        if current_user is None or not current_user.authenticated:
            return False
        if current_user.role not in {ROLE_REVIEWER, ROLE_COMMENTER}:
            return False
        return bool(comment.get("author")) and comment.get("author") == current_user.display_name

    @app.middleware("http")
    async def auth_middleware(request: Request, call_next):
        if not config.auth.enabled:
            request.state.current_user = disabled_auth_user()
            return await call_next(request)

        request.state.current_user = get_user_from_cookie(config, request.cookies.get(config.auth.cookie_name))
        path = request_path_without_root_path(request.url.path, config.app.root_path)
        if request.state.current_user is None and not is_public_request_path(path):
            if request.method in {"GET", "HEAD"}:
                next_url = request.url.path
                if request.url.query:
                    next_url = f"{next_url}?{request.url.query}"
                return RedirectResponse(f"/login?{urlencode({'next': next_url})}", status_code=303)
            return PlainTextResponse("Authentication required", status_code=401)

        response = await call_next(request)
        return response

    @app.middleware("http")
    async def root_path_redirect_middleware(request: Request, call_next):
        response = await call_next(request)
        location = response.headers.get("location")
        if (
            config.app.root_path
            and location
            and location.startswith("/")
            and not location.startswith("//")
            and location != config.app.root_path
            and not location.startswith(f"{config.app.root_path}/")
        ):
            response.headers["location"] = f"{config.app.root_path}{location}"
        return response

    @app.get("/login", response_class=HTMLResponse)
    def login_form(
        request: Request,
        next: str = Query(default="/"),
        warning: str = Query(default=""),
        notice: str = Query(default=""),
    ):
        if not config.auth.enabled:
            return RedirectResponse("/", status_code=303)
        if getattr(request.state, "current_user", None) is not None:
            return RedirectResponse(safe_local_redirect(next), status_code=303)
        return templates.TemplateResponse(
            request,
            "login.html",
            {
                "request": request,
                "config": config,
                "next_url": safe_local_redirect(next),
                "warning_message": warning if isinstance(warning, str) else "",
                "notice_message": notice if isinstance(notice, str) else "",
            },
        )

    @app.post("/login")
    async def login(
        username: str = Form(default=""),
        password: str = Form(default=""),
        next: str = Form(default="/"),
    ):
        if not config.auth.enabled:
            return RedirectResponse("/", status_code=303)
        user = authenticate_local_user(config, username, password)
        if user is None:
            return RedirectResponse(
                append_warning(f"/login?{urlencode({'next': safe_local_redirect(next)})}", "Invalid username or password."),
                status_code=303,
            )
        session_token, _current_user = create_login_session(config, user["username"])
        response = RedirectResponse(safe_local_redirect(next), status_code=303)
        response.set_cookie(
            config.auth.cookie_name,
            session_token,
            path=config.app.root_path or "/",
            httponly=True,
            secure=config.auth.cookie_secure,
            samesite="lax",
            max_age=config.auth.session_days * 24 * 60 * 60,
        )
        return response

    @app.post("/logout")
    async def logout(request: Request, csrf_token: str = Form(default="")):
        require_csrf(request, csrf_token)
        logout_session(config, request.cookies.get(config.auth.cookie_name))
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(config.auth.cookie_name, path=config.app.root_path or "/")
        return response

    @app.get("/help", response_class=HTMLResponse)
    def help_page(request: Request):
        current_user = getattr(request.state, "current_user", None)
        if current_user is not None:
            help_role = current_user.role
        elif config.auth.enabled:
            help_role = ROLE_VIEWER
        else:
            help_role = ROLE_ADMIN

        column_labels = {**DEFAULT_COLUMN_LABELS, **config.ui.column_labels}
        help_columns = [
            {
                "key": column,
                "label": column_labels.get(column, column),
                "description": HELP_COLUMN_DESCRIPTIONS.get(column, "Configured sample field."),
            }
            for column in [*table_columns(config), "comment_count", "actions"]
        ]

        return templates.TemplateResponse(
            request,
            "help.html",
            {
                "request": request,
                "config": config,
                "help_role": help_role,
                "help_columns": help_columns,
                "highlighted_columns": [
                    column_labels.get(column, column)
                    for column in table_columns(config)
                    if column in config.ui.highlight_rules
                ],
                "permissions": {
                    "category_update": permission_allowed(request, PERMISSION_CATEGORY_UPDATE),
                    "comment_add": permission_allowed(request, PERMISSION_COMMENT_ADD),
                    "comment_delete_any": permission_allowed(request, PERMISSION_COMMENT_DELETE),
                    "comment_delete_own": help_role in {ROLE_COMMENTER, ROLE_REVIEWER},
                    "export_lims": permission_allowed(request, PERMISSION_EXPORT_LIMS),
                    "group_update": permission_allowed(request, PERMISSION_GROUP_UPDATE),
                    "metadata_override": permission_allowed(request, PERMISSION_METADATA_OVERRIDE),
                    "qc_update": permission_allowed(request, PERMISSION_QC_UPDATE),
                    "run_refresh": permission_allowed(request, PERMISSION_RUN_REFRESH),
                    "sample_delete": permission_allowed(request, PERMISSION_SAMPLE_DELETE),
                },
                "cluster_enabled": config.cluster.enabled,
                "igv_enabled": config.features.igv and config.igv.enabled,
                "webigv_enabled": webigv_enabled(config),
                "warning_message": "",
                "notice_message": "",
            },
        )

    @app.get("/", response_class=HTMLResponse)
    def index(
        request: Request,
        search: str = Query(default=""),
        run_name: str = Query(default=""),
        subtype: str = Query(default=""),
        qc_status: str = Query(default=""),
        sample_category: list[str] | None = Query(default=None),
        manual_group: list[str] | None = Query(default=None),
        warning: str = Query(default=""),
        notice: str = Query(default=""),
        min_coverage_pct: str = Query(default=""),
        min_mean_depth: str = Query(default=""),
        min_blast_identity: str = Query(default=""),
        max_ct: str = Query(default=""),
        sort: str = Query(default=config.ui.default_sort),
        desc: bool = Query(default=config.ui.default_sort_desc),
    ):
        require_permission(request, PERMISSION_VIEW)
        selected_sample_categories = unique_strings(sample_category)
        selected_sample_categories = visible_category_options(config, request, selected_sample_categories)
        selected_manual_groups = unique_strings(manual_group)
        min_coverage_value = parse_optional_float(min_coverage_pct)
        min_mean_depth_value = parse_optional_float(min_mean_depth)
        min_blast_identity_value = parse_optional_float(min_blast_identity)
        max_ct_value = parse_optional_float(max_ct)
        all_columns = table_columns(config)
        visible_column_set = set(config.ui.visible_columns)
        column_presets = []

        connection = connect(config.database.path)
        try:
            rows = list_samples(
                connection,
                search=search,
                run_name=run_name,
                subtype=subtype,
                qc_status=qc_status,
                sample_categories=selected_sample_categories,
                manual_groups=selected_manual_groups,
                min_coverage_pct=min_coverage_value,
                min_mean_depth=min_mean_depth_value,
                min_blast_identity=min_blast_identity_value,
                max_ct=max_ct_value,
                excluded_sample_categories=restricted_sample_categories(config, request),
                sort=sort,
                desc=desc,
            )
            runs = list_runs(connection)
            subtypes = list_subtypes(connection)
            stored_sample_categories = list_stored_sample_categories(connection)
            available_manual_groups = list_manual_groups(connection)
            if config.auth.enabled:
                current_user = require_authenticated_user(request)
                allowed_columns = set(available_preset_columns(config))
                column_presets = list_user_column_presets(connection, current_user.username)
                for preset in column_presets:
                    preset["columns"] = [column for column in preset["columns"] if column in allowed_columns]
        finally:
            connection.close()

        available_sample_categories = configured_category_options(
            config,
            stored_sample_categories,
            selected_sample_categories,
        )
        available_sample_categories = visible_category_options(config, request, available_sample_categories)

        for row in rows:
            raw = raw_json_for_sample(row)
            row["resistance_summary"] = build_resistance_cells(raw)
            row["resistance_summary_text"] = resistance_summary_text(raw)
            row["resistance_summary_tooltip"] = resistance_tooltip_text(raw)
            row["resistance_sort_key"] = resistance_sort_key(raw)

        if sort == "resistance_summary":
            rows.sort(
                key=lambda row: (row["resistance_sort_key"], row["sample_run_id"]),
                reverse=desc,
            )

        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "request": request,
                "config": config,
                "rows": rows,
                "runs": runs,
                "subtypes": subtypes,
                "table_columns": all_columns,
                "visible_columns": config.ui.visible_columns,
                "visible_column_set": visible_column_set,
                "column_visibility_storage_key": column_visibility_storage_key(config),
                "column_presets": column_presets,
                "column_labels": {**DEFAULT_COLUMN_LABELS, **config.ui.column_labels},
                "cell_class": lambda column, value: cell_class(config, column, value),
                "cell_display_class": cell_display_class,
                "cell_style": cell_style,
                "column_style": lambda column: column_style(config, column),
                "comment_link_label": comment_link_label,
                "row_class": row_class,
                "format_value": format_value,
                "display_identifier": display_identifier,
                "bool_query_value": bool_query_value,
                "index_query_url": lambda **updates: replace_query_params(str(request.url), **updates),
                "sort": sort,
                "desc": desc,
                "search": search,
                "selected_run_name": run_name,
                "selected_subtype": subtype,
                "selected_qc_status": qc_status,
                "selected_sample_categories": selected_sample_categories,
                "selected_manual_groups": selected_manual_groups,
                "available_sample_categories": available_sample_categories,
                "available_manual_groups": available_manual_groups,
                "category_unassigned_value": CATEGORY_UNASSIGNED,
                "min_coverage_pct": min_coverage_pct,
                "min_mean_depth": min_mean_depth,
                "min_blast_identity": min_blast_identity,
                "max_ct": max_ct,
                "warning_message": warning if isinstance(warning, str) else "",
                "notice_message": notice if isinstance(notice, str) else "",
                "permissions": {
                    "category_update": permission_allowed(request, PERMISSION_CATEGORY_UPDATE),
                    "comment_add": permission_allowed(request, PERMISSION_COMMENT_ADD),
                    "export_lims": permission_allowed(request, PERMISSION_EXPORT_LIMS),
                    "export_read": permission_allowed(request, PERMISSION_EXPORT_READ),
                    "group_update": permission_allowed(request, PERMISSION_GROUP_UPDATE),
                    "qc_update": permission_allowed(request, PERMISSION_QC_UPDATE),
                    "run_refresh": permission_allowed(request, PERMISSION_RUN_REFRESH),
                    "sample_delete": permission_allowed(request, PERMISSION_SAMPLE_DELETE),
                },
                "webigv_enabled": webigv_enabled(config),
                "cluster_enabled": config.cluster.enabled,
                "qc_status_options": QC_STATUS_OPTIONS,
                "summary": {
                    "total": len(rows),
                    "pass": sum(1 for row in rows if row.get("qc_status") == "pass"),
                    "fail": sum(1 for row in rows if row.get("qc_status") == "fail"),
                    "unreviewed": sum(1 for row in rows if row.get("qc_status") == "unreviewed"),
                },
            },
        )

    @app.post("/column-presets")
    async def save_column_preset(
        request: Request,
        name: str = Form(default=""),
        columns: list[str] = Form(default=[]),
        overwrite: bool = Form(default=False),
        csrf_token: str = Form(default=""),
    ):
        require_permission(request, PERMISSION_VIEW)
        current_user = require_authenticated_user(request)
        require_csrf(request, csrf_token)

        normalized_name = name.strip()
        if not normalized_name:
            raise HTTPException(status_code=400, detail="A preset name is required")
        if len(normalized_name) > 80:
            raise HTTPException(status_code=400, detail="Preset names must be 80 characters or fewer")
        allowed_columns = set(available_preset_columns(config))
        if len(columns) != len(set(columns)) or any(column not in allowed_columns for column in columns):
            raise HTTPException(status_code=400, detail="Invalid preset columns")

        connection = connect(config.database.path)
        try:
            preset = save_user_column_preset(
                connection,
                current_user.username,
                normalized_name,
                columns,
                overwrite=overwrite,
            )
        finally:
            connection.close()
        if preset is None:
            raise HTTPException(status_code=409, detail="A preset with that name already exists")
        return JSONResponse({"preset": preset})

    @app.delete("/column-presets/{preset_id}", status_code=204)
    async def delete_column_preset(
        request: Request,
        preset_id: int,
        csrf_token: str = Form(default=""),
    ):
        require_permission(request, PERMISSION_VIEW)
        current_user = require_authenticated_user(request)
        require_csrf(request, csrf_token)
        connection = connect(config.database.path)
        try:
            deleted = delete_user_column_preset(connection, current_user.username, preset_id)
        finally:
            connection.close()
        if not deleted:
            raise HTTPException(status_code=404, detail="Column preset not found")
        return Response(status_code=204)

    @app.post("/samples/category")
    async def bulk_category_update(
        request: Request,
        sample_run_id: list[str] = Form(default=[]),
        sample_category: str = Form(default=""),
        redirect_to: str = Form(default="/"),
        csrf_token: str = Form(default=""),
    ):
        require_permission(request, PERMISSION_CATEGORY_UPDATE)
        require_csrf(request, csrf_token)
        if not sample_run_id:
            return RedirectResponse(redirect_to, status_code=303)

        normalized = sample_category.strip()
        if normalized and normalized not in config.annotations.sample_categories:
            raise HTTPException(status_code=400, detail=f"Invalid sample category: {normalized}")

        connection = connect(config.database.path)
        try:
            set_sample_category(connection, sample_run_id, normalized or None)
        finally:
            connection.close()

        if normalized:
            return RedirectResponse(
                append_notice(redirect_to, f"Assigned category '{normalized}' to {len(sample_run_id)} sample(s)."),
                status_code=303,
            )
        return RedirectResponse(
            append_notice(redirect_to, f"Cleared category for {len(sample_run_id)} sample(s)."),
            status_code=303,
        )

    @app.post("/samples/groups/add")
    async def bulk_add_group(
        request: Request,
        sample_run_id: list[str] = Form(default=[]),
        group_name: str = Form(default=""),
        redirect_to: str = Form(default="/"),
        csrf_token: str = Form(default=""),
    ):
        require_permission(request, PERMISSION_GROUP_UPDATE)
        require_csrf(request, csrf_token)
        if not sample_run_id:
            return RedirectResponse(redirect_to, status_code=303)

        normalized = group_name.strip()
        if not normalized:
            return RedirectResponse(
                append_warning(redirect_to, "A group name is required."),
                status_code=303,
            )

        connection = connect(config.database.path)
        try:
            add_samples_to_group(connection, sample_run_id, normalized)
        finally:
            connection.close()

        return RedirectResponse(
            append_notice(redirect_to, f"Added group '{normalized}' to {len(sample_run_id)} sample(s)."),
            status_code=303,
        )

    @app.post("/samples/groups/remove")
    async def bulk_remove_group(
        request: Request,
        sample_run_id: list[str] = Form(default=[]),
        group_name: str = Form(default=""),
        redirect_to: str = Form(default="/"),
        csrf_token: str = Form(default=""),
    ):
        require_permission(request, PERMISSION_GROUP_UPDATE)
        require_csrf(request, csrf_token)
        if not sample_run_id:
            return RedirectResponse(redirect_to, status_code=303)

        normalized = group_name.strip()
        if not normalized:
            return RedirectResponse(
                append_warning(redirect_to, "A group name is required."),
                status_code=303,
            )

        connection = connect(config.database.path)
        try:
            remove_samples_from_group(connection, sample_run_id, normalized)
        finally:
            connection.close()

        return RedirectResponse(
            append_notice(redirect_to, f"Removed group '{normalized}' from {len(sample_run_id)} sample(s)."),
            status_code=303,
        )

    @app.post("/samples/qc")
    async def bulk_qc_update(
        request: Request,
        sample_run_id: list[str] = Form(default=[]),
        qc_status: str = Form(...),
        comment_body: str = Form(default=""),
        comment_author: str = Form(default=""),
        redirect_to: str = Form(default="/"),
        csrf_token: str = Form(default=""),
    ):
        require_permission(request, PERMISSION_QC_UPDATE)
        require_csrf(request, csrf_token)
        if qc_status not in QC_STATUS_OPTIONS:
            raise HTTPException(status_code=400, detail=f"Invalid QC status: {qc_status}")
        if not sample_run_id:
            return RedirectResponse(redirect_to, status_code=303)
        if qc_status == "fail" and not comment_body.strip():
            return RedirectResponse(
                append_warning(redirect_to, "A comment is required when failing a sample."),
                status_code=303,
            )

        connection = connect(config.database.path)
        try:
            current_user = getattr(request.state, "current_user", None)
            actor = current_user.display_name if config.auth.enabled and current_user is not None else None
            update_qc_status(connection, sample_run_id, qc_status, actor)
            if comment_body.strip():
                for item in sample_run_id:
                    add_comment(connection, item, comment_body, actor or comment_author or None)
        finally:
            connection.close()
        return RedirectResponse(redirect_to, status_code=303)

    @app.post("/samples/delete")
    async def bulk_delete_samples(
        request: Request,
        sample_run_id: list[str] = Form(default=[]),
        redirect_to: str = Form(default="/"),
        csrf_token: str = Form(default=""),
    ):
        require_permission(request, PERMISSION_SAMPLE_DELETE)
        require_csrf(request, csrf_token)
        if not sample_run_id:
            return RedirectResponse(redirect_to, status_code=303)

        connection = connect(config.database.path)
        try:
            delete_samples(connection, sample_run_id)
        finally:
            connection.close()
        return RedirectResponse(request_url_without_messages(redirect_to), status_code=303)

    @app.post("/samples/lims-export")
    async def bulk_lims_export(
        request: Request,
        sample_run_id: list[str] = Form(default=[]),
        redirect_to: str = Form(default="/"),
        csrf_token: str = Form(default=""),
    ):
        require_permission(request, PERMISSION_EXPORT_LIMS)
        require_csrf(request, csrf_token)
        if not sample_run_id:
            return RedirectResponse(redirect_to, status_code=303)

        sample_rows = load_sample_rows(config, sample_run_id, request)

        if not sample_rows:
            raise HTTPException(status_code=404, detail="No matching samples found")
        if any(row.get("qc_status") == "unreviewed" for row in sample_rows):
            return RedirectResponse(
                append_warning(redirect_to, "LIMS export is blocked for unreviewed samples."),
                status_code=303,
            )

        content = build_lims_export_content(config, sample_rows)
        export_path = write_server_lims_export(config, sample_rows, content)
        if export_path is None:
            return RedirectResponse(
                append_warning(redirect_to, "No server-side LIMS export root is configured."),
                status_code=303,
            )
        return RedirectResponse(
            append_notice(redirect_to, f"LIMS export written to {export_path}"),
            status_code=303,
        )

    @app.post("/samples/lims-export/download")
    async def bulk_lims_export_download(
        request: Request,
        sample_run_id: list[str] = Form(default=[]),
        redirect_to: str = Form(default="/"),
        csrf_token: str = Form(default=""),
    ):
        require_permission(request, PERMISSION_EXPORT_READ)
        require_csrf(request, csrf_token)
        if not sample_run_id:
            return RedirectResponse(redirect_to, status_code=303)

        sample_rows = load_sample_rows(config, sample_run_id, request)

        if not sample_rows:
            raise HTTPException(status_code=404, detail="No matching samples found")
        if any(row.get("qc_status") == "unreviewed" for row in sample_rows):
            return RedirectResponse(
                append_warning(redirect_to, "LIMS export is blocked for unreviewed samples."),
                status_code=303,
            )

        content = build_lims_export_content(config, sample_rows)
        filename = lims_export_filename(sample_rows)
        return Response(
            content=content,
            media_type="text/tab-separated-values; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @app.post("/samples/clipboard/fasta")
    async def bulk_fasta_clipboard_export(
        request: Request,
        sample_run_id: list[str] = Form(default=[]),
        header_id: str | None = Form(default=None),
        csrf_token: str = Form(default=""),
    ):
        require_permission(request, PERMISSION_EXPORT_READ)
        require_csrf(request, csrf_token)
        if not sample_run_id:
            raise HTTPException(status_code=400, detail="No samples selected")
        sample_rows = load_sample_rows(config, sample_run_id, request)
        if not sample_rows:
            raise HTTPException(status_code=404, detail="No matching samples found")
        header_id = header_id if isinstance(header_id, str) and header_id else "lid"
        if header_id not in {"lid", "sample_id"}:
            raise HTTPException(status_code=400, detail="Unsupported FASTA header identifier")
        artifact_sources = set()
        return Response(
            content=build_fasta_clipboard_content(
                config, sample_rows, "export_fasta", header_id, artifact_sources
            ),
            media_type="text/plain; charset=utf-8",
            headers=artifact_response_headers(artifact_sources),
        )

    @app.post("/samples/clipboard/iupac-fasta")
    async def bulk_iupac_fasta_clipboard_export(
        request: Request,
        sample_run_id: list[str] = Form(default=[]),
        header_id: str | None = Form(default=None),
        csrf_token: str = Form(default=""),
    ):
        require_permission(request, PERMISSION_EXPORT_READ)
        require_csrf(request, csrf_token)
        if not sample_run_id:
            raise HTTPException(status_code=400, detail="No samples selected")
        sample_rows = load_sample_rows(config, sample_run_id, request)
        if not sample_rows:
            raise HTTPException(status_code=404, detail="No matching samples found")
        header_id = header_id if isinstance(header_id, str) and header_id else "lid"
        if header_id not in {"lid", "sample_id"}:
            raise HTTPException(status_code=400, detail="Unsupported FASTA header identifier")
        artifact_sources = set()
        return Response(
            content=build_fasta_clipboard_content(
                config, sample_rows, "export_iupac_fasta", header_id, artifact_sources
            ),
            media_type="text/plain; charset=utf-8",
            headers=artifact_response_headers(artifact_sources),
        )

    @app.post("/clusters")
    async def create_cluster(
        request: Request,
        sample_run_id: list[str] = Form(default=[]),
        redirect_to: str = Form(default="/"),
        allow_duplicate_ids: str = Form(default=""),
        csrf_token: str = Form(default=""),
    ):
        require_permission(request, PERMISSION_EXPORT_READ)
        require_csrf(request, csrf_token)
        if not config.cluster.enabled:
            return RedirectResponse(
                append_warning(redirect_to, "Cluster analysis is disabled."),
                status_code=303,
            )
        if len(sample_run_id) < MIN_CLUSTER_SAMPLES:
            return RedirectResponse(
                append_warning(redirect_to, f"Select at least {MIN_CLUSTER_SAMPLES} samples for clustering."),
                status_code=303,
            )

        job_id = secrets.token_urlsafe(12)
        sample_rows = load_sample_rows(config, sample_run_id, request)
        if len(sample_rows) < MIN_CLUSTER_SAMPLES:
            return RedirectResponse(
                append_warning(redirect_to, f"Select at least {MIN_CLUSTER_SAMPLES} visible samples for clustering."),
                status_code=303,
            )

        connection = connect(config.database.path)
        try:
            try:
                sample_records, warning_text = prepare_cluster_files(
                    config,
                    connection,
                    sample_rows,
                    job_id,
                    allow_duplicate_ids=allow_duplicate_ids == "true",
                )
                write_command_snapshot(config, job_id)
            except ClusterError as exc:
                return RedirectResponse(
                    append_warning(redirect_to, str(exc)),
                    status_code=303,
                )

            create_cluster_job(
                connection,
                {
                    "id": job_id,
                    "status": "queued",
                    "created_at": utc_now(),
                    "started_at": None,
                    "completed_at": None,
                    "selected_count": len(sample_rows),
                    "warning_text": warning_text,
                    "error_text": None,
                    "output_relpath": job_id,
                    "artifacts_json": json.dumps(cluster_artifacts(job_id), sort_keys=True),
                    "config_json": json.dumps(command_config_snapshot(config), sort_keys=True),
                    "public_token": secrets.token_urlsafe(24),
                },
                sample_records,
            )
        finally:
            connection.close()

        request.app.state.cluster_executor.submit(run_cluster_job, config, job_id)
        return RedirectResponse(str(request.url_for("cluster_detail", job_id=job_id)), status_code=303)

    @app.post("/distance-matrices")
    async def create_distance_matrices(
        request: Request,
        sample_run_id: list[str] = Form(default=[]),
        redirect_to: str = Form(default="/"),
        allow_duplicate_ids: str = Form(default=""),
        csrf_token: str = Form(default=""),
    ):
        require_permission(request, PERMISSION_EXPORT_READ)
        require_csrf(request, csrf_token)
        if not config.cluster.enabled:
            return RedirectResponse(append_warning(redirect_to, "Distance analysis is disabled."), status_code=303)
        if len(sample_run_id) < MIN_DISTANCE_SAMPLES:
            return RedirectResponse(
                append_warning(redirect_to, f"Select at least {MIN_DISTANCE_SAMPLES} samples for distance matrices."),
                status_code=303,
            )
        job_id = secrets.token_urlsafe(12)
        sample_rows = load_sample_rows(config, sample_run_id, request)
        if len(sample_rows) < MIN_DISTANCE_SAMPLES:
            return RedirectResponse(
                append_warning(redirect_to, f"Select at least {MIN_DISTANCE_SAMPLES} visible samples for distance matrices."),
                status_code=303,
            )
        connection = connect(config.database.path)
        try:
            try:
                sample_records, warning_text = prepare_distance_files(
                    config, connection, sample_rows, job_id, allow_duplicate_ids=allow_duplicate_ids == "true"
                )
                snapshot = distance_config_snapshot(config)
                commands_path = config.cluster.output_root / distance_artifacts(job_id)["commands"]
                commands_path.write_text(json.dumps(snapshot, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            except ClusterError as exc:
                return RedirectResponse(append_warning(redirect_to, str(exc)), status_code=303)
            create_cluster_job(
                connection,
                {
                    "id": job_id, "status": "queued", "created_at": utc_now(), "started_at": None,
                    "completed_at": None, "selected_count": len(sample_rows), "warning_text": warning_text,
                    "error_text": None, "output_relpath": job_id,
                    "artifacts_json": json.dumps(distance_artifacts(job_id), sort_keys=True),
                    "config_json": json.dumps(snapshot, sort_keys=True), "public_token": secrets.token_urlsafe(24),
                },
                sample_records,
            )
        finally:
            connection.close()
        request.app.state.cluster_executor.submit(run_distance_job, config, job_id)
        return RedirectResponse(str(request.url_for("distance_detail", job_id=job_id)), status_code=303)

    @app.get("/distance-matrices/{job_id}", response_class=HTMLResponse)
    def distance_detail(request: Request, job_id: str):
        require_permission(request, PERMISSION_EXPORT_READ)
        connection = connect(config.database.path)
        try:
            job = get_cluster_job(connection, job_id)
            if job is None or json.loads(job.get("config_json") or "{}").get("analysis_type") != DISTANCE_ANALYSIS_TYPE:
                raise HTTPException(status_code=404, detail="Distance-matrix job not found")
            samples = get_cluster_job_samples(connection, job_id)
        finally:
            connection.close()
        result = None
        if job["status"] == "completed":
            try:
                result = ensure_result_ordering(
                    json.loads(distance_artifact_path(config, job, DISTANCE_RESULT_ARTIFACT).read_text(encoding="utf-8"))
                )
            except (ClusterError, json.JSONDecodeError):
                result = None
        return templates.TemplateResponse(request, "distance_detail.html", {
            "request": request, "config": config, "job": job, "samples": samples,
            "artifacts": json.loads(job.get("artifacts_json") or "{}"), "result": result,
            "status_url": str(request.url_for("distance_status", job_id=job_id)),
            "warning_message": "", "notice_message": "",
        })

    @app.get("/distance-matrices/{job_id}/status")
    def distance_status(request: Request, job_id: str):
        require_permission(request, PERMISSION_EXPORT_READ)
        connection = connect(config.database.path)
        try:
            job = get_cluster_job(connection, job_id)
            if job is None or json.loads(job.get("config_json") or "{}").get("analysis_type") != DISTANCE_ANALYSIS_TYPE:
                raise HTTPException(status_code=404, detail="Distance-matrix job not found")
        finally:
            connection.close()
        return cluster_job_response(job)

    @app.get("/distance-matrices/{job_id}/artifacts/{artifact_key}")
    def distance_artifact(request: Request, job_id: str, artifact_key: str):
        require_permission(request, PERMISSION_EXPORT_READ)
        connection = connect(config.database.path)
        try:
            job = get_cluster_job(connection, job_id)
            if job is None or json.loads(job.get("config_json") or "{}").get("analysis_type") != DISTANCE_ANALYSIS_TYPE:
                raise HTTPException(status_code=404, detail="Distance-matrix job not found")
        finally:
            connection.close()
        try:
            file_path = distance_artifact_path(config, job, artifact_key)
        except ClusterError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return FileResponse(file_path, filename=file_path.name, media_type="text/plain; charset=utf-8", content_disposition_type="inline")

    @app.get("/clusters/{job_id}", response_class=HTMLResponse)
    def cluster_detail(request: Request, job_id: str):
        require_permission(request, PERMISSION_EXPORT_READ)
        connection = connect(config.database.path)
        try:
            job = get_cluster_job(connection, job_id)
            if job is None or job_analysis_type(job) != "cluster":
                raise HTTPException(status_code=404, detail="Cluster job not found")
            samples = get_cluster_job_samples(connection, job_id)
        finally:
            connection.close()

        artifacts = json.loads(job.get("artifacts_json") or "{}")
        return templates.TemplateResponse(
            request,
            "cluster_detail.html",
            {
                "request": request,
                "config": config,
                "job": job,
                "samples": samples,
                "artifacts": artifacts,
                "status_url": str(request.url_for("cluster_status", job_id=job_id)),
                "grapetree_url": build_grapetree_url(config, request, job) if job["status"] == "completed" else "",
                "warning_message": "",
                "notice_message": "",
            },
        )

    @app.get("/clusters/{job_id}/status")
    def cluster_status(request: Request, job_id: str):
        require_permission(request, PERMISSION_EXPORT_READ)
        connection = connect(config.database.path)
        try:
            job = get_cluster_job(connection, job_id)
            if job is None or job_analysis_type(job) != "cluster":
                raise HTTPException(status_code=404, detail="Cluster job not found")
        finally:
            connection.close()
        return cluster_job_response(job)

    @app.get("/clusters/{job_id}/artifacts/{artifact_key}")
    def cluster_artifact(request: Request, job_id: str, artifact_key: str):
        require_permission(request, PERMISSION_EXPORT_READ)
        connection = connect(config.database.path)
        try:
            job = get_cluster_job(connection, job_id)
            if job is None or job_analysis_type(job) != "cluster":
                raise HTTPException(status_code=404, detail="Cluster job not found")
        finally:
            connection.close()
        try:
            normalized_artifact_key = CLUSTER_ARTIFACT_ALIASES.get(artifact_key, artifact_key)
            if normalized_artifact_key == ARTIFACT_GRAPETREE_JSON:
                ensure_grapetree_json(config, job)
            file_path = artifact_path(config, job, artifact_key)
        except ClusterError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return FileResponse(
            file_path,
            filename=file_path.name,
            media_type="text/plain; charset=utf-8",
            content_disposition_type="inline",
        )

    @app.get("/clusters/public/{public_token}/{artifact_key}", name="cluster_public_artifact")
    def cluster_public_artifact(public_token: str, artifact_key: str):
        normalized_artifact_key = CLUSTER_ARTIFACT_ALIASES.get(artifact_key, artifact_key)
        if normalized_artifact_key not in PUBLIC_CLUSTER_ARTIFACTS:
            raise HTTPException(status_code=404, detail="Cluster artifact is not public")
        connection = connect(config.database.path)
        try:
            job = get_cluster_job_by_public_token(connection, public_token)
            if job is None or job_analysis_type(job) != "cluster" or job["status"] != "completed":
                raise HTTPException(status_code=404, detail="Cluster job not found")
        finally:
            connection.close()
        try:
            if normalized_artifact_key == ARTIFACT_GRAPETREE_JSON:
                ensure_grapetree_json(config, job)
            file_path = artifact_path(config, job, normalized_artifact_key)
        except ClusterError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return FileResponse(
            file_path,
            filename=file_path.name,
            media_type="text/plain; charset=utf-8",
            content_disposition_type="inline",
            headers={"Access-Control-Allow-Origin": "*"},
        )

    @app.get("/samples/{sample_run_id}", response_class=HTMLResponse)
    def sample_detail(
        request: Request,
        sample_run_id: str,
        warning: str = Query(default=""),
        notice: str = Query(default=""),
    ):
        require_permission(request, PERMISSION_VIEW)
        connection = connect(config.database.path)
        try:
            sample_row = get_sample(connection, sample_run_id)
            sample_row = require_visible_sample(config, request, sample_row)
            comments = get_comments(connection, sample_run_id)
            raw = raw_json_for_sample(sample_row)
            outputs = effective_outputs(config, sample_row, raw)
            offline_cached_outputs = []
            for output_key in config.cache.output_keys:
                if not effective_output_relname(output_key, outputs):
                    continue
                resolved = resolve_cached_output(config, connection, sample_row, output_key)
                if resolved is not None and resolved.source == CACHE_OFFLINE_UNVERIFIED:
                    offline_cached_outputs.append(output_key)
        finally:
            connection.close()

        cache_warning = ""
        if offline_cached_outputs:
            cache_warning = (
                "Result storage is unavailable; this page is using unverified cached copies for: "
                + ", ".join(offline_cached_outputs)
                + "."
            )
        igv_url = None
        if config.features.igv and config.igv.enabled:
            try:
                igv_url = build_igv_url(config, sample_row, outputs)
            except HTTPException:
                igv_url = None
        webigv_url = None
        if webigv_enabled(config) and webigv_available(outputs):
            webigv_url = str(request.url_for("sample_webigv", sample_run_id=sample_run_id))

        resistance_cells = build_resistance_cells(raw)
        resistance_mutations = build_resistance_mutations(raw, sample_row["sample_id"])
        resistance_summary = raw.get("resistance", {}) if isinstance(raw, dict) else {}

        return templates.TemplateResponse(
            request,
            "sample_detail.html",
            {
                "request": request,
                "config": config,
                "sample": sample_row,
                "sample_json": raw,
                "sample_json_pretty": json.dumps(raw, indent=2, sort_keys=True),
                "comments": comments,
                "format_value": format_value,
                "display_identifier": display_identifier,
                "igv_url": igv_url,
                "webigv_url": webigv_url,
                "resistance_cells": resistance_cells,
                "resistance_mutations": resistance_mutations,
                "resistance_analysis_present": bool(resistance_summary.get("analysis_present")),
                "resistance_has_calls": bool(resistance_summary.get("has_resistance")),
                "warning_message": " ".join(
                    item for item in (warning if isinstance(warning, str) else "", cache_warning) if item
                ),
                "notice_message": notice if isinstance(notice, str) else "",
                "can_delete_comment": lambda comment: can_delete_comment(request, comment),
                "permissions": {
                    "comment_add": permission_allowed(request, PERMISSION_COMMENT_ADD),
                    "comment_delete": permission_allowed(request, PERMISSION_COMMENT_DELETE),
                    "export_lims": permission_allowed(request, PERMISSION_EXPORT_LIMS),
                    "export_read": permission_allowed(request, PERMISSION_EXPORT_READ),
                    "metadata_override": permission_allowed(request, PERMISSION_METADATA_OVERRIDE),
                    "qc_update": permission_allowed(request, PERMISSION_QC_UPDATE),
                    "run_refresh": permission_allowed(request, PERMISSION_RUN_REFRESH),
                    "sample_delete": permission_allowed(request, PERMISSION_SAMPLE_DELETE),
                },
                "qc_status_options": QC_STATUS_OPTIONS,
                "sample_categories": config.annotations.sample_categories,
                "refresh_run_name": sample_row["run_name"],
                "detail_links": output_links(outputs, DETAIL_FILE_LINKS),
                "igv_track_links": output_links(outputs, IGV_TRACK_LINKS),
            },
        )

    @app.post("/samples/{sample_run_id}/overrides")
    async def update_sample_overrides(
        request: Request,
        sample_run_id: str,
        lid: str = Form(default=""),
        sequencing_date: str = Form(default=""),
        sample_metadata_ct: str = Form(default=""),
        sample_metadata_library_concentration_ng_ul: str = Form(default=""),
        typing_report_subtype: str = Form(default=""),
        csrf_token: str = Form(default=""),
    ):
        require_permission(request, PERMISSION_METADATA_OVERRIDE)
        require_csrf(request, csrf_token)
        try:
            values = {
                "lid": lid,
                "sequencing_date": parse_optional_date(sequencing_date),
                "sample_metadata_ct": parse_optional_float(sample_metadata_ct),
                "sample_metadata_library_concentration_ng_ul": parse_optional_float(
                    sample_metadata_library_concentration_ng_ul
                ),
                "typing_report_subtype": typing_report_subtype,
            }
        except ValueError:
            return RedirectResponse(
                append_warning(f"/samples/{sample_run_id}", "Override values must use valid dates and numbers."),
                status_code=303,
            )

        connection = connect(config.database.path)
        try:
            sample_row = get_sample(connection, sample_run_id)
            require_visible_sample(config, request, sample_row)
            current_user = getattr(request.state, "current_user", None)
            actor = current_user.display_name if config.auth.enabled and current_user is not None else None
            changes = set_sample_field_overrides(connection, sample_run_id, values, updated_by=actor)
            for change in changes:
                add_comment(connection, sample_run_id, override_comment_text(change), actor or "Virtitta")
        finally:
            connection.close()

        if changes:
            return RedirectResponse(
                append_notice(f"/samples/{sample_run_id}", f"Updated {len(changes)} override(s)."),
                status_code=303,
            )
        return RedirectResponse(request_url_without_messages(f"/samples/{sample_run_id}"), status_code=303)

    @app.post("/samples/{sample_run_id}/comments")
    async def create_comment(
        request: Request,
        sample_run_id: str,
        body: str = Form(...),
        author: str = Form(default=""),
        csrf_token: str = Form(default=""),
    ):
        require_permission(request, PERMISSION_COMMENT_ADD)
        require_csrf(request, csrf_token)
        connection = connect(config.database.path)
        try:
            sample_row = get_sample(connection, sample_run_id)
            require_visible_sample(config, request, sample_row)
            if not body.strip():
                return RedirectResponse(f"/samples/{sample_run_id}", status_code=303)
            current_user = getattr(request.state, "current_user", None)
            actor = current_user.display_name if config.auth.enabled and current_user is not None else None
            add_comment(connection, sample_run_id, body, actor or author or None)
        finally:
            connection.close()
        return RedirectResponse(request_url_without_messages(f"/samples/{sample_run_id}"), status_code=303)

    @app.post("/samples/{sample_run_id}/comments/{comment_id}/delete")
    async def remove_comment(
        request: Request,
        sample_run_id: str,
        comment_id: int,
        csrf_token: str = Form(default=""),
    ):
        require_csrf(request, csrf_token)
        connection = connect(config.database.path)
        try:
            sample_row = get_sample(connection, sample_run_id)
            require_visible_sample(config, request, sample_row)
            comment = next(
                (comment for comment in get_comments(connection, sample_run_id) if comment["id"] == comment_id),
                None,
            )
            if comment is None:
                raise HTTPException(status_code=404, detail="Comment not found")
            if not can_delete_comment(request, comment):
                raise HTTPException(status_code=403, detail="Forbidden")
            delete_comment(connection, sample_run_id, comment_id)
        finally:
            connection.close()
        return RedirectResponse(request_url_without_messages(f"/samples/{sample_run_id}#comments"), status_code=303)

    @app.post("/samples/{sample_run_id}/delete")
    async def delete_single_sample(
        request: Request,
        sample_run_id: str,
        csrf_token: str = Form(default=""),
    ):
        require_permission(request, PERMISSION_SAMPLE_DELETE)
        require_csrf(request, csrf_token)
        connection = connect(config.database.path)
        try:
            sample_row = get_sample(connection, sample_run_id)
            sample_row = require_visible_sample(config, request, sample_row)
            run_name = sample_row["run_name"]
            delete_samples(connection, [sample_run_id])
        finally:
            connection.close()
        return RedirectResponse(request_url_without_messages(f"/?run_name={run_name}"), status_code=303)

    @app.get("/samples/{sample_run_id}/files/{output_key}")
    def sample_file(request: Request, sample_run_id: str, output_key: str):
        require_permission(request, PERMISSION_EXPORT_READ)
        resolved = None
        connection = connect(config.database.path)
        try:
            sample_row = get_sample(connection, sample_run_id)
            sample_row = require_visible_sample(config, request, sample_row)
            resolved = resolve_cached_output(config, connection, sample_row, output_key)
        finally:
            connection.close()

        outputs = effective_outputs(config, sample_row)
        if resolved is not None:
            return FileResponse(
                resolved.path,
                filename=outputs.get(output_key) or resolved.path.name,
                headers=artifact_response_headers({resolved.source}),
            )
        file_path, relname = resolve_output_file(config, sample_row, output_key)
        return FileResponse(file_path, filename=relname, headers=artifact_response_headers({"live"}))

    @app.get("/samples/{sample_run_id}/files/{output_key}/view")
    def sample_file_view(request: Request, sample_run_id: str, output_key: str):
        require_permission(request, PERMISSION_EXPORT_READ)
        if output_key not in VIEWABLE_DETAIL_OUTPUT_KEYS:
            raise HTTPException(status_code=404, detail="Output is not available for browser viewing")

        resolved = None
        connection = connect(config.database.path)
        try:
            sample_row = get_sample(connection, sample_run_id)
            sample_row = require_visible_sample(config, request, sample_row)
            resolved = resolve_cached_output(config, connection, sample_row, output_key)
        finally:
            connection.close()

        outputs = effective_outputs(config, sample_row)
        if resolved is not None:
            return FileResponse(
                resolved.path,
                filename=outputs.get(output_key) or resolved.path.name,
                media_type=DETAIL_VIEW_MEDIA_TYPE,
                content_disposition_type="inline",
                headers=artifact_response_headers({resolved.source}),
            )
        file_path, relname = resolve_output_file(config, sample_row, output_key)
        return FileResponse(
            file_path,
            filename=relname,
            media_type=DETAIL_VIEW_MEDIA_TYPE,
            content_disposition_type="inline",
            headers=artifact_response_headers({"live"}),
        )

    @app.get("/samples/{sample_run_id}/webigv", response_class=HTMLResponse)
    def sample_webigv(request: Request, sample_run_id: str, locus: str = Query(default="")):
        require_permission(request, PERMISSION_EXPORT_READ)
        connection = connect(config.database.path)
        try:
            sample_row = get_sample(connection, sample_run_id)
            sample_row = require_visible_sample(config, request, sample_row)
            raw = raw_json_for_sample(sample_row)
        finally:
            connection.close()

        outputs = effective_outputs(config, sample_row, raw)
        browser_config = build_webigv_browser_config(
            config,
            request,
            sample_row,
            outputs,
            locus.strip() or None,
        )
        return templates.TemplateResponse(
            request,
            "webigv.html",
            {
                "request": request,
                "config": config,
                "sample": sample_row,
                "display_identifier": display_identifier,
                "webigv_config_json": json.dumps(browser_config),
                "warning_message": "",
                "notice_message": "",
            },
        )

    @app.get("/samples/{sample_run_id}/webigv/files/{output_key}")
    def sample_webigv_file(request: Request, sample_run_id: str, output_key: str):
        return serve_webigv_file(request, sample_run_id, output_key)

    @app.get("/samples/{sample_run_id}/webigv/files/{output_key}/{filename}")
    def sample_webigv_named_file(request: Request, sample_run_id: str, output_key: str, filename: str):
        return serve_webigv_file(request, sample_run_id, output_key)

    def serve_webigv_file(request: Request, sample_run_id: str, output_key: str):
        require_permission(request, PERMISSION_EXPORT_READ)
        if not webigv_enabled(config):
            raise HTTPException(status_code=404, detail="webIGV integration is disabled")
        if output_key not in WEBIGV_ALLOWED_OUTPUT_KEYS:
            raise HTTPException(status_code=404, detail="Output is not available for webIGV")

        connection = connect(config.database.path)
        try:
            sample_row = get_sample(connection, sample_run_id)
            sample_row = require_visible_sample(config, request, sample_row)
        finally:
            connection.close()

        file_path, relname = resolve_webigv_output_file(config, sample_row, output_key)
        return FileResponse(file_path, filename=relname, content_disposition_type="inline")

    @app.post("/runs/{run_name}/refresh")
    async def refresh_run_metadata(
        request: Request,
        run_name: str,
        redirect_to: str = Form(default="/"),
        csrf_token: str = Form(default=""),
    ):
        require_permission(request, PERMISSION_RUN_REFRESH)
        require_csrf(request, csrf_token)
        connection = connect(config.database.path)
        try:
            run_row = get_run(connection, run_name)
        finally:
            connection.close()

        if run_row is None:
            raise HTTPException(status_code=404, detail="Run not found")

        root = config.get_root(run_row["source_root_name"])
        if root is None:
            raise HTTPException(status_code=500, detail=f"Configured results root not found: {run_row['source_root_name']}")

        run_dir = (root.linux_path / run_row["run_relpath"]).resolve()
        report = import_run_dir(config, run_dir)
        redirect_url = append_notice(
            request_url_without_messages(redirect_to),
            f"Refreshed run metadata from per-sample QC summaries ({report.imported} samples).",
        )
        if report.warnings:
            redirect_url = append_warning(redirect_url, " ".join(report.warnings))
        return RedirectResponse(redirect_url, status_code=303)

    @app.get("/samples/{sample_run_id}/lims-export")
    def sample_lims_export(request: Request, sample_run_id: str):
        require_permission(request, PERMISSION_EXPORT_LIMS)
        connection = connect(config.database.path)
        try:
            sample_row = get_sample(connection, sample_run_id)
            sample_row = require_visible_sample(config, request, sample_row)
        finally:
            connection.close()

        if sample_row.get("qc_status") == "unreviewed":
            return RedirectResponse(
                append_warning(
                    f"/samples/{sample_run_id}",
                    "LIMS export is blocked until QC is marked pass or fail.",
                ),
                status_code=303,
            )

        content = build_lims_export_content(config, [sample_row])
        export_path = write_server_lims_export(config, [sample_row], content)
        if export_path is None:
            return RedirectResponse(
                append_warning(
                    f"/samples/{sample_run_id}",
                    "No server-side LIMS export root is configured.",
                ),
                status_code=303,
            )
        return RedirectResponse(
            append_notice(f"/samples/{sample_run_id}", f"LIMS export written to {export_path}"),
            status_code=303,
        )

    @app.get("/samples/{sample_run_id}/lims-export/download")
    def sample_lims_export_download(request: Request, sample_run_id: str):
        require_permission(request, PERMISSION_EXPORT_READ)
        connection = connect(config.database.path)
        try:
            sample_row = get_sample(connection, sample_run_id)
            sample_row = require_visible_sample(config, request, sample_row)
        finally:
            connection.close()

        if sample_row.get("qc_status") == "unreviewed":
            return RedirectResponse(
                append_warning(
                    f"/samples/{sample_run_id}",
                    "LIMS export is blocked until QC is marked pass or fail.",
                ),
                status_code=303,
            )

        content = build_lims_export_content(config, [sample_row])
        filename = lims_export_filename([sample_row])
        return Response(
            content=content,
            media_type="text/tab-separated-values; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @app.get("/samples/{sample_run_id}/igv")
    def sample_igv(request: Request, sample_run_id: str):
        require_permission(request, PERMISSION_EXPORT_READ)
        connection = connect(config.database.path)
        try:
            sample_row = get_sample(connection, sample_run_id)
            sample_row = require_visible_sample(config, request, sample_row)
        finally:
            connection.close()

        return RedirectResponse(build_igv_url(config, sample_row), status_code=307)

    @app.get("/samples/{sample_run_id}/igv/mutations/{mutation_index}")
    def sample_igv_mutation(request: Request, sample_run_id: str, mutation_index: int):
        require_permission(request, PERMISSION_EXPORT_READ)
        connection = connect(config.database.path)
        try:
            sample_row = get_sample(connection, sample_run_id)
            sample_row = require_visible_sample(config, request, sample_row)
        finally:
            connection.close()

        raw = raw_json_for_sample(sample_row)
        mutations = build_resistance_mutations(raw, sample_row["sample_id"])
        if mutation_index < 0 or mutation_index >= len(mutations):
            raise HTTPException(status_code=404, detail="Resistance mutation not found")
        locus = mutations[mutation_index].get("locus")
        if not locus:
            raise HTTPException(status_code=404, detail="No genomic locus available for mutation")
        return RedirectResponse(build_igv_goto_url(config, locus), status_code=307)

    return app
