import tempfile
import unittest
from pathlib import Path

from virtitta.cluster import _read_single_fasta_record
from virtitta.distance import parse_coverage_bed, pair_distance
from virtitta.outputs import effective_output_relname
from virtitta.app import build_resistance_cells


class ConsensusContractTests(unittest.TestCase):
    def test_analytical_majority_is_preferred_without_changing_igv_reference(self):
        outputs = {"main_fasta": "S.fasta", "analytical_majority_fasta": "S-majority.fasta"}
        self.assertEqual(effective_output_relname("export_fasta", outputs), "S-majority.fasta")
        self.assertEqual(effective_output_relname("main_fasta", outputs), "S.fasta")
        self.assertEqual(effective_output_relname("export_fasta", {"main_fasta": "old.fasta"}), "old.fasta")

    def test_description_bearing_header_does_not_break_coverage_mask(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root/"S.fasta").write_text(">S old=provenance\nANGT\n")
            (root/"S.bed").write_text("S\t0\t4\n")
            identifier, sequence = _read_single_fasta_record(root/"S.fasta")
            mask = parse_coverage_bed(root/"S.bed", identifier, len(sequence))
            distance = pair_distance(sequence, "ACGT", mask, mask)
            self.assertEqual(distance.compared_bases, 3)
            self.assertEqual(distance.total_events, 0)

    def test_low_depth_warning_does_not_replace_resistance_or_susceptibility(self):
        for outcome, status in (("resistant", "resistant"), ("susceptible", "clear")):
            sample = {"resistance": {"analysis_present": True, "schema_version": "3.1",
                "by_drug": [{"drug": "Daclatasvir", "outcome": outcome,
                             "warnings": ["Low-depth support: NS5A:93 depths=1/6/7"],
                             "low_depth_positions": ["NS5A:93"]}]}}
            cell = next(c for c in build_resistance_cells(sample) if c["drug"] == "Daclatasvir")
            self.assertEqual(cell["status"], status)
            self.assertIn("Low-depth support", cell["title"])
            self.assertTrue(cell["low_depth"])
