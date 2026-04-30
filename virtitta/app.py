from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path, PureWindowsPath
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from virtitta.config import DEFAULT_COLUMN_LABELS, QC_STATUS_OPTIONS, Config, load_config
from virtitta.repository import (
    add_comment,
    add_samples_to_group,
    connect,
    delete_comment,
    delete_samples,
    get_comments,
    get_run,
    get_sample,
    init_db,
    list_manual_groups,
    list_runs,
    list_stored_sample_categories,
    list_subtypes,
    list_samples,
    raw_json_for_sample,
    remove_samples_from_group,
    set_sample_category,
    set_sample_field_overrides,
    update_qc_status,
)


TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

DETAIL_FILE_LINKS = [
    ("Export FASTA", "export_fasta"),
    ("Export 0.15 IUPAC FASTA", "export_iupac_fasta"),
    ("Main FASTA", "main_fasta"),
    ("Main CRAM", "main_cram"),
    ("0.15 IUPAC FASTA", "iupac_fasta"),
    ("0.15 IUPAC CRAM", "iupac_cram"),
    ("Coverage TSV", "coverage_tsv"),
    ("Resistance TSV", "resistance_tsv"),
    ("Resistance GFF", "resistance_gff"),
    ("VADR BED", "vadr_bed"),
    ("Selected VADR GFF", "selected_vadr_gff"),
    ("Display Rug Plot", "display_rug_kde_plot"),
    ("LID 2limsrs", "lid_2limsrs"),
]

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


def column_visibility_storage_key(config: Config) -> str:
    payload = {
        "table_columns": config.ui.table_columns,
        "visible_columns": config.ui.visible_columns,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    digest = hashlib.sha1(encoded).hexdigest()[:12]
    return f"virtitta.columnVisibility.v2.{digest}"


def configured_category_options(config: Config, stored_categories: list[str], selected_categories: list[str]) -> list[str]:
    options = list(config.annotations.sample_categories)
    for category in list(stored_categories) + list(selected_categories):
        if category == CATEGORY_UNASSIGNED or category in options:
            continue
        options.append(category)
    return options


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
    relpath = Path(relname)
    if relpath.is_absolute() or ".." in relpath.parts:
        raise HTTPException(status_code=400, detail="Unsafe file path")
    return sample_dir / relpath


def effective_outputs(config: Config, sample_row, raw_sample: dict | None = None) -> dict:
    raw = raw_sample if raw_sample is not None else raw_json_for_sample(sample_row)
    return dict(raw.get("outputs", {}))


def output_links(outputs: dict, link_specs: list[tuple[str, str]]) -> list[dict]:
    links: list[dict] = []
    for label, key in link_specs:
        relname = outputs.get(key)
        if relname:
            links.append({"label": label, "key": key, "filename": relname})
    return links


def resolve_sample_results_dir(config: Config, sample_row) -> Path:
    root_name = sample_row["source_root_name"]
    root = config.get_root(root_name)
    if root is None:
        raise HTTPException(status_code=500, detail=f"Configured results root not found: {root_name}")
    return (root.linux_path / sample_row["sample_results_relpath"]).resolve()


def resolve_output_file(config: Config, sample_row, output_key: str) -> tuple[Path, str]:
    outputs = effective_outputs(config, sample_row)
    relname = outputs.get(output_key)
    if not relname:
        raise HTTPException(status_code=404, detail=f"Output not available: {output_key}")

    sample_dir = resolve_sample_results_dir(config, sample_row)
    candidate = safe_output_path(sample_dir, relname)
    if not candidate.exists():
        raise HTTPException(status_code=404, detail=f"Missing file on disk: {candidate}")
    return candidate, relname


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


def build_lims_export_rows(config: Config, sample_row: dict) -> list[str]:
    raw = raw_json_for_sample(sample_row)
    rows: list[str] = []
    lid = sample_row.get("lid") or raw.get("lid") or sample_row.get("sample_id") or ""
    subtype = (
        sample_row.get("typing_report_subtype")
        or raw.get("typing", {}).get("report_subtype")
        or ""
    )

    try:
        lims_file, _ = resolve_output_file(config, sample_row, "lid_2limsrs")
    except HTTPException:
        lims_file = None

    if lims_file is not None:
        for line in lims_file.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped == LIMS_EXPORT_HEADER:
                continue
            parts = normalize_lims_row(line).split("\t")
            if lid:
                parts[0] = str(lid)
            if parts[1] == "hcvtyp" and subtype:
                parts[2] = f"HCV genotyp {subtype}"
            rows.append("\t".join(parts))
    else:
        if lid and subtype:
            rows.append(normalize_lims_row(f"{lid}\thcvtyp\tHCV genotyp {subtype}\t"))

    rows.append(normalize_lims_row(f"{lid}\thcvqc\t{lims_qc_value(sample_row.get('qc_status'))}\t"))
    return rows


def build_lims_export_content(config: Config, sample_rows: list[dict]) -> str:
    lines = [LIMS_EXPORT_HEADER]
    for sample_row in sample_rows:
        lines.extend(build_lims_export_rows(config, sample_row))
    return "\n".join(lines) + "\n"


def load_sample_rows(config: Config, sample_run_ids: list[str]) -> list[dict]:
    connection = connect(config.database.path)
    try:
        sample_rows = []
        for item in sample_run_ids:
            sample_row = get_sample(connection, item)
            if sample_row is not None:
                sample_rows.append(sample_row)
    finally:
        connection.close()
    return sample_rows


def build_fasta_clipboard_content(config: Config, sample_rows: list[dict], output_key: str) -> str:
    chunks: list[str] = []
    for sample_row in sample_rows:
        file_path, _ = resolve_output_file(config, sample_row, output_key)
        text = file_path.read_text(encoding="utf-8")
        if text and not text.endswith("\n"):
            text = f"{text}\n"
        chunks.append(text)
    return "".join(chunks)


def lims_export_filename(sample_rows: list[dict]) -> str:
    if len(sample_rows) == 1:
        identifier = display_identifier(sample_rows[0]) or sample_rows[0].get("sample_id") or "sample"
        safe_identifier = str(identifier).replace("/", "_").replace(" ", "_")
        return f"{safe_identifier}-2limsrs.txt"
    return "virtitta-2limsrs.txt"


def write_server_lims_export(config: Config, sample_rows: list[dict], content: str) -> Path | None:
    if config.exports.lims_root is None:
        return None

    export_dir = config.exports.lims_root / datetime.now().date().isoformat()
    export_dir.mkdir(parents=True, exist_ok=True)

    base_name = lims_export_filename(sample_rows)
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
        relname = resolved_outputs.get(key)
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


def create_app(config_path: str | Path | None = None) -> FastAPI:
    config = load_config(config_path)
    from virtitta.importer import import_run as import_run_dir
    connection = connect(config.database.path)
    init_db(connection)
    connection.close()

    app = FastAPI(title=config.app.title)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    app.state.config = config

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
        selected_sample_categories = unique_strings(sample_category)
        selected_manual_groups = unique_strings(manual_group)
        min_coverage_value = parse_optional_float(min_coverage_pct)
        min_mean_depth_value = parse_optional_float(min_mean_depth)
        min_blast_identity_value = parse_optional_float(min_blast_identity)
        max_ct_value = parse_optional_float(max_ct)
        all_columns = table_columns(config)
        visible_column_set = set(config.ui.visible_columns)

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
                sort=sort,
                desc=desc,
            )
            runs = list_runs(connection)
            subtypes = list_subtypes(connection)
            stored_sample_categories = list_stored_sample_categories(connection)
            available_manual_groups = list_manual_groups(connection)
        finally:
            connection.close()

        available_sample_categories = configured_category_options(
            config,
            stored_sample_categories,
            selected_sample_categories,
        )

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
                "column_labels": {**DEFAULT_COLUMN_LABELS, **config.ui.column_labels},
                "cell_class": lambda column, value: cell_class(config, column, value),
                "cell_display_class": cell_display_class,
                "cell_style": cell_style,
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
                "warning_message": warning,
                "notice_message": notice,
                "qc_status_options": QC_STATUS_OPTIONS,
                "summary": {
                    "total": len(rows),
                    "pass": sum(1 for row in rows if row.get("qc_status") == "pass"),
                    "fail": sum(1 for row in rows if row.get("qc_status") == "fail"),
                    "unreviewed": sum(1 for row in rows if row.get("qc_status") == "unreviewed"),
                },
            },
        )

    @app.post("/samples/category")
    async def bulk_category_update(
        sample_run_id: list[str] = Form(default=[]),
        sample_category: str = Form(default=""),
        redirect_to: str = Form(default="/"),
    ):
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
        sample_run_id: list[str] = Form(default=[]),
        group_name: str = Form(default=""),
        redirect_to: str = Form(default="/"),
    ):
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
        sample_run_id: list[str] = Form(default=[]),
        group_name: str = Form(default=""),
        redirect_to: str = Form(default="/"),
    ):
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
        sample_run_id: list[str] = Form(default=[]),
        qc_status: str = Form(...),
        comment_body: str = Form(default=""),
        comment_author: str = Form(default=""),
        redirect_to: str = Form(default="/"),
    ):
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
            update_qc_status(connection, sample_run_id, qc_status)
            if comment_body.strip():
                for item in sample_run_id:
                    add_comment(connection, item, comment_body, comment_author or None)
        finally:
            connection.close()
        return RedirectResponse(redirect_to, status_code=303)

    @app.post("/samples/delete")
    async def bulk_delete_samples(
        sample_run_id: list[str] = Form(default=[]),
        redirect_to: str = Form(default="/"),
    ):
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
        sample_run_id: list[str] = Form(default=[]),
        redirect_to: str = Form(default="/"),
    ):
        if not sample_run_id:
            return RedirectResponse(redirect_to, status_code=303)

        sample_rows = load_sample_rows(config, sample_run_id)

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
        sample_run_id: list[str] = Form(default=[]),
        redirect_to: str = Form(default="/"),
    ):
        if not sample_run_id:
            return RedirectResponse(redirect_to, status_code=303)

        sample_rows = load_sample_rows(config, sample_run_id)

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
    async def bulk_fasta_clipboard_export(sample_run_id: list[str] = Form(default=[])):
        if not sample_run_id:
            raise HTTPException(status_code=400, detail="No samples selected")
        sample_rows = load_sample_rows(config, sample_run_id)
        if not sample_rows:
            raise HTTPException(status_code=404, detail="No matching samples found")
        return Response(
            content=build_fasta_clipboard_content(config, sample_rows, "export_fasta"),
            media_type="text/plain; charset=utf-8",
        )

    @app.post("/samples/clipboard/iupac-fasta")
    async def bulk_iupac_fasta_clipboard_export(sample_run_id: list[str] = Form(default=[])):
        if not sample_run_id:
            raise HTTPException(status_code=400, detail="No samples selected")
        sample_rows = load_sample_rows(config, sample_run_id)
        if not sample_rows:
            raise HTTPException(status_code=404, detail="No matching samples found")
        return Response(
            content=build_fasta_clipboard_content(config, sample_rows, "export_iupac_fasta"),
            media_type="text/plain; charset=utf-8",
        )

    @app.get("/samples/{sample_run_id}", response_class=HTMLResponse)
    def sample_detail(
        request: Request,
        sample_run_id: str,
        warning: str = Query(default=""),
        notice: str = Query(default=""),
    ):
        connection = connect(config.database.path)
        try:
            sample_row = get_sample(connection, sample_run_id)
            if sample_row is None:
                raise HTTPException(status_code=404, detail="Sample not found")
            comments = get_comments(connection, sample_run_id)
            raw = raw_json_for_sample(sample_row)
        finally:
            connection.close()

        outputs = effective_outputs(config, sample_row, raw)
        igv_url = None
        if config.features.igv and config.igv.enabled:
            try:
                igv_url = build_igv_url(config, sample_row, outputs)
            except HTTPException:
                igv_url = None

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
                "resistance_cells": resistance_cells,
                "resistance_mutations": resistance_mutations,
                "resistance_analysis_present": bool(resistance_summary.get("analysis_present")),
                "resistance_has_calls": bool(resistance_summary.get("has_resistance")),
                "warning_message": warning,
                "notice_message": notice,
                "qc_status_options": QC_STATUS_OPTIONS,
                "sample_categories": config.annotations.sample_categories,
                "refresh_run_name": sample_row["run_name"],
                "detail_links": output_links(outputs, DETAIL_FILE_LINKS),
                "igv_track_links": output_links(outputs, IGV_TRACK_LINKS),
            },
        )

    @app.post("/samples/{sample_run_id}/overrides")
    async def update_sample_overrides(
        sample_run_id: str,
        lid: str = Form(default=""),
        sequencing_date: str = Form(default=""),
        sample_metadata_ct: str = Form(default=""),
        sample_metadata_library_concentration_ng_ul: str = Form(default=""),
        typing_report_subtype: str = Form(default=""),
    ):
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
            if sample_row is None:
                raise HTTPException(status_code=404, detail="Sample not found")
            changes = set_sample_field_overrides(connection, sample_run_id, values)
            for change in changes:
                add_comment(connection, sample_run_id, override_comment_text(change), "Virtitta")
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
        sample_run_id: str,
        body: str = Form(...),
        author: str = Form(default=""),
    ):
        if not body.strip():
            return RedirectResponse(f"/samples/{sample_run_id}", status_code=303)
        connection = connect(config.database.path)
        try:
            add_comment(connection, sample_run_id, body, author or None)
        finally:
            connection.close()
        return RedirectResponse(request_url_without_messages(f"/samples/{sample_run_id}"), status_code=303)

    @app.post("/samples/{sample_run_id}/comments/{comment_id}/delete")
    async def remove_comment(sample_run_id: str, comment_id: int):
        connection = connect(config.database.path)
        try:
            sample_row = get_sample(connection, sample_run_id)
            if sample_row is None:
                raise HTTPException(status_code=404, detail="Sample not found")
            delete_comment(connection, sample_run_id, comment_id)
        finally:
            connection.close()
        return RedirectResponse(request_url_without_messages(f"/samples/{sample_run_id}#comments"), status_code=303)

    @app.post("/samples/{sample_run_id}/delete")
    async def delete_single_sample(sample_run_id: str):
        connection = connect(config.database.path)
        try:
            sample_row = get_sample(connection, sample_run_id)
            if sample_row is None:
                raise HTTPException(status_code=404, detail="Sample not found")
            run_name = sample_row["run_name"]
            delete_samples(connection, [sample_run_id])
        finally:
            connection.close()
        return RedirectResponse(request_url_without_messages(f"/?run_name={run_name}"), status_code=303)

    @app.get("/samples/{sample_run_id}/files/{output_key}")
    def sample_file(sample_run_id: str, output_key: str):
        connection = connect(config.database.path)
        try:
            sample_row = get_sample(connection, sample_run_id)
            if sample_row is None:
                raise HTTPException(status_code=404, detail="Sample not found")
        finally:
            connection.close()

        file_path, relname = resolve_output_file(config, sample_row, output_key)
        return FileResponse(file_path, filename=relname)

    @app.post("/runs/{run_name}/refresh")
    async def refresh_run_metadata(run_name: str, redirect_to: str = Form(default="/")):
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
        imported = import_run_dir(config, run_dir)
        return RedirectResponse(
            append_notice(
                request_url_without_messages(redirect_to),
                f"Refreshed run metadata from per-sample QC summaries ({imported} samples).",
            ),
            status_code=303,
        )

    @app.get("/samples/{sample_run_id}/lims-export")
    def sample_lims_export(sample_run_id: str):
        connection = connect(config.database.path)
        try:
            sample_row = get_sample(connection, sample_run_id)
            if sample_row is None:
                raise HTTPException(status_code=404, detail="Sample not found")
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
    def sample_lims_export_download(sample_run_id: str):
        connection = connect(config.database.path)
        try:
            sample_row = get_sample(connection, sample_run_id)
            if sample_row is None:
                raise HTTPException(status_code=404, detail="Sample not found")
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
    def sample_igv(sample_run_id: str):
        connection = connect(config.database.path)
        try:
            sample_row = get_sample(connection, sample_run_id)
            if sample_row is None:
                raise HTTPException(status_code=404, detail="Sample not found")
        finally:
            connection.close()

        return RedirectResponse(build_igv_url(config, sample_row), status_code=307)

    @app.get("/samples/{sample_run_id}/igv/mutations/{mutation_index}")
    def sample_igv_mutation(sample_run_id: str, mutation_index: int):
        connection = connect(config.database.path)
        try:
            sample_row = get_sample(connection, sample_run_id)
            if sample_row is None:
                raise HTTPException(status_code=404, detail="Sample not found")
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
