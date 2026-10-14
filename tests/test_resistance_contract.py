import unittest
from virtitta.app import build_resistance_cells, resistance_summary_text
from virtitta.resistance import drug_record


class ResistanceContractTests(unittest.TestCase):
    def test_all_outcomes_are_distinct_and_null_prediction_is_not_unknown(self):
        expected = {"resistant": "resistant", "reduced susceptibility": "warning",
                    "susceptible": "clear", "susceptible with scored substitutions": "scored",
                    "not assessed": "unassessed", "not licensed": "unlicensed"}
        for outcome, status in expected.items():
            with self.subTest(outcome=outcome):
                self.assertEqual(drug_record({"outcome": outcome, "prediction": None})["status"], status)

    def test_legacy_mutations_and_predictions_survive(self):
        record = drug_record({"prediction": "resistant", "mutations": ["NS5A:Y93H"]})
        self.assertEqual(record["mutations"], ["NS5A:Y93H"])
        self.assertEqual(record["status"], "resistant")

    def test_partial_compound_is_not_resistance_and_missing_drug_is_not_clear(self):
        sample = {"resistance": {"schema_version": "3.0", "analysis_present": True,
                   "has_resistance": False, "by_drug": [{"drug": "Daclatasvir",
                   "outcome": "not assessed", "warnings": ["Incomplete compound rule"]}]}}
        cells = {row["drug"]: row for row in build_resistance_cells(sample)}
        self.assertEqual(cells["Daclatasvir"]["status"], "unassessed")
        self.assertIn("Incomplete compound rule", cells["Daclatasvir"]["title"])
        self.assertEqual(cells["Sofosbuvir"]["status"], "missing")
        self.assertEqual(resistance_summary_text(sample), "Resistance not fully assessed")
