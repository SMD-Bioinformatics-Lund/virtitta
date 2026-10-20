import json
import unittest
from virtitta.app import build_resistance_cells, resistance_summary_text
from virtitta.cluster import _metadata_value, _resistance_metadata_value
from virtitta.resistance import HCV_RESISTANCE_DRUGS, drug_record


class ResistanceContractTests(unittest.TestCase):
    def test_tree_metadata_and_sample_summary_share_outcome_semantics(self):
        def sample(outcome="susceptible"):
            return {"resistance": {"schema_version": "3.1", "analysis_present": True,
                    "has_resistance": False, "by_drug": [
                        {"drug": drug, "outcome": outcome} for drug, _ in HCV_RESISTANCE_DRUGS]}}

        cases = [({}, "No resistance data"),
                 (sample(), "No resistance detected"),
                 (sample("susceptible with scored substitutions"), "No resistance detected"),
                 (sample("not licensed"), "No resistance detected"),
                 (sample("not assessed"), "Resistance not fully assessed"),
                 (sample("unrecognized outcome"), "Resistance not fully assessed"),
                 ({"resistance": {"analysis_present": True, "schema_version": "3.1",
                                   "by_drug": []}}, "Resistance not fully assessed"),
                 ({"resistance": {"analysis_present": True, "has_resistance": False}},
                  "No resistance detected"),
                 ({"resistance": {"analysis_present": True, "has_resistance": True,
                                   "mutation_count": 2}}, "2 mutations")]
        for outcome in ("resistant", "reduced susceptibility"):
            positive = sample()
            positive["resistance"]["has_resistance"] = True
            next(row for row in positive["resistance"]["by_drug"]
                 if row["drug"] == "Daclatasvir")["outcome"] = outcome
            cases.append((positive, "DCV"))
        legacy = {"resistance": {"analysis_present": True, "has_resistance": True,
                  "by_drug": [{"drug": "Daclatasvir", "prediction": "resistant",
                               "mutations": ["NS5A:Y93H"]}]}}
        cases.append((legacy, "DCV"))
        for record, expected in cases:
            with self.subTest(record=record):
                self.assertEqual(resistance_summary_text(record), expected)
                self.assertEqual(_resistance_metadata_value(record), expected)
                self.assertEqual(_metadata_value(None, {"raw_json": json.dumps(record)},
                                                 "resistance_summary"), expected)

    def test_missing_drug_is_not_clear_and_positive_call_survives_partial_assessment(self):
        sample = {"resistance": {"schema_version": "3.1", "analysis_present": True,
                   "has_resistance": False, "by_drug": [{"drug": "Daclatasvir",
                   "outcome": "susceptible"}]}}
        self.assertEqual(_resistance_metadata_value(sample), "Resistance not fully assessed")
        sample["resistance"]["by_drug"][0]["outcome"] = "resistant"
        self.assertEqual(_resistance_metadata_value(sample), "DCV")

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
