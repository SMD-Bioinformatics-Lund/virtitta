from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from virtitta.artifact_cache import cache_sample_outputs
from virtitta.config import Config, ResultsRoot
from virtitta.repository import connect, init_db, sync_run_sample_count, upsert_run, upsert_sample, utc_now


MANUAL_FAILED_RUN_NAME = "manual_failed_samples"


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


def _sequencing_date_from_run_name(run_name: object, fallback_date: str | None) -> str | None:
    text = str(run_name or "")
    prefix = text[:6]
    if len(prefix) == 6 and prefix.isdigit():
        try:
            return datetime.strptime(prefix, "%y%m%d").date().isoformat()
        except ValueError:
            pass
    return fallback_date


def _maybe_float(value: object) -> float | None:
    if value in (None, ""):
        return None
    return float(value)


def _maybe_int(value: object) -> int | None:
    if value in (None, ""):
        return None
    return int(float(value))


def _load_clarity_sample_info(sample_info_path: Path | None) -> dict[str, dict[str, object]]:
    if sample_info_path is None:
        return {}

    resolved = sample_info_path.resolve()
    if not resolved.exists():
        raise FileNotFoundError(f"Missing clarity_sample_info.json: {resolved}")

    payload = json.loads(resolved.read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        entries = list(payload.values())
    elif isinstance(payload, list):
        entries = payload
    else:
        raise ValueError(f"Expected a JSON object or list in {resolved}")

    sample_info_by_id: dict[str, dict[str, object]] = {}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        sample_id = str(entry.get("clarity_sample_id", "")).strip()
        if not sample_id:
            continue
        sample_info_by_id[sample_id] = {
            "ct": _maybe_float(entry.get("CT")),
            "library_concentration_ng_ul": _maybe_float(entry.get("Library concentration (ng/ul)")),
            "library_fragment_length_bp": _maybe_int(entry.get("Library fragment length (bp)")),
        }

    return sample_info_by_id


def _merge_clarity_sample_metadata(sample: dict, sample_info_by_id: dict[str, dict[str, object]]) -> dict:
    sample_id = str(sample.get("sample_id", "")).strip()
    clarity_sample_info = sample_info_by_id.get(sample_id)
    if not clarity_sample_info:
        return sample

    existing_metadata = sample.get("sample_metadata", {})
    if not isinstance(existing_metadata, dict):
        existing_metadata = {}

    merged_metadata = dict(existing_metadata)
    changed = False
    for key, value in clarity_sample_info.items():
        if merged_metadata.get(key) in (None, "") and value is not None:
            merged_metadata[key] = value
            changed = True

    if not changed:
        return sample

    merged_sample = dict(sample)
    merged_sample["sample_metadata"] = merged_metadata
    return merged_sample


def _flatten_sample_record(sample: dict, *, root_name: str, sample_results_relpath: Path) -> dict:
    qc = sample.get("qc", {})
    coverage = qc.get("coverage_thresholds_pct", {})
    typing = sample.get("typing", {})
    host_filter = sample.get("host_filter", {})
    sample_metadata = sample.get("sample_metadata", {})
    af_counts = sample.get("variants", {}).get("af_counts", {})
    imported_at = utc_now()
    generated_date = _extract_date_portion(sample.get("generated_at_utc")) or _extract_date_portion(imported_at)

    return {
        "sample_run_id": sample["sample_run_id"],
        "run_name": sample["run_name"],
        "generated_date": generated_date,
        "sequencing_date": _sequencing_date_from_run_name(sample["run_name"], generated_date),
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
        "variant_af_count_005": af_counts.get("0.05"),
        "variant_af_count_01": af_counts.get("0.1"),
        "variant_af_count_015": af_counts.get("0.15"),
        "variant_af_count_02": af_counts.get("0.2"),
        "variant_af_count_03": af_counts.get("0.3"),
        "variant_af_count_04": af_counts.get("0.4"),
        "sample_metadata_ct": sample_metadata.get("ct"),
        "sample_metadata_library_concentration_ng_ul": sample_metadata.get("library_concentration_ng_ul"),
        "sample_metadata_library_fragment_length_bp": sample_metadata.get("library_fragment_length_bp"),
        "raw_json": json.dumps(sample, sort_keys=True),
        "imported_at": imported_at,
    }


def _sample_qc_summary_paths(run_dir: Path) -> list[Path]:
    paths: list[Path] = []
    for sample_dir in sorted(path for path in run_dir.iterdir() if path.is_dir()):
        results_dir = sample_dir / "results"
        if not results_dir.is_dir():
            continue
        qc_summary_path = results_dir / f"{sample_dir.name}_qc_summary.json"
        if qc_summary_path.is_file():
            paths.append(qc_summary_path)
    return paths


def _load_sample_summary(qc_summary_path: Path) -> dict:
    payload = json.loads(qc_summary_path.read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, list) and len(payload) == 1 and isinstance(payload[0], dict):
        return payload[0]
    raise ValueError(f"Expected a JSON object in {qc_summary_path}")


def _manual_failed_sample_summary(
    *,
    run_name: str,
    sample_id: str,
    lid: str,
    generated_at_utc: str,
    sample_metadata: dict[str, object] | None = None,
) -> dict:
    return {
        "schema_version": 4,
        "pipeline_name": "virpipa",
        "generated_at_utc": generated_at_utc,
        "run_name": run_name,
        "sample_id": sample_id,
        "sample_run_id": f"{sample_id}_{run_name}",
        "lid": lid,
        "sample_metadata": dict(sample_metadata or {}),
        "host_filter": {},
        "qc": {},
        "typing": {},
        "outputs": {},
        "resistance": {
            "analysis_present": False,
            "has_resistance": False,
            "mutation_count": 0,
            "by_drug": [],
            "mutations": [],
        },
        "analysis_status": "failed",
        "analysis_note": "Manually imported failed sample without virpipa QC summary output.",
    }


def import_run(config: Config, run_dir: Path, clarity_sample_info_path: Path | None = None) -> int:
    run_dir = run_dir.resolve()
    qc_summary_paths = _sample_qc_summary_paths(run_dir)
    if not qc_summary_paths:
        raise FileNotFoundError(f"Missing per-sample QC summary files under: {run_dir}")

    root, run_relpath = _find_matching_root(run_dir, config.results_roots)
    sample_info_by_id = _load_clarity_sample_info(clarity_sample_info_path)
    records = [
        (_merge_clarity_sample_metadata(_load_sample_summary(path), sample_info_by_id), path)
        for path in qc_summary_paths
    ]

    connection = connect(config.database.path)
    try:
        init_db(connection)
        if not records:
            return 0

        first, _ = records[0]
        run_name = first.get("run_name") or run_dir.name
        upsert_run(
            connection,
            {
                "run_name": run_name,
                "sample_count": len(records),
                "pipeline_name": first.get("pipeline_name"),
                "virus": first.get("virus"),
                "source_root_name": root.name,
                "run_relpath": run_relpath.as_posix(),
                "imported_at": utc_now(),
            },
        )

        imported = 0
        root_path = root.linux_path.resolve()
        for sample, qc_summary_path in records:
            sample_results_relpath = qc_summary_path.parent.resolve().relative_to(root_path)
            sample_record = _flatten_sample_record(
                sample,
                root_name=root.name,
                sample_results_relpath=sample_results_relpath,
            )
            upsert_sample(
                connection,
                sample_record,
            )
            cache_sample_outputs(config, connection, sample_record)
            imported += 1

        sync_run_sample_count(connection, run_name)
        connection.commit()
        return imported
    finally:
        connection.close()


def import_sample(
    config: Config,
    sample_id: str,
    lid: str,
    *,
    run_dir: Path | None = None,
    clarity_sample_info_path: Path | None = None,
    ct: float | None = None,
    library_concentration_ng_ul: float | None = None,
) -> str:
    sample_id = sample_id.strip()
    lid = lid.strip()
    if not sample_id:
        raise ValueError("sample_id is required")
    if not lid:
        raise ValueError("lid is required")

    if run_dir is not None:
        run_dir = run_dir.resolve()
        root, run_relpath = _find_matching_root(run_dir, config.results_roots)
        run_name = run_dir.name
    else:
        root = config.results_roots[0] if config.results_roots else None
        run_relpath = Path(MANUAL_FAILED_RUN_NAME)
        run_name = MANUAL_FAILED_RUN_NAME

    sample_metadata = {}
    if ct is not None:
        sample_metadata["ct"] = ct
    if library_concentration_ng_ul is not None:
        sample_metadata["library_concentration_ng_ul"] = library_concentration_ng_ul

    sample_info_by_id = _load_clarity_sample_info(clarity_sample_info_path)
    sample = _merge_clarity_sample_metadata(
        _manual_failed_sample_summary(
            run_name=run_name,
            sample_id=sample_id,
            lid=lid,
            generated_at_utc=utc_now(),
            sample_metadata=sample_metadata,
        ),
        sample_info_by_id,
    )

    connection = connect(config.database.path)
    try:
        init_db(connection)
        upsert_run(
            connection,
            {
                "run_name": run_name,
                "sample_count": 0,
                "pipeline_name": sample.get("pipeline_name"),
                "virus": sample.get("virus"),
                "source_root_name": root.name if root is not None else None,
                "run_relpath": run_relpath.as_posix(),
                "imported_at": utc_now(),
            },
        )
        upsert_sample(
            connection,
            _flatten_sample_record(
                sample,
                root_name=root.name if root is not None else "",
                sample_results_relpath=run_relpath / sample_id / "results",
            ),
        )
        sync_run_sample_count(connection, run_name)
        connection.commit()
        return sample["sample_run_id"]
    finally:
        connection.close()


def import_all_roots(config: Config) -> int:
    total = 0
    for root in config.results_roots:
        if not root.linux_path.exists():
            continue
        for run_dir in sorted(path for path in root.linux_path.iterdir() if path.is_dir()):
            if _sample_qc_summary_paths(run_dir):
                total += import_run(config, run_dir)
    return total
