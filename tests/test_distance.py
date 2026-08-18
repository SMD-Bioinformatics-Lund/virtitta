import unittest
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from tempfile import TemporaryDirectory
from unittest.mock import patch

from virtitta.cluster import ClusterError
from virtitta.distance import (
    _historical_mask,
    _matrix_tsv,
    add_upgma_ordering,
    calculate_matrix,
    pair_distance,
    parse_coverage_bed,
    project_mask,
    project_sequence,
)


def matrix_cell(total_events, compared_bases=100):
    return {
        "substitutions": total_events,
        "indel_events": 0,
        "total_events": total_events,
        "compared_bases": compared_bases,
    }


class PairDistanceTests(unittest.TestCase):
    def test_iupac_overlap_and_disjoint_ambiguity(self):
        value = pair_distance("ARY", "AGG")
        self.assertEqual(value.substitutions, 1)
        self.assertEqual(value.compared_bases, 3)

    def test_unknown_bases_are_skipped_and_lowercase_is_compared(self):
        value = pair_distance("ANcT", "ATGT")
        self.assertEqual(value.substitutions, 1)
        self.assertEqual(value.compared_bases, 3)

    def test_terminal_gaps_are_ignored(self):
        value = pair_distance("--ACGT--", "TTACGTTT")
        self.assertEqual(value.indel_events, 0)
        self.assertEqual(value.compared_bases, 4)

    def test_internal_gap_block_is_one_event(self):
        value = pair_distance("AC---GT", "ACTTTGT")
        self.assertEqual(value.indel_events, 1)
        self.assertEqual(value.total_events, 1)

    def test_partially_uncovered_gap_block_is_skipped(self):
        value = pair_distance("AC---GT", "ACTTTGT", [True] * 7, [True, True, False, True, True, True, True])
        self.assertEqual(value.indel_events, 0)

    def test_gap_block_without_covered_flanks_is_skipped(self):
        value = pair_distance("AC---GT", "ACTTTGT", [True, False, False, False, False, True, True], [True] * 7)
        self.assertEqual(value.indel_events, 0)

    def test_masks_control_compared_bases(self):
        value = pair_distance("acGT", "ATGT", [True, False, True, True], [True] * 4)
        self.assertEqual(value.substitutions, 0)
        self.assertEqual(value.compared_bases, 3)

    def test_bed_validation_and_projection(self):
        with TemporaryDirectory() as directory:
            bed = Path(directory) / "mask.bed"
            bed.write_text("ref\t0\t2\nref\t4\t5\n", encoding="utf-8")
            self.assertEqual(parse_coverage_bed(bed, "ref", 5), [True, True, False, False, True])
            self.assertEqual(project_mask("A-CGT", [True, False, True, True]), [True, False, False, True, True])
            bed.write_text("ref\t2\t4\nref\t3\t5\n", encoding="utf-8")
            with self.assertRaisesRegex(ClusterError, "overlap"):
                parse_coverage_bed(bed, "ref", 5)

    def test_historical_mask_cache_uses_and_invalidates_source_signature(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            paths = {key: root / key for key in ("main_fasta", "main_fasta_index", "main_cram", "main_cram_index")}
            paths["main_fasta"].write_text(">ref\nACGT\n", encoding="utf-8")
            for key in ("main_fasta_index", "main_cram", "main_cram_index"):
                paths[key].write_text(key, encoding="utf-8")
            config = SimpleNamespace(
                cache=SimpleNamespace(outputs_root=root / "cache"),
                cluster=SimpleNamespace(samtools_command="samtools", timeout_seconds=10),
            )
            calls = []

            def fake_run(argv, **kwargs):
                calls.append(argv)
                kwargs["stdout_path"].write_text("ref\t1\t1\nref\t2\t0\nref\t3\t2\nref\t4\t1\n", encoding="utf-8")

            with (
                patch("virtitta.distance._distance_output_file", side_effect=lambda config, row, key: paths[key]),
                patch("virtitta.distance._run_command", side_effect=fake_run),
            ):
                bed, first = _historical_mask(config, None, {"sample_run_id": "sample_run"}, StringIO())
                self.assertEqual(bed.read_text(encoding="utf-8"), "ref\t0\t1\nref\t2\t4\n")
                _, second = _historical_mask(config, None, {"sample_run_id": "sample_run"}, StringIO())
                paths["main_cram"].write_text("changed", encoding="utf-8")
                _, third = _historical_mask(config, None, {"sample_run_id": "sample_run"}, StringIO())
            self.assertEqual(len(calls), 2)
            self.assertEqual(
                calls[0],
                ["samtools", "depth", "-aa", "--reference", str(paths["main_fasta"]), str(paths["main_cram"])],
            )
            self.assertEqual((first["source"], second["source"], third["source"]), ("derived", "derived_cache", "derived"))

    def test_matrix_is_symmetric(self):
        matrix = calculate_matrix([("one", "AC-G"), ("two", "ACTG"), ("three", "ATTG")])
        self.assertEqual(matrix["cells"][0][2], matrix["cells"][2][0])
        self.assertEqual(matrix["cells"][0][1], matrix["cells"][1][0])

    def test_upgma_uses_average_linkage_and_returns_identifier_order(self):
        matrix = {
            "identifiers": ["A", "B", "C"],
            "cells": [
                [matrix_cell(0), matrix_cell(5), matrix_cell(1)],
                [matrix_cell(5), matrix_cell(0), matrix_cell(4)],
                [matrix_cell(1), matrix_cell(4), matrix_cell(0)],
            ],
        }

        add_upgma_ordering(matrix)

        self.assertEqual(matrix["ordering"]["identifiers"], ["A", "C", "B"])
        self.assertEqual(matrix["ordering"]["method"], "UPGMA")
        self.assertEqual(matrix["ordering"]["metric"], "total_events")

    def test_upgma_penalizes_zero_overlap_and_is_deterministic_for_ties(self):
        matrix = {
            "identifiers": ["A", "B", "C"],
            "cells": [
                [matrix_cell(0), matrix_cell(0, 0), matrix_cell(2)],
                [matrix_cell(0, 0), matrix_cell(0), matrix_cell(2)],
                [matrix_cell(2), matrix_cell(2), matrix_cell(0)],
            ],
        }

        first = add_upgma_ordering(matrix)["ordering"]
        second = add_upgma_ordering(matrix)["ordering"]

        self.assertEqual(first, second)
        self.assertEqual(first["zero_compared_penalty"], 3)
        self.assertEqual(first["identifiers"], ["A", "C", "B"])

    def test_tsv_marks_off_diagonal_zero_overlap_as_unavailable(self):
        matrix = {
            "identifiers": ["A", "B"],
            "cells": [
                [matrix_cell(0, 0), matrix_cell(0, 0)],
                [matrix_cell(0, 0), matrix_cell(0, 0)],
            ],
        }

        self.assertEqual(_matrix_tsv(matrix, details=False), "ID\tA\tB\nA\t0\t-1\nB\t-1\t0\n")
        self.assertIn("A\t0 events (0 substitutions + 0 indel); 0 bases compared\tNo bases compared", _matrix_tsv(matrix, details=True))

    def test_invalid_alignment_mapping_fails(self):
        with self.assertRaisesRegex(ClusterError, "cannot be mapped"):
            project_sequence("AC-G", "AC")
        with self.assertRaisesRegex(ClusterError, "cannot be mapped"):
            project_sequence("AT-G", "ACG")


if __name__ == "__main__":
    unittest.main()
