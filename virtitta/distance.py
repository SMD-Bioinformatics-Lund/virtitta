from __future__ import annotations

import csv
import io
import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

from virtitta.cluster import (
    ARTIFACT_COMMANDS,
    ARTIFACT_LOG,
    ClusterError,
    _iter_fasta_records,
    _read_single_fasta_record,
    _run_command,
    _trim_poly_t_tail,
    artifact_path,
    cluster_output_dir,
    sample_tree_id,
)
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


MIN_DISTANCE_SAMPLES = 2
ANALYSIS_TYPE = "distance_matrices"
ARTIFACT_IUPAC_RAW = "iupac_input_fasta"
ARTIFACT_MAJORITY_RAW = "majority_input_fasta"
ARTIFACT_IUPAC_PREPARED = "iupac_prepared_fasta"
ARTIFACT_MAJORITY_PREPARED = "majority_prepared_fasta"
ARTIFACT_ALIGNMENT = "iupac_alignment"
ARTIFACT_RESULT = "result_json"
ARTIFACT_IUPAC_COUNTS = "iupac_counts_tsv"
ARTIFACT_IUPAC_DETAILS = "iupac_details_tsv"
ARTIFACT_MAJORITY_COUNTS = "majority_counts_tsv"
ARTIFACT_MAJORITY_DETAILS = "majority_details_tsv"
ARTIFACT_COVERAGE_MASKS = "coverage_masks_bed"

IUPAC_ALLELES = {
    "A": frozenset("A"), "C": frozenset("C"), "G": frozenset("G"), "T": frozenset("T"),
    "R": frozenset("AG"), "Y": frozenset("CT"), "S": frozenset("GC"), "W": frozenset("AT"),
    "K": frozenset("GT"), "M": frozenset("AC"), "B": frozenset("CGT"), "D": frozenset("AGT"),
    "H": frozenset("ACT"), "V": frozenset("ACG"),
}


@dataclass(frozen=True)
class PairDistance:
    substitutions: int
    indel_events: int
    total_events: int
    compared_bases: int


def distance_artifacts(output_relpath: str) -> dict[str, str]:
    return {
        ARTIFACT_IUPAC_RAW: f"{output_relpath}/iupac.raw.fasta",
        ARTIFACT_MAJORITY_RAW: f"{output_relpath}/majority.raw.fasta",
        ARTIFACT_IUPAC_PREPARED: f"{output_relpath}/iupac.prepared.fasta",
        ARTIFACT_MAJORITY_PREPARED: f"{output_relpath}/majority.prepared.fasta",
        ARTIFACT_ALIGNMENT: f"{output_relpath}/iupac.aligned.fasta",
        ARTIFACT_RESULT: f"{output_relpath}/distance-matrices.json",
        ARTIFACT_IUPAC_COUNTS: f"{output_relpath}/iupac.counts.tsv",
        ARTIFACT_IUPAC_DETAILS: f"{output_relpath}/iupac.details.tsv",
        ARTIFACT_MAJORITY_COUNTS: f"{output_relpath}/majority.counts.tsv",
        ARTIFACT_MAJORITY_DETAILS: f"{output_relpath}/majority.details.tsv",
        ARTIFACT_COVERAGE_MASKS: f"{output_relpath}/coverage-masks.bed",
        ARTIFACT_COMMANDS: f"{output_relpath}/commands.json",
        ARTIFACT_LOG: f"{output_relpath}/distance.log",
    }


def distance_artifact_path(config: Config, job: dict, artifact_key: str) -> Path:
    return artifact_path(config, job, artifact_key)


def distance_config_snapshot(config: Config) -> dict:
    snapshot = asdict(config.cluster)
    snapshot["output_root"] = str(config.cluster.output_root)
    return {"analysis_type": ANALYSIS_TYPE, "cluster": snapshot}


def _distance_output_file(config: Config, sample_row: dict, output_key: str) -> Path:
    """Resolve one coordinate-sensitive input without mixing in the general output cache."""
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
    if not candidate.is_file():
        raise ClusterError(f"Missing file on disk for {sample_row['sample_run_id']}: {candidate}")
    return candidate


def prepare_distance_files(
    config: Config,
    connection,
    sample_rows: list[dict],
    output_relpath: str,
    *,
    allow_duplicate_ids: bool = False,
) -> tuple[list[dict], str]:
    if len(sample_rows) < MIN_DISTANCE_SAMPLES:
        raise ClusterError(f"Select at least {MIN_DISTANCE_SAMPLES} samples for distance matrices")

    artifacts = distance_artifacts(output_relpath)
    records = []
    id_counts: dict[str, int] = {}
    for row in sample_rows:
        _, iupac = _read_single_fasta_record(_distance_output_file(config, row, "export_iupac_fasta"))
        _, majority = _read_single_fasta_record(_distance_output_file(config, row, "export_fasta"))
        if len(iupac) != len(majority):
            raise ClusterError(
                f"IUPAC and majority FASTA lengths differ for {row['sample_run_id']}: {len(iupac)} != {len(majority)}"
            )
        tree_id = sample_tree_id(row)
        records.append((row, tree_id, iupac, majority))
        id_counts[tree_id] = id_counts.get(tree_id, 0) + 1

    duplicates = {tree_id for tree_id, count in id_counts.items() if count > 1}
    if duplicates and not allow_duplicate_ids:
        raise ClusterError(f"Duplicate FASTA tree ID after normalization: {sorted(duplicates)[0]}")

    output_dir = cluster_output_dir(config, output_relpath)
    output_dir.mkdir(parents=True, exist_ok=False)
    sample_records = []
    resolved = set()
    iupac_lines = []
    majority_lines = []
    for index, (row, tree_id, iupac, majority) in enumerate(records):
        if tree_id in duplicates:
            tree_id = f"{tree_id}-{row['run_name']}"
        if tree_id in resolved:
            raise ClusterError(f"Duplicate FASTA tree ID after run-name disambiguation: {tree_id}")
        resolved.add(tree_id)
        iupac_lines.extend((f">{tree_id}", iupac))
        majority_lines.extend((f">{tree_id}", majority))
        sample_records.append({
            "job_id": output_relpath,
            "sample_run_id": row["sample_run_id"],
            "tree_id": tree_id,
            "display_identifier": row.get("lid") or row.get("sample_id") or tree_id,
            "sort_order": index,
        })
    (config.cluster.output_root / artifacts[ARTIFACT_IUPAC_RAW]).write_text("\n".join(iupac_lines) + "\n", encoding="utf-8")
    (config.cluster.output_root / artifacts[ARTIFACT_MAJORITY_RAW]).write_text("\n".join(majority_lines) + "\n", encoding="utf-8")
    warning = ""
    if duplicates:
        warning = "Duplicate FASTA tree IDs were renamed with run name suffixes: " + ", ".join(sorted(duplicates)) + "."
    return sample_records, warning


def _trim_coordinates(config: Config, iupac: str) -> tuple[int, int]:
    start = min(config.cluster.five_prime_trim, len(iupac)) if config.cluster.five_prime_trim else 0
    end = len(iupac)
    if config.cluster.poly_t:
        trimmed = _trim_poly_t_tail(
            iupac[start:],
            min_length=config.cluster.poly_t_min_length,
            seed_length=config.cluster.poly_t_seed_length,
            seed_min_t=config.cluster.poly_t_seed_min_t,
            max_trailing_bases=config.cluster.poly_t_max_trailing_bases,
        ).sequence
        end = start + len(trimmed)
    return start, end


def _prepared_variants(config: Config, iupac: str, majority: str) -> tuple[str, str]:
    start, end = _trim_coordinates(config, iupac)
    return iupac[start:end], majority[start:end]


def _write_prepared_variants(config: Config, artifacts: dict[str, str]) -> None:
    iupac_records = list(_iter_fasta_records(config.cluster.output_root / artifacts[ARTIFACT_IUPAC_RAW]))
    majority_records = list(_iter_fasta_records(config.cluster.output_root / artifacts[ARTIFACT_MAJORITY_RAW]))
    if [x[0] for x in iupac_records] != [x[0] for x in majority_records]:
        raise ClusterError("IUPAC and majority FASTA identifiers do not match")
    iupac_lines, majority_lines = [], []
    for (identifier, iupac), (_, majority) in zip(iupac_records, majority_records, strict=True):
        if len(iupac) != len(majority):
            raise ClusterError(f"IUPAC and majority FASTA lengths differ for {identifier}")
        prepared_iupac, prepared_majority = _prepared_variants(config, iupac, majority)
        iupac_lines.extend((f">{identifier}", prepared_iupac))
        majority_lines.extend((f">{identifier}", prepared_majority))
    (config.cluster.output_root / artifacts[ARTIFACT_IUPAC_PREPARED]).write_text("\n".join(iupac_lines) + "\n", encoding="utf-8")
    (config.cluster.output_root / artifacts[ARTIFACT_MAJORITY_PREPARED]).write_text("\n".join(majority_lines) + "\n", encoding="utf-8")


def project_sequence(aligned_template: str, source: str, *, validate_bases: bool = True) -> str:
    if sum(char != "-" for char in aligned_template) != len(source):
        raise ClusterError("Aligned IUPAC sequence cannot be mapped to its prepared sequence")
    if validate_bases and aligned_template.replace("-", "").upper() != source.upper():
        raise ClusterError("Aligned IUPAC sequence cannot be mapped to its prepared sequence")
    source_iter = iter(source)
    return "".join("-" if char == "-" else next(source_iter) for char in aligned_template)


def project_mask(aligned_template: str, source: list[bool]) -> list[bool]:
    if sum(char != "-" for char in aligned_template) != len(source):
        raise ClusterError("Aligned sequence cannot be mapped to its coverage mask")
    source_iter = iter(source)
    return [False if char == "-" else next(source_iter) for char in aligned_template]


def parse_coverage_bed(path: Path, identifier: str, length: int) -> list[bool]:
    mask = [False] * length
    previous_end = 0
    seen = False
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise ClusterError(f"Cannot read coverage BED {path}: {exc}") from exc
    for line_number, line in enumerate(lines, 1):
        if not line.strip() or line.startswith("#"):
            continue
        fields = line.split("\t")
        if len(fields) < 3:
            raise ClusterError(f"Invalid coverage BED {path}:{line_number}: expected at least 3 columns")
        if fields[0] != identifier:
            raise ClusterError(
                f"Invalid coverage BED {path}:{line_number}: identifier {fields[0]!r} does not match FASTA {identifier!r}"
            )
        try:
            start, end = int(fields[1]), int(fields[2])
        except ValueError as exc:
            raise ClusterError(f"Invalid coverage BED {path}:{line_number}: coordinates must be integers") from exc
        if start < 0 or end <= start or end > length:
            raise ClusterError(f"Invalid coverage BED {path}:{line_number}: interval {start}-{end} is out of bounds")
        if seen and start < previous_end:
            raise ClusterError(f"Invalid coverage BED {path}:{line_number}: intervals overlap or are out of order")
        mask[start:end] = [True] * (end - start)
        previous_end = end
        seen = True
    return mask


def _mask_bed(identifier: str, mask: list[bool]) -> str:
    lines = []
    start = None
    for index, covered in enumerate([*mask, False]):
        if covered and start is None:
            start = index
        elif not covered and start is not None:
            lines.append(f"{identifier}\t{start}\t{index}")
            start = None
    return "\n".join(lines) + ("\n" if lines else "")


def _file_signature(paths: dict[str, Path]) -> dict:
    signature = {}
    for key, path in paths.items():
        try:
            stat = path.stat()
        except OSError as exc:
            raise ClusterError(f"Missing {key} required to derive coverage mask: {path}") from exc
        signature[key] = {"path": str(path.resolve()), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
    return signature


def _historical_mask(config: Config, connection, row: dict, log_handle) -> tuple[Path, dict]:
    paths = {key: _distance_output_file(config, row, key) for key in (
        "main_fasta", "main_fasta_index", "main_cram", "main_cram_index"
    )}
    signature = _file_signature(paths)
    cache_dir = config.cache.outputs_root / "coverage-1x" / row["sample_run_id"]
    bed_path = cache_dir / "coverage-1x.bed"
    manifest_path = cache_dir / "manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        manifest = None
    if manifest == signature and bed_path.is_file():
        return bed_path, {"source": "derived_cache", "path": str(bed_path), "signature": signature}

    cache_dir.mkdir(parents=True, exist_ok=True)
    fd, depth_name = tempfile.mkstemp(prefix="depth-", suffix=".tsv", dir=cache_dir)
    os.close(fd)
    depth_path = Path(depth_name)
    try:
        _run_command(
            [
                config.cluster.samtools_command,
                "depth",
                "-aa",
                "--reference",
                str(paths["main_fasta"]),
                str(paths["main_cram"]),
            ],
            cwd=cache_dir, timeout_seconds=config.cluster.timeout_seconds, stdout_path=depth_path, log_handle=log_handle,
        )
        fasta_id, sequence = _read_single_fasta_record(paths["main_fasta"])
        mask = [False] * len(sequence)
        expected_position = 1
        for line_number, line in enumerate(depth_path.read_text(encoding="utf-8").splitlines(), 1):
            fields = line.split("\t")
            if len(fields) < 3 or fields[0] != fasta_id:
                raise ClusterError(f"Invalid samtools depth output at line {line_number}")
            try:
                position, depth = int(fields[1]), int(fields[2])
            except ValueError as exc:
                raise ClusterError(f"Invalid samtools depth coordinates at line {line_number}") from exc
            if position != expected_position or position > len(mask) or depth < 0:
                raise ClusterError(f"Invalid samtools depth ordering at line {line_number}")
            mask[position - 1] = depth >= 1
            expected_position += 1
        if expected_position != len(mask) + 1:
            raise ClusterError("samtools depth -aa did not return every FASTA position")
        bed_text = _mask_bed(fasta_id, mask)
        bed_fd, bed_tmp_name = tempfile.mkstemp(prefix=".coverage-1x-", suffix=".bed.tmp", dir=cache_dir)
        manifest_fd, manifest_tmp_name = tempfile.mkstemp(prefix=".manifest-", suffix=".json.tmp", dir=cache_dir)
        os.close(bed_fd)
        os.close(manifest_fd)
        bed_tmp = Path(bed_tmp_name)
        manifest_tmp = Path(manifest_tmp_name)
        try:
            bed_tmp.write_text(bed_text, encoding="utf-8")
            manifest_tmp.write_text(json.dumps(signature, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            os.replace(bed_tmp, bed_path)
            os.replace(manifest_tmp, manifest_path)
        finally:
            bed_tmp.unlink(missing_ok=True)
            manifest_tmp.unlink(missing_ok=True)
    finally:
        depth_path.unlink(missing_ok=True)
    return bed_path, {"source": "derived", "path": str(bed_path), "signature": signature}


def _sample_mask(config: Config, connection, row: dict, log_handle) -> tuple[list[bool], dict]:
    main_fasta = _distance_output_file(config, row, "main_fasta")
    fasta_id, sequence = _read_single_fasta_record(main_fasta)
    raw = json.loads(row["raw_json"])
    if raw.get("outputs", {}).get("coverage_1x_bed"):
        bed_path = _distance_output_file(config, row, "coverage_1x_bed")
        provenance = {"source": "supplied", "path": str(bed_path)}
    else:
        bed_path, provenance = _historical_mask(config, connection, row, log_handle)
    mask = parse_coverage_bed(bed_path, fasta_id, len(sequence))
    provenance["covered_positions"] = sum(mask)
    provenance["total_positions"] = len(mask)
    return mask, provenance


def pair_distance(first: str, second: str, first_mask: list[bool] | None = None, second_mask: list[bool] | None = None) -> PairDistance:
    if len(first) != len(second):
        raise ClusterError("Aligned sequences have different lengths")
    if first_mask is None:
        first_mask = [True] * len(first)
    if second_mask is None:
        second_mask = [True] * len(second)
    if len(first_mask) != len(first) or len(second_mask) != len(second):
        raise ClusterError("Aligned sequences and coverage masks have different lengths")
    substitutions = compared = indels = 0
    first_non_gap = next((i for i, char in enumerate(first) if char != "-"), len(first))
    second_non_gap = next((i for i, char in enumerate(second) if char != "-"), len(second))
    first_last = next((i for i in range(len(first) - 1, -1, -1) if first[i] != "-"), -1)
    second_last = next((i for i in range(len(second) - 1, -1, -1) if second[i] != "-"), -1)
    index = 0
    while index < len(first):
        a, b = first[index], second[index]
        direction = 1 if a == "-" and b != "-" else 2 if b == "-" and a != "-" else 0
        if direction:
            start = index
            evaluable = True
            while index < len(first):
                a, b = first[index], second[index]
                current = 1 if a == "-" and b != "-" else 2 if b == "-" and a != "-" else 0
                if current != direction:
                    break
                nongap, covered = (b, second_mask[index]) if direction == 1 else (a, first_mask[index])
                evaluable = evaluable and covered and nongap.upper() in IUPAC_ALLELES
                index += 1
            terminal = (direction == 1 and (start < first_non_gap or index - 1 > first_last)) or (
                direction == 2 and (start < second_non_gap or index - 1 > second_last)
            )
            gapped_mask = first_mask if direction == 1 else second_mask
            gapped_seq = first if direction == 1 else second
            left, right = start - 1, index
            flanked = (
                left >= 0 and right < len(first) and gapped_seq[left] != "-" and gapped_seq[right] != "-"
                and gapped_mask[left] and gapped_mask[right]
                and gapped_seq[left].upper() in IUPAC_ALLELES and gapped_seq[right].upper() in IUPAC_ALLELES
            )
            if evaluable and flanked and not terminal:
                indels += 1
            continue
        if a != "-" and b != "-" and first_mask[index] and second_mask[index]:
            alleles_a = IUPAC_ALLELES.get(a.upper())
            alleles_b = IUPAC_ALLELES.get(b.upper())
            if alleles_a is not None and alleles_b is not None:
                compared += 1
                if alleles_a.isdisjoint(alleles_b):
                    substitutions += 1
        index += 1
    return PairDistance(substitutions, indels, substitutions + indels, compared)


def calculate_matrix(records: list[tuple[str, str]], masks: dict[str, list[bool]] | None = None) -> dict:
    identifiers = [identifier for identifier, _ in records]
    size = len(records)
    cells = [[None for _ in range(size)] for _ in range(size)]
    for i in range(size):
        mask = masks[records[i][0]] if masks else [True] * len(records[i][1])
        cells[i][i] = asdict(PairDistance(0, 0, 0, sum(covered and c.upper() in IUPAC_ALLELES for c, covered in zip(records[i][1], mask, strict=True))))
        for j in range(i + 1, size):
            first_mask = masks[records[i][0]] if masks else None
            second_mask = masks[records[j][0]] if masks else None
            value = asdict(pair_distance(records[i][1], records[j][1], first_mask, second_mask))
            cells[i][j] = value
            cells[j][i] = dict(value)
    return {"identifiers": identifiers, "cells": cells}


def add_upgma_ordering(matrix: dict) -> dict:
    identifiers = matrix.get("identifiers", [])
    cells = matrix.get("cells", [])
    size = len(identifiers)
    if len(set(identifiers)) != size or len(cells) != size or any(len(row) != size for row in cells):
        raise ClusterError("Distance matrix cannot be ordered because it is malformed")

    available_events = [
        cells[i][j]["total_events"]
        for i in range(size)
        for j in range(i + 1, size)
        if cells[i][j]["compared_bases"] > 0
    ]
    zero_compared_penalty = max(available_events, default=0) + 1

    def leaf_distance(first: int, second: int) -> int:
        cell = cells[first][second]
        return zero_compared_penalty if cell["compared_bases"] == 0 else cell["total_events"]

    clusters = [{"members": (index,), "order": (index,)} for index in range(size)]
    while len(clusters) > 1:
        choices = []
        for first_index in range(len(clusters)):
            for second_index in range(first_index + 1, len(clusters)):
                pair = sorted((clusters[first_index], clusters[second_index]), key=lambda item: item["members"])
                distance = sum(
                    leaf_distance(first, second)
                    for first in pair[0]["members"]
                    for second in pair[1]["members"]
                ) / (len(pair[0]["members"]) * len(pair[1]["members"]))
                choices.append((distance, pair[0]["members"], pair[1]["members"], first_index, second_index, pair))
        _, _, _, first_index, second_index, pair = min(choices)
        merged = {
            "members": tuple(sorted((*pair[0]["members"], *pair[1]["members"]))),
            "order": (*pair[0]["order"], *pair[1]["order"]),
        }
        for index in sorted((first_index, second_index), reverse=True):
            clusters.pop(index)
        clusters.append(merged)

    order = clusters[0]["order"] if clusters else ()
    matrix["ordering"] = {
        "method": "UPGMA",
        "metric": "total_events",
        "zero_compared_penalty": zero_compared_penalty,
        "identifiers": [identifiers[index] for index in order],
    }
    return matrix


def ensure_result_ordering(result: dict) -> dict:
    for mode in ("iupac", "majority"):
        matrix = result.get(mode)
        if isinstance(matrix, dict) and "ordering" not in matrix:
            add_upgma_ordering(matrix)
    return result


def _detail(value: dict) -> str:
    return (
        f"{value['total_events']} events ({value['substitutions']} substitutions + "
        f"{value['indel_events']} indel); {value['compared_bases']:,} bases compared"
    )


def _matrix_tsv(matrix: dict, *, details: bool) -> str:
    buffer = io.StringIO(newline="")
    writer = csv.writer(buffer, delimiter="\t", lineterminator="\n")
    writer.writerow(["ID", *matrix["identifiers"]])
    for row_index, (identifier, row) in enumerate(zip(matrix["identifiers"], matrix["cells"], strict=True)):
        values = []
        for column_index, cell in enumerate(row):
            unavailable = row_index != column_index and cell["compared_bases"] == 0
            if unavailable:
                values.append("No bases compared" if details else -1)
            else:
                values.append(_detail(cell) if details else cell["total_events"])
        writer.writerow([identifier, *values])
    return buffer.getvalue()


def run_distance_job(config: Config, job_id: str) -> None:
    artifacts = distance_artifacts(job_id)
    artifacts_json = json.dumps(artifacts, sort_keys=True)
    initial_error = None
    connection = connect(config.database.path)
    try:
        if get_cluster_job(connection, job_id) is None:
            return
        update_cluster_job_status(connection, job_id, "running", started_at=utc_now(), artifacts_json=artifacts_json)
        for sample in get_cluster_job_samples(connection, job_id):
            if get_sample(connection, sample["sample_run_id"]) is None:
                raise ClusterError(f"Sample disappeared before distance analysis: {sample['sample_run_id']}")
    except Exception as exc:
        initial_error = str(exc)
    finally:
        connection.close()

    error_text = initial_error
    try:
        if error_text:
            raise ClusterError(error_text)
        output_dir = cluster_output_dir(config, job_id)
        log_path = config.cluster.output_root / artifacts[ARTIFACT_LOG]
        with log_path.open("w", encoding="utf-8") as log_handle:
            _write_prepared_variants(config, artifacts)
            mask_connection = connect(config.database.path)
            try:
                job_samples = get_cluster_job_samples(mask_connection, job_id)
                prepared_by_id = dict(_iter_fasta_records(config.cluster.output_root / artifacts[ARTIFACT_IUPAC_PREPARED]))
                raw_by_id = dict(_iter_fasta_records(config.cluster.output_root / artifacts[ARTIFACT_IUPAC_RAW]))
                prepared_masks: dict[str, list[bool]] = {}
                provenance: dict[str, dict] = {}
                bed_lines = []
                for job_sample in job_samples:
                    row = get_sample(mask_connection, job_sample["sample_run_id"])
                    if row is None:
                        raise ClusterError(f"Sample disappeared before distance analysis: {job_sample['sample_run_id']}")
                    mask, sample_provenance = _sample_mask(config, mask_connection, row, log_handle)
                    raw_sequence = raw_by_id[job_sample["tree_id"]]
                    if len(mask) != len(raw_sequence):
                        raise ClusterError(
                            f"Coverage mask and consensus FASTA lengths differ for {row['sample_run_id']}: "
                            f"{len(mask)} != {len(raw_sequence)}"
                        )
                    start, end = _trim_coordinates(config, raw_sequence)
                    prepared_mask = mask[start:end]
                    if len(prepared_mask) != len(prepared_by_id[job_sample["tree_id"]]):
                        raise ClusterError(f"Prepared coverage mask length differs for {row['sample_run_id']}")
                    prepared_masks[job_sample["tree_id"]] = prepared_mask
                    sample_provenance.update({
                        "sample_run_id": row["sample_run_id"], "trim_start": start, "trim_end": end,
                        "prepared_covered_positions": sum(prepared_mask),
                        "prepared_total_positions": len(prepared_mask),
                    })
                    provenance[job_sample["tree_id"]] = sample_provenance
                    bed_lines.append(_mask_bed(job_sample["tree_id"], prepared_mask).rstrip())
                (config.cluster.output_root / artifacts[ARTIFACT_COVERAGE_MASKS]).write_text(
                    "\n".join(line for line in bed_lines if line) + "\n", encoding="utf-8"
                )
            finally:
                mask_connection.close()
            prepared = config.cluster.output_root / artifacts[ARTIFACT_IUPAC_PREPARED]
            alignment_path = config.cluster.output_root / artifacts[ARTIFACT_ALIGNMENT]
            _run_command(
                [config.cluster.mafft_command, *config.cluster.mafft_args, str(prepared)],
                cwd=output_dir,
                timeout_seconds=config.cluster.timeout_seconds,
                stdout_path=alignment_path,
                log_handle=log_handle,
            )
        aligned = dict(_iter_fasta_records(config.cluster.output_root / artifacts[ARTIFACT_ALIGNMENT]))
        prepared_iupac = list(_iter_fasta_records(config.cluster.output_root / artifacts[ARTIFACT_IUPAC_PREPARED]))
        prepared_majority = dict(_iter_fasta_records(config.cluster.output_root / artifacts[ARTIFACT_MAJORITY_PREPARED]))
        iupac_projected, majority_projected = [], []
        projected_masks = {}
        for identifier, source_iupac in prepared_iupac:
            if identifier not in aligned or identifier not in prepared_majority:
                raise ClusterError(f"MAFFT alignment is missing sequence: {identifier}")
            template = aligned[identifier]
            iupac_projected.append((identifier, project_sequence(template, source_iupac)))
            majority_projected.append(
                (identifier, project_sequence(template, prepared_majority[identifier], validate_bases=False))
            )
            projected_masks[identifier] = project_mask(template, prepared_masks[identifier])
        result = {
            "analysis_type": ANALYSIS_TYPE,
            "description": "Coverage-supported event-like minimum difference counts; not phylogenetic or transmission distance.",
            "coverage": {"threshold": ">=1x", "artifact": ARTIFACT_COVERAGE_MASKS, "samples": provenance},
            "iupac": calculate_matrix(iupac_projected, projected_masks),
            "majority": calculate_matrix(majority_projected, projected_masks),
        }
        ensure_result_ordering(result)
        result["max_total_events"] = max(
            (cell["total_events"] for mode in (result["iupac"], result["majority"]) for row in mode["cells"] for cell in row),
            default=0,
        )
        (config.cluster.output_root / artifacts[ARTIFACT_RESULT]).write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        for mode, counts_key, details_key in (
            ("iupac", ARTIFACT_IUPAC_COUNTS, ARTIFACT_IUPAC_DETAILS),
            ("majority", ARTIFACT_MAJORITY_COUNTS, ARTIFACT_MAJORITY_DETAILS),
        ):
            (config.cluster.output_root / artifacts[counts_key]).write_text(_matrix_tsv(result[mode], details=False), encoding="utf-8")
            (config.cluster.output_root / artifacts[details_key]).write_text(_matrix_tsv(result[mode], details=True), encoding="utf-8")
    except Exception as exc:
        error_text = str(exc)

    connection = connect(config.database.path)
    try:
        update_cluster_job_status(
            connection, job_id, "failed" if error_text else "completed", completed_at=utc_now(),
            error_text=error_text, artifacts_json=artifacts_json,
        )
    finally:
        connection.close()
