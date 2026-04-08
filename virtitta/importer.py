from __future__ import annotations

import json
from pathlib import Path

from virtitta.config import Config, ResultsRoot
from virtitta.repository import connect, init_db, upsert_run, upsert_sample, utc_now


def _find_matching_root(path: Path, roots: list[ResultsRoot]) -> tuple[ResultsRoot, Path]:
    resolved = path.resolve()
    matches: list[tuple[int, ResultsRoot, Path]] = []
    for root in roots:
        root_path = root.linux_path.resolve()
        try:
            relpath = resolved.relative_to(root_path)
        except ValueError:
            continue
        matches.append((len(root_path.parts), root, relpath))

    if not matches:
        raise ValueError(f"Run directory is not under any configured results root: {path}")

    _, root, relpath = sorted(matches, key=lambda item: item[0], reverse=True)[0]
    return root, relpath


def _extract_date_portion(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if len(text) >= 10 and text[4] == "-" and text[7] == "-":
        return text[:10]
    return None


def _flatten_sample_record(sample: dict, *, root_name: str, sample_results_relpath: Path) -> dict:
    qc = sample.get("qc", {})
    coverage = qc.get("coverage_thresholds_pct", {})
    typing = sample.get("typing", {})
    host_filter = sample.get("host_filter", {})
    sample_metadata = sample.get("sample_metadata", {})

    return {
        "sample_run_id": sample["sample_run_id"],
        "run_name": sample["run_name"],
        "generated_date": _extract_date_portion(sample.get("generated_at_utc")),
        "sample_id": sample["sample_id"],
        "lid": sample.get("lid"),
        "source_root_name": root_name,
        "sample_results_relpath": sample_results_relpath.as_posix(),
        "typing_report_subtype": typing.get("report_subtype"),
        "typing_main_blast_identity": typing.get("main_blast_identity"),
        "host_filter_reads_in": host_filter.get("reads_in"),
        "host_filter_reads_removed_proportion": host_filter.get("reads_removed_proportion"),
        "qc_coverage_pct": qc.get("coverage_pct"),
        "qc_mean_depth": qc.get("mean_depth"),
        "qc_coverage_1x_pct": coverage.get("1x"),
        "qc_coverage_10x_pct": coverage.get("10x"),
        "qc_coverage_100x_pct": coverage.get("100x"),
        "qc_coverage_1000x_pct": coverage.get("1000x"),
        "sample_metadata_ct": sample_metadata.get("ct"),
        "sample_metadata_library_concentration_ng_ul": sample_metadata.get("library_concentration_ng_ul"),
        "sample_metadata_library_fragment_length_bp": sample_metadata.get("library_fragment_length_bp"),
        "raw_json": json.dumps(sample, sort_keys=True),
        "imported_at": utc_now(),
    }


def import_run(config: Config, run_dir: Path) -> int:
    run_dir = run_dir.resolve()
    qc_summary_path = run_dir / "pipeline_info" / "qc_summary.json"
    if not qc_summary_path.exists():
        raise FileNotFoundError(f"Missing run summary file: {qc_summary_path}")

    root, run_relpath = _find_matching_root(run_dir, config.results_roots)
    records = json.loads(qc_summary_path.read_text(encoding="utf-8"))
    if not isinstance(records, list):
        raise ValueError(f"Expected a JSON array in {qc_summary_path}")

    connection = connect(config.database.path)
    try:
        init_db(connection)
        if not records:
            return 0

        first = records[0]
        upsert_run(
            connection,
            {
                "run_name": first["run_name"],
                "sample_count": len(records),
                "pipeline_name": first.get("pipeline_name"),
                "virus": first.get("virus"),
                "source_root_name": root.name,
                "run_relpath": run_relpath.as_posix(),
                "imported_at": utc_now(),
            },
        )

        imported = 0
        for sample in records:
            sample_results_relpath = run_relpath / sample["sample_id"] / "results"
            upsert_sample(
                connection,
                _flatten_sample_record(
                    sample,
                    root_name=root.name,
                    sample_results_relpath=sample_results_relpath,
                ),
            )
            imported += 1

        connection.commit()
        return imported
    finally:
        connection.close()


def import_all_roots(config: Config) -> int:
    total = 0
    for root in config.results_roots:
        if not root.linux_path.exists():
            continue
        for run_dir in sorted(path for path in root.linux_path.iterdir() if path.is_dir()):
            qc_summary_path = run_dir / "pipeline_info" / "qc_summary.json"
            if qc_summary_path.exists():
                total += import_run(config, run_dir)
    return total
