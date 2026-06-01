from __future__ import annotations

import csv
import io
import json
import re
import subprocess
from dataclasses import asdict, dataclass, field
from pathlib import Path

from virtitta.artifact_cache import get_cached_output_file
from virtitta.config import Config
from virtitta.outputs import effective_output_relname, safe_relative_path
from virtitta.repository import (
    connect,
    get_cluster_job,
    get_cluster_job_samples,
    get_sample,
    update_cluster_job_status,
    utc_now,
)


ARTIFACT_INPUT_FASTA = "input_fasta"
ARTIFACT_PREPARED_FASTA = "prepared_fasta"
ARTIFACT_ALIGNMENT = "alignment"
ARTIFACT_TREE = "treefile"
ARTIFACT_METADATA = "metadata"
ARTIFACT_GRAPETREE_JSON = "grapetree_json"
ARTIFACT_COMMANDS = "commands"
ARTIFACT_LOG = "log"
PUBLIC_CLUSTER_ARTIFACTS = {ARTIFACT_TREE, ARTIFACT_METADATA, ARTIFACT_GRAPETREE_JSON}
CLUSTER_ARTIFACT_ALIASES = {
    "treefile.nwk": ARTIFACT_TREE,
    "treefile.newick": ARTIFACT_TREE,
    "metadata.tsv": ARTIFACT_METADATA,
    "metadata.txt": ARTIFACT_METADATA,
    "grapetree.json": ARTIFACT_GRAPETREE_JSON,
}


class ClusterError(RuntimeError):
    pass


@dataclass
class PolyTTrimResult:
    sequence: str
    method: str | None = None


@dataclass
class FastaPrepStats:
    record_count: int = 0
    five_prime_trimmed_records: int = 0
    five_prime_trimmed_bases: int = 0
    poly_t_trimmed_records: int = 0
    poly_t_trimmed_bases: int = 0
    poly_t_exact_records: int = 0
    poly_t_fuzzy_records: int = 0
    poly_t_events: list[dict[str, str | int]] = field(default_factory=list)


def cluster_output_dir(config: Config, output_relpath: str) -> Path:
    return config.cluster.output_root / output_relpath


def cluster_artifacts(output_relpath: str) -> dict[str, str]:
    return {
        ARTIFACT_INPUT_FASTA: f"{output_relpath}/input.raw.fasta",
        ARTIFACT_PREPARED_FASTA: f"{output_relpath}/prepared.fasta",
        ARTIFACT_ALIGNMENT: f"{output_relpath}/aligned.fasta",
        ARTIFACT_TREE: f"{output_relpath}/iqtree.treefile",
        ARTIFACT_METADATA: f"{output_relpath}/metadata.tsv",
        ARTIFACT_GRAPETREE_JSON: f"{output_relpath}/grapetree.json",
        ARTIFACT_COMMANDS: f"{output_relpath}/commands.json",
        ARTIFACT_LOG: f"{output_relpath}/cluster.log",
    }


def artifact_path(config: Config, job: dict, artifact_key: str) -> Path:
    artifact_key = CLUSTER_ARTIFACT_ALIASES.get(artifact_key, artifact_key)
    artifacts = json.loads(job.get("artifacts_json") or "{}")
    relpath = artifacts.get(artifact_key)
    if not relpath and artifact_key == ARTIFACT_GRAPETREE_JSON and job.get("output_relpath"):
        relpath = cluster_artifacts(job["output_relpath"])[ARTIFACT_GRAPETREE_JSON]
    if not relpath:
        raise ClusterError(f"Artifact is not available: {artifact_key}")
    candidate = (config.cluster.output_root / relpath).resolve()
    root = config.cluster.output_root.resolve()
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise ClusterError("Cluster artifact path escaped output root") from exc
    if not candidate.exists():
        raise ClusterError(f"Cluster artifact is missing on disk: {artifact_key}")
    return candidate


def _sample_output_file(config: Config, connection, sample_row: dict, output_key: str) -> Path:
    cached_path = get_cached_output_file(config, connection, sample_row["sample_run_id"], output_key)
    if cached_path is not None:
        return cached_path

    raw = json.loads(sample_row["raw_json"])
    relname = effective_output_relname(output_key, raw.get("outputs", {}))
    if not relname:
        raise ClusterError(f"Missing {output_key} output for {sample_row['sample_run_id']}")

    root = config.get_root(sample_row["source_root_name"])
    if root is None:
        raise ClusterError(f"Configured results root not found: {sample_row['source_root_name']}")

    sample_dir = root.linux_path / sample_row["sample_results_relpath"]
    try:
        candidate = safe_relative_path(sample_dir, str(relname))
    except ValueError as exc:
        raise ClusterError(f"Unsafe file path for {sample_row['sample_run_id']}: {relname}") from exc
    if not candidate.exists():
        raise ClusterError(f"Missing file on disk for {sample_row['sample_run_id']}: {candidate}")
    return candidate


def _read_single_fasta_record(path: Path) -> tuple[str, str]:
    header = ""
    seq_parts: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line:
            continue
        if line.startswith(">"):
            if header:
                raise ClusterError(f"Expected one FASTA record in {path}")
            header = line[1:].strip()
        else:
            seq_parts.append(line.strip())
    if not header or not seq_parts:
        raise ClusterError(f"Missing FASTA content in {path}")
    return header, "".join(seq_parts)


def _iter_fasta_records(path: Path):
    header = ""
    seq_parts: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line:
            continue
        if line.startswith(">"):
            if header:
                yield header, "".join(seq_parts)
            header = line[1:].strip()
            seq_parts = []
        else:
            seq_parts.append(line.strip())
    if header:
        yield header, "".join(seq_parts)


def _trim_poly_t_tail(
    sequence: str,
    *,
    min_length: int,
    seed_length: int,
    seed_min_t: int,
    max_trailing_bases: int,
) -> PolyTTrimResult:
    if len(sequence) < min_length:
        return PolyTTrimResult(sequence)

    sequence_upper = sequence.upper()
    for match in re.finditer(f"T{{{min_length},}}", sequence_upper):
        if len(sequence) - match.end() <= max_trailing_bases:
            return PolyTTrimResult(sequence[: match.start()], "exact-run")

    seed_length = max(seed_length, min_length)
    seed_min_t = min(seed_min_t, seed_length)
    search_start = max(0, len(sequence) - max_trailing_bases - seed_length)
    for index in range(search_start, len(sequence) - seed_length + 1):
        trailing_bases = len(sequence) - (index + seed_length)
        if trailing_bases > max_trailing_bases:
            continue
        if sequence_upper[index] == "T" and sequence_upper[index : index + seed_length].count("T") >= seed_min_t:
            return PolyTTrimResult(sequence[:index], "fuzzy-seed")
    return PolyTTrimResult(sequence)


def _write_prepared_fasta(config: Config, input_fasta: Path, prepared_fasta: Path) -> FastaPrepStats:
    stats = FastaPrepStats()
    with prepared_fasta.open("w", encoding="utf-8") as handle:
        for header, sequence in _iter_fasta_records(input_fasta):
            stats.record_count += 1
            if config.cluster.five_prime_trim:
                trim_bases = min(len(sequence), config.cluster.five_prime_trim)
                sequence = sequence[config.cluster.five_prime_trim :]
                if trim_bases:
                    stats.five_prime_trimmed_records += 1
                    stats.five_prime_trimmed_bases += trim_bases
            if config.cluster.poly_t:
                original_length = len(sequence)
                trim_result = _trim_poly_t_tail(
                    sequence,
                    min_length=config.cluster.poly_t_min_length,
                    seed_length=config.cluster.poly_t_seed_length,
                    seed_min_t=config.cluster.poly_t_seed_min_t,
                    max_trailing_bases=config.cluster.poly_t_max_trailing_bases,
                )
                sequence = trim_result.sequence
                trimmed_bases = original_length - len(sequence)
                if trimmed_bases:
                    stats.poly_t_trimmed_records += 1
                    stats.poly_t_trimmed_bases += trimmed_bases
                    if trim_result.method == "exact-run":
                        stats.poly_t_exact_records += 1
                    elif trim_result.method == "fuzzy-seed":
                        stats.poly_t_fuzzy_records += 1
                    stats.poly_t_events.append(
                        {
                            "header": header,
                            "method": trim_result.method or "unknown",
                            "before": original_length,
                            "after": len(sequence),
                            "trimmed_bases": trimmed_bases,
                        }
                    )
            handle.write(f">{header}\n{sequence}\n")
    return stats


def _write_fasta_prep_log_summary(log_handle, config: Config, stats: FastaPrepStats) -> None:
    log_handle.write("prepare-fasta summary:\n")
    log_handle.write(f"  records: {stats.record_count}\n")
    five_prime_state = "enabled" if config.cluster.five_prime_trim else "disabled"
    log_handle.write(
        "  five-prime trim:"
        f" {five_prime_state}, {stats.five_prime_trimmed_records} records,"
        f" {stats.five_prime_trimmed_bases} bases\n"
    )
    if not config.cluster.poly_t:
        log_handle.write("  poly-T trim: disabled\n")
        return

    log_handle.write(
        "  poly-T trim:"
        f" enabled, {stats.poly_t_trimmed_records}/{stats.record_count} records,"
        f" {stats.poly_t_trimmed_bases} bases"
        f" ({stats.poly_t_exact_records} exact-run, {stats.poly_t_fuzzy_records} fuzzy-seed)\n"
    )
    if not stats.poly_t_events:
        log_handle.write("  poly-T events: none\n")
        return
    trim_lengths = sorted(int(event["trimmed_bases"]) for event in stats.poly_t_events)
    median = trim_lengths[len(trim_lengths) // 2]
    log_handle.write(
        "  poly-T trim lengths:"
        f" min={trim_lengths[0]}, median={median}, max={trim_lengths[-1]}\n"
    )
    for event in stats.poly_t_events:
        log_handle.write(
            "  poly-T event:"
            f" {event['header']} {event['before']} -> {event['after']}"
            f" (-{event['trimmed_bases']} bases, {event['method']})\n"
        )


def sample_tree_id(sample_row: dict) -> str:
    tree_id = str(sample_row.get("lid") or sample_row.get("sample_id") or sample_row.get("sample_run_id") or "").strip()
    if not tree_id:
        raise ClusterError(f"Sample has no usable tree ID: {sample_row.get('sample_run_id')}")
    return tree_id


def _resistance_metadata_value(raw_sample: dict) -> str:
    resistance = raw_sample.get("resistance", {}) if isinstance(raw_sample, dict) else {}
    if not resistance.get("analysis_present"):
        return "No resistance data"
    if not resistance.get("has_resistance"):
        return "No resistance detected"
    calls = []
    for record in resistance.get("by_drug", []):
        if not isinstance(record, dict):
            continue
        prediction = str(record.get("prediction") or "").lower()
        mutations = [str(item) for item in record.get("mutations", []) if item]
        if prediction and prediction not in {"susceptible", "none", "no resistance"}:
            calls.append(record.get("drug") or prediction)
        elif mutations:
            calls.extend(mutations)
    if calls:
        return ", ".join(str(item) for item in calls)
    count = resistance.get("mutation_count") or 0
    return f"{count} mutation{'s' if count != 1 else ''}"


def _metadata_value(connection, sample_row: dict, column: str) -> str:
    if column == "resistance_summary":
        return _resistance_metadata_value(json.loads(sample_row["raw_json"]))
    if column == "comment_count":
        row = connection.execute(
            "SELECT COUNT(*) AS count FROM sample_comments WHERE sample_run_id = ?",
            (sample_row["sample_run_id"],),
        ).fetchone()
        return str(row["count"] if row is not None else 0)
    value = sample_row.get(column)
    if value is None:
        return ""
    return str(value)


def cluster_metadata_columns(config: Config) -> list[str]:
    columns: list[str] = []
    for column in [*config.ui.table_columns, "comment_count"]:
        if column == "actions" or column in columns:
            continue
        columns.append(column)
    return columns


def prepare_cluster_files(
    config: Config,
    connection,
    sample_rows: list[dict],
    output_relpath: str,
    *,
    allow_duplicate_ids: bool = False,
) -> tuple[list[dict], str]:
    if len(sample_rows) < 2:
        raise ClusterError("Select at least two samples for clustering")

    output_dir = cluster_output_dir(config, output_relpath)
    output_dir.mkdir(parents=True, exist_ok=False)
    artifacts = cluster_artifacts(output_relpath)
    raw_fasta = config.cluster.output_root / artifacts[ARTIFACT_INPUT_FASTA]
    metadata_path = config.cluster.output_root / artifacts[ARTIFACT_METADATA]

    fasta_records: list[tuple[dict, str, str, str]] = []
    tree_id_counts: dict[str, int] = {}
    subtype_values = {str(row.get("typing_report_subtype") or "") for row in sample_rows}
    warnings = []
    if len(subtype_values) > 1:
        shown = ", ".join(sorted(value or "blank" for value in subtype_values))
        warnings.append(f"Selected samples contain multiple subtypes: {shown}.")

    for sample_row in sample_rows:
        fasta_path = _sample_output_file(config, connection, sample_row, config.cluster.input_output_key)
        header, sequence = _read_single_fasta_record(fasta_path)
        tree_id = sample_tree_id(sample_row)
        fasta_records.append((sample_row, tree_id, sequence, header))
        tree_id_counts[tree_id] = tree_id_counts.get(tree_id, 0) + 1

    duplicate_tree_ids = {tree_id for tree_id, count in tree_id_counts.items() if count > 1}
    if duplicate_tree_ids and not allow_duplicate_ids:
        first_duplicate = next(tree_id for _, tree_id, _, _ in fasta_records if tree_id in duplicate_tree_ids)
        raise ClusterError(f"Duplicate FASTA tree ID after normalization: {first_duplicate}")
    if duplicate_tree_ids:
        shown = ", ".join(sorted(duplicate_tree_ids))
        warnings.append(f"Duplicate FASTA tree IDs were renamed with run name suffixes: {shown}.")

    sample_records: list[dict] = []
    seen_tree_ids: set[str] = set()
    with raw_fasta.open("w", encoding="utf-8") as fasta_handle:
        for index, (sample_row, tree_id, sequence, _header) in enumerate(fasta_records):
            if tree_id in duplicate_tree_ids:
                tree_id = f"{tree_id}-{sample_row['run_name']}"
            if tree_id in seen_tree_ids:
                raise ClusterError(f"Duplicate FASTA tree ID after run-name disambiguation: {tree_id}")
            seen_tree_ids.add(tree_id)
            fasta_handle.write(f">{tree_id}\n{sequence}\n")
            sample_records.append(
                {
                    "job_id": output_relpath,
                    "sample_run_id": sample_row["sample_run_id"],
                    "tree_id": tree_id,
                    "display_identifier": sample_row.get("lid") or sample_row.get("sample_id") or tree_id,
                    "sort_order": index,
                }
            )

    metadata_buffer = io.StringIO(newline="")
    with metadata_buffer:
        writer = csv.writer(metadata_buffer, delimiter="\t", lineterminator="\n")
        metadata_columns = cluster_metadata_columns(config)
        writer.writerow(["ID", *metadata_columns])
        for sample_row, sample_record in zip(sample_rows, sample_records, strict=True):
            writer.writerow(
                [sample_record["tree_id"], *[_metadata_value(connection, sample_row, column) for column in metadata_columns]]
            )
        metadata_path.write_text(metadata_buffer.getvalue().rstrip("\n"), encoding="utf-8")

    return sample_records, "\n".join(warnings)


def write_grapetree_json(config: Config, output_relpath: str) -> None:
    artifacts = cluster_artifacts(output_relpath)
    tree_path = config.cluster.output_root / artifacts[ARTIFACT_TREE]
    metadata_path = config.cluster.output_root / artifacts[ARTIFACT_METADATA]
    grapetree_path = config.cluster.output_root / artifacts[ARTIFACT_GRAPETREE_JSON]

    metadata_rows: dict[str, dict[str, str]] = {}
    with metadata_path.open("r", encoding="utf-8", newline="") as metadata_handle:
        reader = csv.DictReader(metadata_handle, delimiter="\t")
        fieldnames = reader.fieldnames or []
        for row in reader:
            row_id = row.get("ID") or ""
            if row_id:
                metadata_rows[row_id] = {key: value or "" for key, value in row.items() if key}

    metadata_options = {
        field: {"label": field, "category": "User Added", "coltype": "character"}
        for field in fieldnames
        if field != "ID"
    }
    payload = {
        "nwk": tree_path.read_text(encoding="utf-8").strip(),
        "layout_algorithm": "greedy",
        "metadata": metadata_rows,
        "metadata_options": metadata_options,
    }
    if "sample_category" in metadata_options:
        payload["initial_category"] = "sample_category"
    grapetree_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def ensure_grapetree_json(config: Config, job: dict) -> None:
    output_relpath = job.get("output_relpath")
    if not output_relpath:
        raise ClusterError("Cluster job is missing output path")
    grapetree_path = config.cluster.output_root / cluster_artifacts(output_relpath)[ARTIFACT_GRAPETREE_JSON]
    should_write = not grapetree_path.exists()
    if not should_write:
        try:
            should_write = "layout_algorithm" not in json.loads(grapetree_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            should_write = True
    if should_write:
        write_grapetree_json(config, output_relpath)


def command_config_snapshot(config: Config) -> dict:
    snapshot = asdict(config.cluster)
    snapshot["output_root"] = str(config.cluster.output_root)
    return snapshot


def write_command_snapshot(config: Config, output_relpath: str) -> None:
    path = config.cluster.output_root / cluster_artifacts(output_relpath)[ARTIFACT_COMMANDS]
    path.write_text(json.dumps(command_config_snapshot(config), indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _run_command(argv: list[str], *, cwd: Path, timeout_seconds: int, stdout_path: Path | None, log_handle) -> None:
    log_handle.write(f"$ {' '.join(argv)}\n")
    stdout_target = subprocess.PIPE if stdout_path is None else stdout_path.open("wb")
    try:
        result = subprocess.run(
            argv,
            cwd=cwd,
            stdout=stdout_target,
            stderr=subprocess.PIPE,
            timeout=timeout_seconds,
            check=False,
        )
    except FileNotFoundError as exc:
        raise ClusterError(f"Command not found: {argv[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise ClusterError(f"Command timed out: {argv[0]}") from exc
    finally:
        if stdout_path is not None:
            stdout_target.close()

    if stdout_path is None:
        log_handle.write(result.stdout.decode("utf-8", errors="replace"))
    log_handle.write(result.stderr.decode("utf-8", errors="replace"))
    if result.returncode != 0:
        raise ClusterError(f"Command failed with exit code {result.returncode}: {argv[0]}")


def run_cluster_job(config: Config, job_id: str) -> None:
    connection = connect(config.database.path)
    artifacts_json = json.dumps(cluster_artifacts(job_id), sort_keys=True)
    try:
        job = get_cluster_job(connection, job_id)
        if job is None:
            return
        update_cluster_job_status(connection, job_id, "running", started_at=utc_now(), artifacts_json=artifacts_json)
        samples = get_cluster_job_samples(connection, job_id)
        for sample in samples:
            if get_sample(connection, sample["sample_run_id"]) is None:
                raise ClusterError(f"Sample disappeared before clustering: {sample['sample_run_id']}")
    finally:
        connection.close()

    output_dir = cluster_output_dir(config, job_id)
    artifacts = cluster_artifacts(job_id)
    prepared_fasta = config.cluster.output_root / artifacts[ARTIFACT_PREPARED_FASTA]
    alignment = config.cluster.output_root / artifacts[ARTIFACT_ALIGNMENT]
    treefile = config.cluster.output_root / artifacts[ARTIFACT_TREE]
    log_path = config.cluster.output_root / artifacts[ARTIFACT_LOG]

    error_text = None
    try:
        with log_path.open("w", encoding="utf-8") as log_handle:
            input_fasta = config.cluster.output_root / artifacts[ARTIFACT_INPUT_FASTA]
            log_handle.write(
                "$ virtitta prepare-fasta"
                f" --five-prime-trim {config.cluster.five_prime_trim}"
                f" --poly-t-min-length {config.cluster.poly_t_min_length}"
                f" --poly-t-seed-length {config.cluster.poly_t_seed_length}"
                f" --poly-t-seed-min-t {config.cluster.poly_t_seed_min_t}"
                f" --poly-t-max-trailing-bases {config.cluster.poly_t_max_trailing_bases}"
                f" {input_fasta} {prepared_fasta}\n"
            )
            prep_stats = _write_prepared_fasta(config, input_fasta, prepared_fasta)
            _write_fasta_prep_log_summary(log_handle, config, prep_stats)

            mafft_argv = [config.cluster.mafft_command, *config.cluster.mafft_args, str(prepared_fasta)]
            _run_command(
                mafft_argv,
                cwd=output_dir,
                timeout_seconds=config.cluster.timeout_seconds,
                stdout_path=alignment,
                log_handle=log_handle,
            )

            iqtree_argv = [
                config.cluster.iqtree_command,
                "-s",
                str(alignment),
                "-T",
                str(config.cluster.iqtree_threads),
                "-pre",
                str(output_dir / "iqtree"),
                *config.cluster.iqtree_args,
            ]
            _run_command(
                iqtree_argv,
                cwd=output_dir,
                timeout_seconds=config.cluster.timeout_seconds,
                stdout_path=None,
                log_handle=log_handle,
            )

        if not treefile.exists():
            raise ClusterError("IQ-TREE completed but did not create iqtree.treefile")
        write_grapetree_json(config, job_id)
    except Exception as exc:
        error_text = str(exc)

    connection = connect(config.database.path)
    try:
        if error_text:
            update_cluster_job_status(
                connection,
                job_id,
                "failed",
                completed_at=utc_now(),
                error_text=error_text,
                artifacts_json=artifacts_json,
            )
        else:
            update_cluster_job_status(
                connection,
                job_id,
                "completed",
                completed_at=utc_now(),
                artifacts_json=artifacts_json,
            )
    finally:
        connection.close()
