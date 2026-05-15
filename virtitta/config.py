from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


DEFAULT_VISIBLE_COLUMNS = [
    "lid",
    "sample_id",
    "sequencing_date",
    "generated_date",
    "sample_category",
    "qc_status",
    "manual_groups",
    "typing_report_subtype",
    "typing_main_blast_identity",
    "resistance_summary",
    "host_filter_reads_in",
    "host_filter_reads_removed_proportion",
    "qc_coverage_pct",
    "qc_mean_depth",
    "qc_coverage_1x_pct",
    "qc_coverage_10x_pct",
    "qc_coverage_100x_pct",
    "sample_metadata_ct",
    "sample_metadata_library_concentration_ng_ul",
    "sample_metadata_library_fragment_length_bp",
    "run_name",
]

OPTIONAL_TABLE_COLUMNS = [
    "sequencing_date",
    "manual_groups",
    "qc_coverage_1000x_pct",
    "variant_af_count_005",
    "variant_af_count_01",
    "variant_af_count_015",
    "variant_af_count_02",
    "variant_af_count_03",
    "variant_af_count_04",
]

DEFAULT_TABLE_COLUMNS = DEFAULT_VISIBLE_COLUMNS + [
    column for column in OPTIONAL_TABLE_COLUMNS if column not in DEFAULT_VISIBLE_COLUMNS
]

DEFAULT_COLUMN_LABELS = {
    "sequencing_date": "Date",
    "generated_date": "Import Date",
    "sample_id": "Sample ID",
    "lid": "LID",
    "qc_status": "QC",
    "sample_category": "Cat",
    "typing_report_subtype": "Subtype",
    "typing_main_blast_identity": "BLAST %",
    "resistance_summary": "Resistance",
    "host_filter_reads_in": "Reads In",
    "host_filter_reads_removed_proportion": "Human",
    "qc_coverage_pct": "Cov %",
    "qc_mean_depth": "Depth",
    "qc_coverage_1x_pct": "1x %",
    "qc_coverage_10x_pct": "10x %",
    "qc_coverage_100x_pct": "100x %",
    "qc_coverage_1000x_pct": "1000x %",
    "variant_af_count_005": "af 0.05",
    "variant_af_count_01": "af 0.1",
    "variant_af_count_015": "af 0.15",
    "variant_af_count_02": "af 0.2",
    "variant_af_count_03": "af 0.3",
    "variant_af_count_04": "af 0.4",
    "sample_metadata_ct": "CT",
    "sample_metadata_library_concentration_ng_ul": "Lib Conc",
    "sample_metadata_library_fragment_length_bp": "Frag bp",
    "run_name": "Run",
    "manual_groups": "Groups",
    "comment_count": "Comments",
}

DEFAULT_HIGHLIGHT_RULES = {
    "host_filter_reads_removed_proportion": {"warn_over": 0.01, "danger_over": 0.05},
    "qc_coverage_pct": {"warn_under": 95, "danger_under": 90},
    "qc_mean_depth": {"warn_under": 100, "danger_under": 30},
    "typing_main_blast_identity": {"warn_under": 90, "danger_under": 85},
    "sample_metadata_ct": {"warn_over": 32, "danger_over": 36},
}

QC_STATUS_OPTIONS = ["unreviewed", "pass", "fail"]
DEFAULT_CACHE_OUTPUT_KEYS = ["export_fasta", "export_iupac_fasta", "display_rug_kde_plot"]


@dataclass(frozen=True)
class AppSettings:
    title: str = "Virtitta"
    host: str = "127.0.0.1"
    port: int = 8000


@dataclass(frozen=True)
class DatabaseSettings:
    path: Path


@dataclass(frozen=True)
class IgvSettings:
    enabled: bool = True
    base_url: str = "http://localhost:60151/load"


@dataclass(frozen=True)
class FeatureSettings:
    comments: bool = True
    bulk_qc: bool = True
    igv: bool = True


@dataclass(frozen=True)
class AnnotationSettings:
    sample_categories: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ExportSettings:
    lims_root: Path | None = None


@dataclass(frozen=True)
class CacheSettings:
    outputs_root: Path
    output_keys: list[str] = field(default_factory=lambda: list(DEFAULT_CACHE_OUTPUT_KEYS))


@dataclass(frozen=True)
class UiSettings:
    table_columns: list[str] = field(default_factory=lambda: list(DEFAULT_TABLE_COLUMNS))
    visible_columns: list[str] = field(default_factory=lambda: list(DEFAULT_VISIBLE_COLUMNS))
    column_max_widths: dict[str, str] = field(default_factory=dict)
    default_sort: str = "run_name"
    default_sort_desc: bool = True
    column_labels: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_COLUMN_LABELS))
    highlight_rules: dict[str, dict[str, float]] = field(default_factory=lambda: dict(DEFAULT_HIGHLIGHT_RULES))


@dataclass(frozen=True)
class ResultsRoot:
    name: str
    linux_path: Path
    windows_path: str


@dataclass(frozen=True)
class Config:
    config_path: Path
    app: AppSettings
    database: DatabaseSettings
    igv: IgvSettings
    features: FeatureSettings
    annotations: AnnotationSettings
    exports: ExportSettings
    cache: CacheSettings
    ui: UiSettings
    results_roots: list[ResultsRoot]

    def get_root(self, name: str) -> ResultsRoot | None:
        for root in self.results_roots:
            if root.name == name:
                return root
        return None


def _merge_labels(user_labels: dict[str, str] | None) -> dict[str, str]:
    labels = dict(DEFAULT_COLUMN_LABELS)
    if user_labels:
        labels.update({str(key): str(value) for key, value in user_labels.items()})
    return labels


def _merge_highlight_rules(user_rules: dict | None) -> dict[str, dict[str, float]]:
    rules = {key: dict(value) for key, value in DEFAULT_HIGHLIGHT_RULES.items()}
    if user_rules:
        for key, value in user_rules.items():
            rules[str(key)] = {str(rule_key): float(rule_value) for rule_key, rule_value in value.items()}
    return rules


def _normalize_string_list(raw_values: object) -> list[str]:
    if not isinstance(raw_values, list):
        return []

    normalized: list[str] = []
    seen: set[str] = set()
    for value in raw_values:
        item = str(value).strip()
        if not item or item in seen:
            continue
        seen.add(item)
        normalized.append(item)
    return normalized


def _configured_table_columns(ui_raw: dict, visible_columns: list[str]) -> list[str]:
    if "table_columns" in ui_raw:
        return _normalize_string_list(ui_raw.get("table_columns"))

    table_columns = list(visible_columns)
    for column in OPTIONAL_TABLE_COLUMNS:
        if column not in table_columns:
            table_columns.append(column)
    return table_columns


def _normalize_string_map(raw_values: object) -> dict[str, str]:
    if not isinstance(raw_values, dict):
        return {}
    return {
        str(key).strip(): str(value).strip()
        for key, value in raw_values.items()
        if str(key).strip() and str(value).strip()
    }


def load_config(config_path: str | Path | None = None) -> Config:
    path = Path(config_path or os.environ.get("VIRTITTA_CONFIG", "virtitta.toml"))
    if not path.exists():
        example_path = Path("virtitta.example.toml")
        if example_path.exists():
            path = example_path
        else:
            raise FileNotFoundError(f"Config file not found: {path}")

    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    base_dir = path.resolve().parent

    app_raw = raw.get("app", {})
    db_raw = raw.get("database", {})
    igv_raw = raw.get("igv", {})
    features_raw = raw.get("features", {})
    annotations_raw = raw.get("annotations", {})
    exports_raw = raw.get("exports", {})
    cache_raw = raw.get("cache", {})
    ui_raw = raw.get("ui", {})
    root_entries = raw.get("results_roots", [])

    visible_columns = _normalize_string_list(ui_raw.get("visible_columns", DEFAULT_VISIBLE_COLUMNS))
    table_columns = _configured_table_columns(ui_raw, visible_columns)

    roots = [
        ResultsRoot(
            name=str(entry["name"]),
            linux_path=(base_dir / entry["linux_path"]).resolve() if not Path(entry["linux_path"]).is_absolute() else Path(entry["linux_path"]),
            windows_path=str(entry["windows_path"]),
        )
        for entry in root_entries
    ]

    return Config(
        config_path=path.resolve(),
        app=AppSettings(
            title=str(app_raw.get("title", "Virtitta")),
            host=str(app_raw.get("host", "127.0.0.1")),
            port=int(app_raw.get("port", 8000)),
        ),
        database=DatabaseSettings(
            path=(base_dir / db_raw.get("path", "data/virtitta.sqlite3")).resolve()
            if not Path(db_raw.get("path", "data/virtitta.sqlite3")).is_absolute()
            else Path(db_raw.get("path", "data/virtitta.sqlite3")),
        ),
        igv=IgvSettings(
            enabled=bool(igv_raw.get("enabled", True)),
            base_url=str(igv_raw.get("base_url", "http://localhost:60151/load")),
        ),
        features=FeatureSettings(
            comments=bool(features_raw.get("comments", True)),
            bulk_qc=bool(features_raw.get("bulk_qc", True)),
            igv=bool(features_raw.get("igv", True)),
        ),
        annotations=AnnotationSettings(
            sample_categories=_normalize_string_list(annotations_raw.get("sample_categories", [])),
        ),
        exports=ExportSettings(
            lims_root=(
                (base_dir / exports_raw["lims_root"]).resolve()
                if exports_raw.get("lims_root") and not Path(exports_raw["lims_root"]).is_absolute()
                else Path(exports_raw["lims_root"]).resolve()
            )
            if exports_raw.get("lims_root")
            else None
        ),
        cache=CacheSettings(
            outputs_root=(base_dir / cache_raw.get("outputs_root", "data/output_cache")).resolve()
            if not Path(cache_raw.get("outputs_root", "data/output_cache")).is_absolute()
            else Path(cache_raw.get("outputs_root", "data/output_cache")),
            output_keys=_normalize_string_list(cache_raw.get("output_keys", DEFAULT_CACHE_OUTPUT_KEYS)),
        ),
        ui=UiSettings(
            table_columns=table_columns,
            visible_columns=visible_columns,
            column_max_widths=_normalize_string_map(ui_raw.get("column_max_widths")),
            default_sort=str(ui_raw.get("default_sort", "run_name")),
            default_sort_desc=bool(ui_raw.get("default_sort_desc", True)),
            column_labels=_merge_labels(ui_raw.get("column_labels")),
            highlight_rules=_merge_highlight_rules(ui_raw.get("highlight_rules")),
        ),
        results_roots=roots,
    )
