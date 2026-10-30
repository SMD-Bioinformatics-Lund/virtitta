import copy
import json
import unittest

from virtitta.app import create_app
from virtitta.config import load_config
from virtitta.importer import import_run, _flatten_sample_record
from virtitta.repository import (connect, get_sample, get_comments, update_qc_status,
                                 add_comment, set_sample_category, add_samples_to_group,
                                 set_sample_field_overrides)
import test_smoke as smoke


def evidence(status="assigned", subtype="2b"):
    groups = []
    for index, label in enumerate(("2b", "1a", "3a", "4d", "5a", "6a")):
        references = [dict(reference=f"{label}_REF{n}", subtype=label,
                           identity=92.5 - index, aligned_bases=8000, query_coverage_pct=85.0,
                           observed_coverage_pct=95.0, bit_score=10000 - index * 100 - n)
                      for n in range(4)]
        groups.append(dict(subtype=label, bit_score=references[0]["bit_score"], references=references))
    return dict(schema_version="1.0", method="ordered-nonoverlapping-hsp-chain-v1",
                status=status, subtype=subtype, selected_reference=f"{subtype}_REF0" if subtype else None,
                groups=groups, score_margin=100, unrecognized_references=[],
                input={"role": "analytical_15pct_iupac", "sequence_sha256": "a" * 64},
                database={"db.nsq": "b" * 64})


class TypingContractTests(unittest.TestCase):
    setUp = smoke.VirtittaSmokeTests.setUp
    tearDown = smoke.VirtittaSmokeTests.tearDown
    make_request = smoke.VirtittaSmokeTests.make_request
    write_run_summaries = smoke.VirtittaSmokeTests.write_run_summaries
    write_sample_summary = smoke.VirtittaSmokeTests.write_sample_summary

    def import_evidence(self, result):
        raw = json.loads(smoke.FIXTURE_PATH.read_text())[0]
        raw["typing"]["evidence"] = result
        raw["typing"]["main_blast_genotype"] = "legacy_scaffold_call"
        raw["typing"]["main_blast_identity"] = 99.9
        raw["outputs"]["typing_json"] = "SAMPLE001_typing.json"
        (self.sample_dir / "SAMPLE001_typing.json").write_text(json.dumps(result))
        self.write_run_summaries([raw])
        config = load_config(self.config_path)
        self.assertEqual(import_run(config, self.run_dir), 1)
        connection = connect(config.database.path)
        try:
            sample = get_sample(connection, "SAMPLE001_fixture_run")
        finally:
            connection.close()
        return config, sample

    def detail(self):
        app = create_app(self.config_path)
        route = next(r for r in app.router.routes if getattr(r, "name", None) == "sample_detail")
        return route.endpoint(self.make_request(app), "SAMPLE001_fixture_run")

    def test_import_prefers_authoritative_subtype_and_chain_identity(self):
        _, sample = self.import_evidence(evidence())
        self.assertEqual(sample["typing_report_subtype"], "2b")
        self.assertEqual(sample["typing_main_blast_identity"], 92.5)
        self.assertEqual(json.loads(sample["raw_json"])["typing"]["main_blast_identity"], 99.9)
        response = self.detail()
        html = response.body.decode()
        self.assertIn("8000 aligned bases", html)
        self.assertIn("Typing evidence", html)
        self.assertTrue(any(link["key"] == "typing_json" for link in response.context["detail_links"]))

    def test_unknown_assignment_does_not_fall_back_and_is_visible(self):
        for status in ("ambiguous", "unassigned"):
            with self.subTest(status=status):
                _, sample = self.import_evidence(evidence(status, None))
                self.assertIsNone(sample["typing_report_subtype"])
                self.assertIsNone(sample["typing_main_blast_identity"])
                response = self.detail()
                self.assertIn(status.capitalize(), response.body.decode())
                app = create_app(self.config_path)
                route = next(r for r in app.router.routes if getattr(r, "name", None) == "index")
                listing = route.endpoint(self.make_request(app), search="", run_name="", subtype="",
                                         qc_status="", min_coverage_pct="", min_mean_depth="",
                                         min_blast_identity="", max_ct="", sort="run_name", desc=True)
                self.assertIn(status.capitalize(), listing.body.decode())

    def test_legacy_import_and_detail_remain_unchanged(self):
        config = load_config(self.config_path)
        import_run(config, self.run_dir)
        connection = connect(config.database.path)
        try:
            sample = get_sample(connection, "SAMPLE001_fixture_run")
        finally:
            connection.close()
        raw = json.loads(sample["raw_json"])
        self.assertEqual(sample["typing_report_subtype"], raw["typing"]["main_blast_genotype"])
        self.assertEqual(sample["typing_main_blast_identity"], raw["typing"]["main_blast_identity"])
        self.assertNotIn('id="typing-evidence"', self.detail().body.decode())

    def test_alternative_limits_and_descriptive_score_labels(self):
        self.import_evidence(evidence())
        section = self.detail().body.decode().split('id="typing-evidence"', 1)[1].split("</section>", 1)[0]
        self.assertIn("5a_REF0", section)
        self.assertNotIn("6a_REF0", section)
        self.assertIn("2b_REF2", section)
        self.assertNotIn("2b_REF3", section)
        self.assertIn("not confidence probabilities", section)
        self.assertIn("Observed coverage %", section)

    def test_reimport_preserves_all_review_fields_and_override(self):
        config, _ = self.import_evidence(evidence())
        sample_id = "SAMPLE001_fixture_run"
        connection = connect(config.database.path)
        try:
            update_qc_status(connection, [sample_id], "pass")
            add_comment(connection, sample_id, "Retain review", "curator")
            set_sample_category(connection, [sample_id], "EQA")
            add_samples_to_group(connection, [sample_id], "review-group")
            set_sample_field_overrides(connection, sample_id, {"typing_report_subtype": "1b"})
        finally:
            connection.close()
        self.import_evidence(evidence("unassigned", None))
        connection = connect(config.database.path)
        try:
            sample = get_sample(connection, sample_id)
            comments = get_comments(connection, sample_id)
        finally:
            connection.close()
        self.assertEqual(sample["typing_report_subtype"], "1b")
        self.assertIsNone(sample["imported_typing_report_subtype"])
        self.assertEqual(sample["qc_status"], "pass")
        self.assertEqual(sample["sample_category"], "EQA")
        self.assertEqual(sample["manual_groups"], "review-group")
        self.assertEqual(len(comments), 1)
        self.assertIn("Retain review", comments[0]["body"])
        self.assertIn("Unassigned", self.detail().body.decode())

    def test_bad_declared_evidence_never_becomes_a_legacy_call(self):
        raw = json.loads(smoke.FIXTURE_PATH.read_text())[0]
        invalid = [None, {"schema_version": "99"}, evidence("unassigned", "2b"),
                   evidence("assigned", None), evidence("genotype_only", "2b")]
        missing = evidence()
        missing["selected_reference"] = "missing"
        invalid.append(missing)
        bad_metric = evidence()
        bad_metric["groups"][0]["references"][0]["identity"] = float("nan")
        invalid.append(bad_metric)
        bad_source = evidence()
        bad_source["input"]["role"] = "scaffold"
        invalid.append(bad_source)
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                sample = copy.deepcopy(raw)
                sample["typing"]["evidence"] = value
                _flatten_sample_record(sample, root_name="fixture", sample_results_relpath=self.sample_dir)

    def test_genotype_only_is_not_converted_to_subtype(self):
        result = evidence("genotype_only", "1")
        result["groups"][0]["subtype"] = "1"
        result["groups"][0]["references"][0]["reference"] = "1_REF0"
        _, sample = self.import_evidence(result)
        self.assertEqual(sample["typing_report_subtype"], "1")
        self.assertIn("Genotype only: 1", self.detail().body.decode())
