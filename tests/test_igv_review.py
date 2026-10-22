import asyncio
import copy
import json
import unittest
from urllib.parse import parse_qs, urlsplit

from fastapi import HTTPException
from virtitta.app import (build_igv_url, build_webigv_browser_config, build_resistance_mutations,
                          create_app, resolve_webigv_output_file, WEBIGV_ALLOWED_OUTPUT_KEYS)
from virtitta.config import load_config
from virtitta.igv_review import REVIEW_KEYS, select_review
from virtitta.importer import import_run
from virtitta.outputs import required_sidecars
from virtitta.repository import connect, get_sample, update_qc_status
import test_smoke as smoke


class IgvReviewTests(unittest.TestCase):
    setUp = smoke.VirtittaSmokeTests.setUp
    tearDown = smoke.VirtittaSmokeTests.tearDown
    make_request = smoke.VirtittaSmokeTests.make_request
    write_run_summaries = smoke.VirtittaSmokeTests.write_run_summaries
    write_sample_summary = smoke.VirtittaSmokeTests.write_sample_summary
    enable_webigv = smoke.VirtittaSmokeTests.enable_webigv

    def bundle(self):
        self.enable_webigv()
        raw = json.loads(smoke.FIXTURE_PATH.read_text())[0]
        outputs = raw["outputs"]
        mapped = {}
        for old, key in REVIEW_KEYS.items():
            if old == "main_fasta":
                name = "SAMPLE001-0.15-iupac.fasta"
            elif old == "main_fasta_index":
                name = "SAMPLE001-0.15-iupac.fasta.fai"
            elif old == "main_cram":
                name = "SAMPLE001-0.15-iupac.cram"
            elif old == "main_cram_index":
                name = "SAMPLE001-0.15-iupac.cram.crai"
            else:
                name = "SAMPLE001-0.15-iupac.review."+old
                if old.startswith("filtered_vcf"):
                    name += ".csi" if old.endswith("_index") else ".vcf.gz"
            mapped[key] = name
            (self.sample_dir / name).write_text("display fixture")
        outputs.update(mapped)
        outputs["igv_review_manifest"] = "SAMPLE001-0.15-iupac.review.json"
        raw["igv_review"] = {
            "schema_version": "1.0", "view": "analytical_iupac", "sample_id": "SAMPLE001",
            "source_reference": {"contig": "SAMPLE001"},
            "display_reference": {"contig": "SAMPLE001-0.15-iupac"},
            "outputs": mapped,
        }
        (self.sample_dir / outputs["igv_review_manifest"]).write_text(json.dumps(raw["igv_review"]))
        self.write_run_summaries([raw])
        config = load_config(self.config_path)
        self.assertEqual(import_run(config, self.run_dir), 1)
        conn = connect(config.database.path)
        try:
            sample = get_sample(conn, "SAMPLE001_fixture_run")
        finally:
            conn.close()
        return config, sample, raw

    def test_desktop_and_web_select_same_coherent_bundle(self):
        config, sample, raw = self.bundle()
        query = parse_qs(urlsplit(build_igv_url(config, sample)).query)
        self.assertTrue(query["genome"][0].endswith("SAMPLE001-0.15-iupac.fasta"))
        paths = query["file"][0].split(",")
        names = query["name"][0].split(",")
        self.assertEqual(len(paths), len(names))
        self.assertEqual(sum("Scaffold-derived variants" in name for name in names), 6)
        self.assertTrue(any(path.endswith("SAMPLE001-0.15-iupac.cram") for path in paths))
        self.assertFalse(any(path.endswith("/SAMPLE001.cram") for path in paths))
        app = create_app(self.config_path)
        browser = build_webigv_browser_config(config, self.make_request(app), sample, locus="SAMPLE001:20-22")
        self.assertIn("/igv_iupac_fasta/", browser["reference"]["fastaURL"])
        self.assertEqual(browser["locus"], "SAMPLE001-0.15-iupac:20-22")
        variants = [t for t in browser["tracks"] if t["type"] == "variant"]
        self.assertEqual(len(variants), 6)
        self.assertTrue(all(t["name"].startswith("Scaffold-derived variants") for t in variants))
        alignment = next(t for t in browser["tracks"] if t["type"] == "alignment")
        self.assertFalse(alignment["checkSequenceMD5"])
        self.assertIn("/igv_iupac_cram/", alignment["url"])
        self.assertIn("/igv_iupac_cram_index/", alignment["indexURL"])
        self.assertIn("igv_iupac_resistance_sites_gff3", WEBIGV_ALLOWED_OUTPUT_KEYS)
        for key in REVIEW_KEYS.values():
            path, name = resolve_webigv_output_file(config, sample, key)
            self.assertTrue(path.is_file())
            self.assertEqual(name, raw["outputs"][key])

    def test_incomplete_bundle_never_falls_back(self):
        config, sample, raw = self.bundle()
        (self.sample_dir / raw["outputs"]["igv_iupac_resistance_sites_gff3"]).unlink()
        with self.assertRaises(HTTPException) as error:
            build_igv_url(config, sample)
        self.assertEqual(error.exception.status_code, 409)
        app = create_app(self.config_path)
        with self.assertRaises(HTTPException):
            build_webigv_browser_config(config, self.make_request(app), sample)
        broken = copy.deepcopy(raw["outputs"])
        del broken["igv_iupac_cram_index"]
        with self.assertRaises(ValueError):
            select_review(broken, raw["igv_review"])
        with self.assertRaises(ValueError):
            required_sidecars(broken)
        with self.assertRaises(ValueError):
            select_review(raw["outputs"], None)

    def test_navigation_changes_only_display_contig(self):
        _, _, raw = self.bundle()
        raw["resistance"]["mutations"] = [{"genomic_start": 20, "genomic_end": 22, "drugs": []}]
        before = copy.deepcopy(raw)
        mutations = build_resistance_mutations(raw, "SAMPLE001")
        self.assertEqual(mutations[0]["locus"], "SAMPLE001-0.15-iupac:20-22")
        self.assertEqual(raw, before)

    def test_reimport_preserves_curator_state_and_original_downloads(self):
        config, _, raw = self.bundle()
        conn = connect(config.database.path)
        try:
            update_qc_status(conn, ["SAMPLE001_fixture_run"], "pass", "tester")
            conn.commit()
        finally:
            conn.close()
        self.assertEqual(import_run(config, self.run_dir), 1)
        conn = connect(config.database.path)
        try:
            sample = get_sample(conn, "SAMPLE001_fixture_run")
        finally:
            conn.close()
        self.assertEqual(sample["qc_status"], "pass")
        self.assertEqual(raw["outputs"]["main_fasta"], "SAMPLE001.fasta")
        self.assertEqual(raw["outputs"]["resistance_gff"], "SAMPLE001_resistance.gff")

    def test_legacy_archives_keep_scaffold_selection(self):
        view = select_review({"main_fasta": "OLD.fasta"})
        self.assertFalse(view["keys"])
        self.assertEqual(view["label"], "Legacy scaffold view")

    def test_review_file_route_supports_byte_ranges(self):
        config, sample, raw = self.bundle()
        app = create_app(self.config_path)
        route = next(r for r in app.routes if r.name == "sample_webigv_named_file")
        request = self.make_request(app)
        request.scope["headers"] = [(b"range", b"bytes=0-3")]
        key = "igv_iupac_cram"
        response = route.endpoint(request, sample["sample_run_id"], key, raw["outputs"][key])
        messages = []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            messages.append(message)

        asyncio.run(asyncio.wait_for(response(request.scope, receive, send), timeout=10))
        self.assertEqual(messages[0]["status"], 206)
        self.assertEqual(b"".join(m.get("body", b"") for m in messages), b"disp")
