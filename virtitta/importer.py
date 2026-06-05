from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from virtitta.artifact_cache import cache_sample_outputs
from virtitta.config import Config, ResultsRoot
from virtitta.outputs import required_sidecars, safe_relative_path
from virtitta.repository import connect, init_db, sync_run_sample_count, upsert_run, upsert_sample, utc_now


MANUAL_FAILED_RUN_NAME = "manual_failed_samples"


@dataclass(frozen=True)
class ImportReport:
    imported: int
    warnings: list[str]


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
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _maybe_int(value: object) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _first_present(mapping: dict, keys: tuple[str, ...]) -> object:
    for key in keys:
        value = mapping.get(key)
        if value not in (None, ""):
            return value
    return None


def _clarity_lookup_run_name(run_name: str) -> str:
    candidates = [part.strip() for part in run_name.split("+") if part.strip()]
    dated_candidates = [part for part in candidates if len(part) >= 6 and part[:6].isdigit()]
    if dated_candidates:
        return sorted(dated_candidates, key=lambda part: part[:6])[-1]
    if candidates:
        return candidates[-1]
    return run_name


def _run_name_without_run_number(run_name: str) -> str | None:
    run_name = _clarity_lookup_run_name(run_name)
    parts = run_name.split("_")
    if len(parts) < 4 or not parts[0].isdigit():
        return None
    return f"{parts[0]}_{parts[1]}_{parts[-1]}"


def _clarity_sample_info_candidates(config: Config, run_dir: Path) -> list[Path]:
    candidates = [
        run_dir / "pipeline_info" / "clarity_sample_info.json",
        run_dir / "clarity_sample_info.json",
    ]
    if config.imports.clarity_metadata_root is not None:
        reduced_run_name = _run_name_without_run_number(run_dir.name)
        if reduced_run_name is not None:
            pattern = f"*_{reduced_run_name}.json"
            matches = sorted(config.imports.clarity_metadata_root.glob(pattern))
            candidates.extend(matches or [config.imports.clarity_metadata_root / pattern])
    return candidates


def _default_clarity_sample_info_path(config: Config, run_dir: Path) -> Path | None:
    for sample_info_path in _clarity_sample_info_candidates(config, run_dir):
        if sample_info_path.is_file():
            return sample_info_path
    return None


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


def _missing_metadata_fields(sample: dict) -> list[str]:
    sample_metadata = sample.get("sample_metadata", {})
    if not isinstance(sample_metadata, dict):
        sample_metadata = {}

    missing = []
    if _maybe_float(_first_present(sample_metadata, ("ct", "CT"))) is None:
        missing.append("CT")
    if _maybe_float(
        _first_present(
            sample_metadata,
            (
                "library_concentration_ng_ul",
                "Library concentration (ng/ul)",
                "library_concentration",
                "libconc",
            ),
        )
    ) is None:
        missing.append("library concentration")
    if _maybe_int(
        _first_present(
            sample_metadata,
            (
                "library_fragment_length_bp",
                "Library fragment length (bp)",
                "library_fragment_length",
                "libfrag",
            ),
        )
    ) is None:
        missing.append("library fragment length")
    return missing


def _format_sample_metadata_warnings(
    *,
    samples: list[dict],
    sample_info_by_id: dict[str, dict[str, object]],
    sample_info_path: Path | None,
    checked_paths: list[Path],
    run_name: str,
) -> list[str]:
    warnings: list[str] = []
    missing_by_sample = {
        str(sample.get("sample_id") or sample.get("sample_run_id") or "").strip(): _missing_metadata_fields(sample)
        for sample in samples
    }
    missing_by_sample = {
        sample_id: fields
        for sample_id, fields in missing_by_sample.items()
        if sample_id and fields
    }
    if not missing_by_sample:
        return warnings

    if sample_info_path is None:
        checked = ", ".join(str(path) for path in checked_paths)
        sample_list = ", ".join(sorted(missing_by_sample))
        warnings.append(
            f"No Clarity metadata file found for run {run_name}; missing metadata for {sample_list}. "
            f"Checked: {checked}."
        )
        return warnings

    source = str(sample_info_path)
    for sample_id in sorted(missing_by_sample):
        fields = ", ".join(missing_by_sample[sample_id])
        if sample_id not in sample_info_by_id:
            warnings.append(
                f"No Clarity metadata entry for {sample_id} in {source}; missing {fields}."
            )
        else:
            warnings.append(
                f"Incomplete Clarity metadata for {sample_id} in {source}; missing {fields}."
            )
    return warnings


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
        "sample_metadata_ct": _maybe_float(_first_present(sample_metadata, ("ct", "CT"))),
        "sample_metadata_library_concentration_ng_ul": _maybe_float(
            _first_present(
                sample_metadata,
                (
                    "library_concentration_ng_ul",
                    "Library concentration (ng/ul)",
                    "library_concentration",
                    "libconc",
                ),
            )
        ),
        "sample_metadata_library_fragment_length_bp": _maybe_int(
            _first_present(
                sample_metadata,
                (
                    "library_fragment_length_bp",
                    "Library fragment length (bp)",
                    "library_fragment_length",
                    "libfrag",
                ),
            )
        ),
        "raw_json": json.dumps(sample, sort_keys=True),
        "imported_at": imported_at,
    }


def _sample_qc_summary_paths(run_dir: Path) -> list[Path]:
    paths: list[Path] = []
    for sample_dir in sorted(path for path in run_dir.iterdir() if path.is_dir()):
        flat_qc_summary_path = sample_dir / f"{sample_dir.name}_qc_summary.json"
        nested_qc_summary_path = sample_dir / "results" / f"{sample_dir.name}_qc_summary.json"
        flat_exists = flat_qc_summary_path.is_file()
        nested_exists = nested_qc_summary_path.is_file()
        if flat_exists and nested_exists:
            raise ValueError(
                "Ambiguous QC summary layout for"
                f" {sample_dir.name}: found both {flat_qc_summary_path} and {nested_qc_summary_path}"
            )
        if flat_exists:
            paths.append(flat_qc_summary_path)
        elif nested_exists:
            paths.append(nested_qc_summary_path)
    return paths


def _load_sample_summary(qc_summary_path: Path) -> dict:
    payload = json.loads(qc_summary_path.read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        return payload
    if isinstance(payload, list) and len(payload) == 1 and isinstance(payload[0], dict):
        return payload[0]
    raise ValueError(f"Expected a JSON object in {qc_summary_path}")


def _validate_required_sidecars(sample: dict, sample_dir: Path) -> None:
    outputs = sample.get("outputs", {})
    if not isinstance(outputs, dict):
        return
    missing: list[str] = []
    for output_key, relname in required_sidecars(outputs):
        try:
            path = safe_relative_path(sample_dir, str(relname))
        except ValueError as exc:
            raise ValueError(f"Unsafe sidecar path for {output_key}: {relname}") from exc
        if not path.is_file():
            missing.append(f"{output_key}: {path}")
    if missing:
        sample_id = sample.get("sample_id") or sample.get("sample_run_id") or sample_dir.name
        raise FileNotFoundError(
            f"Missing required index sidecar(s) for {sample_id}: " + "; ".join(missing)
        )


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


def import_run_with_report(
    config: Config,
    run_dir: Path,
    clarity_sample_info_path: Path | None = None,
) -> ImportReport:
    run_dir = run_dir.resolve()
    qc_summary_paths = _sample_qc_summary_paths(run_dir)
    if not qc_summary_paths:
        raise FileNotFoundError(f"Missing per-sample QC summary files under: {run_dir}")

    root, run_relpath = _find_matching_root(run_dir, config.results_roots)
    if clarity_sample_info_path is not None:
        sample_info_path = clarity_sample_info_path
        checked_paths = [clarity_sample_info_path]
    else:
        checked_paths = _clarity_sample_info_candidates(config, run_dir)
        sample_info_path = _default_clarity_sample_info_path(config, run_dir)

    sample_info_by_id = _load_clarity_sample_info(sample_info_path)
    records = [
        (_merge_clarity_sample_metadata(_load_sample_summary(path), sample_info_by_id), path)
        for path in qc_summary_paths
    ]
    first, _ = records[0]
    run_name = first.get("run_name") or run_dir.name
    warnings = _format_sample_metadata_warnings(
        samples=[sample for sample, _ in records],
        sample_info_by_id=sample_info_by_id,
        sample_info_path=sample_info_path,
        checked_paths=checked_paths,
        run_name=str(run_name),
    )

    connection = connect(config.database.path)
    try:
        init_db(connection)
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
            _validate_required_sidecars(sample, qc_summary_path.parent)
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
        return ImportReport(imported=imported, warnings=warnings)
    finally:
        connection.close()


def import_run(config: Config, run_dir: Path, clarity_sample_info_path: Path | None = None) -> int:
    return import_run_with_report(config, run_dir, clarity_sample_info_path).imported


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


def import_all_roots_with_report(config: Config) -> ImportReport:
    total = 0
    warnings: list[str] = []
    for root in config.results_roots:
        if not root.linux_path.exists():
            continue
        for run_dir in sorted(path for path in root.linux_path.iterdir() if path.is_dir()):
            if _sample_qc_summary_paths(run_dir):
                report = import_run_with_report(config, run_dir)
                total += report.imported
                warnings.extend(report.warnings)
    return ImportReport(imported=total, warnings=warnings)


def import_all_roots(config: Config) -> int:
    return import_all_roots_with_report(config).imported
