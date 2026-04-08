from __future__ import annotations

import json
from pathlib import Path, PureWindowsPath
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from fastapi import FastAPI, Form, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from virtitta.config import DEFAULT_COLUMN_LABELS, QC_STATUS_OPTIONS, Config, load_config
from virtitta.repository import (
    add_comment,
    connect,
    delete_comment,
    delete_samples,
    get_comments,
    get_sample,
    init_db,
    list_runs,
    list_subtypes,
    list_samples,
    raw_json_for_sample,
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


def format_value(value: object, column: str | None = None) -> str:
    if value is None:
        return ""
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


def bool_query_value(value: bool) -> str:
    return "true" if value else "false"


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


def output_links(raw_sample: dict, link_specs: list[tuple[str, str]]) -> list[dict]:
    outputs = raw_sample.get("outputs", {})
    return [
        {"label": label, "key": key, "filename": outputs.get(key)}
        for label, key in link_specs
        if outputs.get(key)
    ]


def resolve_sample_results_dir(config: Config, sample_row) -> Path:
    root_name = sample_row["source_root_name"]
    root = config.get_root(root_name)
    if root is None:
        raise HTTPException(status_code=500, detail=f"Configured results root not found: {root_name}")
    return (root.linux_path / sample_row["sample_results_relpath"]).resolve()


def resolve_output_file(config: Config, sample_row, output_key: str) -> tuple[Path, str]:
    raw = raw_json_for_sample(sample_row)
    outputs = raw.get("outputs", {})
    relname = outputs.get(output_key)
    if not relname:
        raise HTTPException(status_code=404, detail=f"Output not available: {output_key}")

    sample_dir = resolve_sample_results_dir(config, sample_row)
    candidate = (sample_dir / relname).resolve()
    if sample_dir not in candidate.parents and candidate != sample_dir:
        raise HTTPException(status_code=400, detail="Unsafe file path")
    if not candidate.exists():
        raise HTTPException(status_code=404, detail=f"Missing file on disk: {candidate}")
    return candidate, relname


def windows_path_to_igv_path(path_str: str) -> str:
    path_str = path_str.replace("\\", "/")
    if len(path_str) >= 3 and path_str[1:3] == ":/":
        return f"/{path_str[:2]}{path_str[3:]}"
    return path_str


def lims_qc_value(qc_status: str | None) -> str:
    return "passed" if qc_status == "pass" else "failed"


def normalize_lims_row(line: str) -> str:
    parts = line.rstrip("\n\r").split("\t")
    if len(parts) < 4:
        parts.extend([""] * (4 - len(parts)))
    return "\t".join(parts[:4])


def build_lims_export_rows(config: Config, sample_row: dict) -> list[str]:
    raw = raw_json_for_sample(sample_row)
    rows: list[str] = []

    try:
        lims_file, _ = resolve_output_file(config, sample_row, "lid_2limsrs")
    except HTTPException:
        lims_file = None

    if lims_file is not None:
        for line in lims_file.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped == LIMS_EXPORT_HEADER:
                continue
            rows.append(normalize_lims_row(line))
    else:
        lid = raw.get("lid") or sample_row.get("lid") or sample_row.get("sample_id") or ""
        subtype = (
            raw.get("typing", {}).get("report_subtype")
            or sample_row.get("typing_report_subtype")
            or ""
        )
        if lid and subtype:
            rows.append(normalize_lims_row(f"{lid}\thcvtyp\tHCV genotyp {subtype}\t"))

    lid = raw.get("lid") or sample_row.get("lid") or sample_row.get("sample_id") or ""
    rows.append(normalize_lims_row(f"{lid}\thcvqc\t{lims_qc_value(sample_row.get('qc_status'))}\t"))
    return rows


def build_lims_export_content(config: Config, sample_rows: list[dict]) -> str:
    lines = [LIMS_EXPORT_HEADER]
    for sample_row in sample_rows:
        lines.extend(build_lims_export_rows(config, sample_row))
    return "\n".join(lines) + "\n"


def lims_export_filename(sample_rows: list[dict]) -> str:
    if len(sample_rows) == 1:
        identifier = display_identifier(sample_rows[0]) or sample_rows[0].get("sample_id") or "sample"
        safe_identifier = str(identifier).replace("/", "_").replace(" ", "_")
        return f"{safe_identifier}-2limsrs.txt"
    return "virtitta-2limsrs.txt"


def append_warning(url: str, message: str) -> str:
    parts = urlsplit(url)
    query_items = parse_qsl(parts.query, keep_blank_values=True)
    query_items = [(key, value) for key, value in query_items if key != "warning"]
    query_items.append(("warning", message))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query_items), parts.fragment))


def request_url_without_warning(url: str) -> str:
    parts = urlsplit(url)
    query_items = [(key, value) for key, value in parse_qsl(parts.query, keep_blank_values=True) if key != "warning"]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query_items), parts.fragment))


def build_igv_url(config: Config, sample_row) -> str:
    if not config.features.igv or not config.igv.enabled:
        raise HTTPException(status_code=404, detail="IGV integration is disabled")

    raw = raw_json_for_sample(sample_row)
    outputs = raw.get("outputs", {})
    root = config.get_root(sample_row["source_root_name"])
    if root is None:
        raise HTTPException(status_code=500, detail="Missing results root mapping")

    sample_rel = Path(sample_row["sample_results_relpath"])
    sample_windows_root = PureWindowsPath(root.windows_path)

    def convert_output(key: str) -> str | None:
        relname = outputs.get(key)
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


def create_app(config_path: str | Path | None = None) -> FastAPI:
    config = load_config(config_path)
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
        warning: str = Query(default=""),
        min_coverage_pct: str = Query(default=""),
        min_mean_depth: str = Query(default=""),
        min_blast_identity: str = Query(default=""),
        max_ct: str = Query(default=""),
        sort: str = Query(default=config.ui.default_sort),
        desc: bool = Query(default=config.ui.default_sort_desc),
    ):
        min_coverage_value = parse_optional_float(min_coverage_pct)
        min_mean_depth_value = parse_optional_float(min_mean_depth)
        min_blast_identity_value = parse_optional_float(min_blast_identity)
        max_ct_value = parse_optional_float(max_ct)

        connection = connect(config.database.path)
        try:
            rows = list_samples(
                connection,
                search=search,
                run_name=run_name,
                subtype=subtype,
                qc_status=qc_status,
                min_coverage_pct=min_coverage_value,
                min_mean_depth=min_mean_depth_value,
                min_blast_identity=min_blast_identity_value,
                max_ct=max_ct_value,
                sort=sort,
                desc=desc,
            )
            runs = list_runs(connection)
            subtypes = list_subtypes(connection)
        finally:
            connection.close()

        return templates.TemplateResponse(
            request,
            "index.html",
            {
                "request": request,
                "config": config,
                "rows": rows,
                "runs": runs,
                "subtypes": subtypes,
                "visible_columns": config.ui.visible_columns,
                "column_labels": {**DEFAULT_COLUMN_LABELS, **config.ui.column_labels},
                "cell_class": lambda column, value: cell_class(config, column, value),
                "cell_display_class": cell_display_class,
                "cell_style": cell_style,
                "row_class": row_class,
                "format_value": format_value,
                "display_identifier": display_identifier,
                "bool_query_value": bool_query_value,
                "sort": sort,
                "desc": desc,
                "search": search,
                "selected_run_name": run_name,
                "selected_subtype": subtype,
                "selected_qc_status": qc_status,
                "min_coverage_pct": min_coverage_pct,
                "min_mean_depth": min_mean_depth,
                "min_blast_identity": min_blast_identity,
                "max_ct": max_ct,
                "warning_message": warning,
                "qc_status_options": QC_STATUS_OPTIONS,
                "summary": {
                    "total": len(rows),
                    "pass": sum(1 for row in rows if row.get("qc_status") == "pass"),
                    "fail": sum(1 for row in rows if row.get("qc_status") == "fail"),
                    "unreviewed": sum(1 for row in rows if row.get("qc_status") == "unreviewed"),
                },
            },
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
        return RedirectResponse(request_url_without_warning(redirect_to), status_code=303)

    @app.post("/samples/lims-export")
    async def bulk_lims_export(
        sample_run_id: list[str] = Form(default=[]),
        redirect_to: str = Form(default="/"),
    ):
        if not sample_run_id:
            return RedirectResponse(redirect_to, status_code=303)

        connection = connect(config.database.path)
        try:
            sample_rows = []
            for item in sample_run_id:
                sample_row = get_sample(connection, item)
                if sample_row is not None:
                    sample_rows.append(sample_row)
        finally:
            connection.close()

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

    @app.get("/samples/{sample_run_id}", response_class=HTMLResponse)
    def sample_detail(request: Request, sample_run_id: str, warning: str = Query(default="")):
        connection = connect(config.database.path)
        try:
            sample_row = get_sample(connection, sample_run_id)
            if sample_row is None:
                raise HTTPException(status_code=404, detail="Sample not found")
            comments = get_comments(connection, sample_run_id)
            raw = raw_json_for_sample(sample_row)
        finally:
            connection.close()

        igv_url = None
        if config.features.igv and config.igv.enabled:
            try:
                igv_url = build_igv_url(config, sample_row)
            except HTTPException:
                igv_url = None

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
                "warning_message": warning,
                "qc_status_options": QC_STATUS_OPTIONS,
                "detail_links": output_links(raw, DETAIL_FILE_LINKS),
                "igv_track_links": output_links(raw, IGV_TRACK_LINKS),
            },
        )

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
        return RedirectResponse(f"/samples/{sample_run_id}", status_code=303)

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
        return RedirectResponse(f"/samples/{sample_run_id}#comments", status_code=303)

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
        return RedirectResponse(f"/?run_name={run_name}", status_code=303)

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

    return app
