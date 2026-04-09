from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path


DEFAULT_VISIBLE_COLUMNS = [
    "lid",
    "sample_id",
    "generated_date",
    "qc_status",
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
    "qc_coverage_1000x_pct",
    "sample_metadata_ct",
    "sample_metadata_library_concentration_ng_ul",
    "sample_metadata_library_fragment_length_bp",
    "run_name",
]

DEFAULT_COLUMN_LABELS = {
    "generated_date": "Date",
    "sample_id": "Sample ID",
    "lid": "LID",
    "qc_status": "QC Status",
    "typing_report_subtype": "Subtype",
    "typing_main_blast_identity": "BLAST %",
    "resistance_summary": "Resistance",
    "host_filter_reads_in": "Reads In",
    "host_filter_reads_removed_proportion": "Human",
    "qc_coverage_pct": "Coverage %",
    "qc_mean_depth": "Mean Depth",
    "qc_coverage_1x_pct": "1x %",
    "qc_coverage_10x_pct": "10x %",
    "qc_coverage_100x_pct": "100x %",
    "qc_coverage_1000x_pct": "1000x %",
    "sample_metadata_ct": "CT",
    "sample_metadata_library_concentration_ng_ul": "Lib Conc",
    "sample_metadata_library_fragment_length_bp": "Fragment BP",
    "run_name": "Run",
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
class ExportSettings:
    lims_root: Path | None = None


@dataclass(frozen=True)
class UiSettings:
    visible_columns: list[str] = field(default_factory=lambda: list(DEFAULT_VISIBLE_COLUMNS))
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
    exports: ExportSettings
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
    exports_raw = raw.get("exports", {})
    ui_raw = raw.get("ui", {})
    root_entries = raw.get("results_roots", [])

    visible_columns = [str(column) for column in ui_raw.get("visible_columns", DEFAULT_VISIBLE_COLUMNS)]

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
        exports=ExportSettings(
            lims_root=(
                (base_dir / exports_raw["lims_root"]).resolve()
                if exports_raw.get("lims_root") and not Path(exports_raw["lims_root"]).is_absolute()
                else Path(exports_raw["lims_root"]).resolve()
            )
            if exports_raw.get("lims_root")
            else None
        ),
        ui=UiSettings(
            visible_columns=visible_columns,
            default_sort=str(ui_raw.get("default_sort", "run_name")),
            default_sort_desc=bool(ui_raw.get("default_sort_desc", True)),
            column_labels=_merge_labels(ui_raw.get("column_labels")),
            highlight_rules=_merge_highlight_rules(ui_raw.get("highlight_rules")),
        ),
        results_roots=roots,
    )
