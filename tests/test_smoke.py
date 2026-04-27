from __future__ import annotations

import asyncio
import json
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from starlette.requests import Request

from virtitta.app import (
    build_fasta_clipboard_content,
    build_igv_goto_url,
    build_igv_url,
    build_lims_export_content,
    build_resistance_cells,
    build_resistance_mutations,
    resistance_tooltip_text,
    cell_style,
    comment_link_label,
    create_app,
    format_value,
    override_comment_text,
)
from virtitta.cli import build_parser
from virtitta.config import load_config
from virtitta.importer import import_run, import_sample
from virtitta.repository import (
    add_comment,
    add_samples_to_group,
    backfill_variant_af_counts,
    connect,
    get_comments,
    get_sample,
    init_db,
    list_manual_groups,
    list_runs,
    list_samples,
    list_stored_sample_categories,
    remove_samples_from_group,
    set_sample_category,
    set_sample_field_overrides,
    update_qc_status,
)


FIXTURE_PATH = Path("/home/jonas/git/virpipa/assets/test_data/qc_summary/qc_summary.json")


def write_test_config(config_path: Path, *, root: Path, db_path: Path) -> None:
    config_path.write_text(
        "\n".join(
            [
                "[app]",
                'title = "Virtitta Test"',
                "",
                "[database]",
                f'path = "{db_path.as_posix()}"',
                "",
                "[exports]",
                f'lims_root = "{(root / "lims_exports").as_posix()}"',
                "",
                "[igv]",
                "enabled = true",
                'base_url = "http://localhost:60151/load"',
                "",
                "[features]",
                "comments = true",
                "bulk_qc = true",
                "igv = true",
                "",
                "[annotations]",
                'sample_categories = ["production", "validation", "EQA"]',
                "",
                "[[results_roots]]",
                'name = "test"',
                f'linux_path = "{root.as_posix()}"',
                'windows_path = "Q:/virtitta-test"',
            ]
        )
        + "\n",
        encoding="utf-8",
    )


class VirtittaSmokeTests(unittest.TestCase):
    def write_sample_summary(self, sample: dict) -> None:
        sample_id = sample["sample_id"]
        summary_path = self.run_dir / sample_id / "results" / f"{sample_id}_qc_summary.json"
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        summary_path.write_text(json.dumps(sample), encoding="utf-8")

    def write_run_summaries(self, samples: list[dict]) -> None:
        for summary_path in self.run_dir.glob("*/results/*_qc_summary.json"):
            summary_path.unlink()
        for sample in samples:
            self.write_sample_summary(sample)

    def write_clarity_sample_info(self, entries: dict[str, dict]) -> Path:
        clarity_path = self.tmp_path / "clarity_sample_info.json"
        clarity_path.write_text(json.dumps(entries), encoding="utf-8")
        return clarity_path

    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        self.tmp_path = Path(self.temp_dir.name)
        self.root = self.tmp_path / "results_root"
        self.run_dir = self.root / "fixture_run"
        self.sample_dir = self.run_dir / "SAMPLE001" / "results"
        self.sample_dir.mkdir(parents=True)

        fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        fixture[0]["generated_at_utc"] = "2026-04-08T09:12:34Z"
        self.write_run_summaries([fixture[0]])

        for filename in [
            "SAMPLE001_rug_kde_plot.png",
            "SAMPLE001.fasta",
            "SAMPLE001.cram",
            "SAMPLE001-pilon-m0.05.vcf.gz",
            "SAMPLE001-pilon-m0.1.vcf.gz",
            "SAMPLE001-pilon-m0.15.vcf.gz",
            "SAMPLE001-pilon-m0.2.vcf.gz",
            "SAMPLE001-pilon-m0.3.vcf.gz",
            "SAMPLE001-pilon-m0.4.vcf.gz",
            "SAMPLE001.vadr.bed",
            "SAMPLE001_resistance.gff",
            "SAMPLE001.vadr.pass_mod.gff",
        ]:
            (self.sample_dir / filename).write_text("placeholder", encoding="utf-8")
        (self.sample_dir / "lid").mkdir(parents=True)
        (self.sample_dir / "lid" / "LID001-2limsrs.txt").write_text(
            "sample_id\tparameter_name\tparameter_value\tcomment\n"
            "LID001\thcvtyp\tHCV genotyp 3a\t\n",
            encoding="utf-8",
        )
        (self.sample_dir / "lid" / "LID001.fasta").write_text(
            ">LID001\nACGT\n",
            encoding="utf-8",
        )
        (self.sample_dir / "lid" / "LID001-0.15-iupac.fasta").write_text(
            ">LID001-0.15-iupac\nARYT\n",
            encoding="utf-8",
        )

        self.config_path = self.tmp_path / "virtitta.toml"
        self.db_path = self.tmp_path / "virtitta.sqlite3"
        write_test_config(self.config_path, root=self.root, db_path=self.db_path)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_import_run_populates_database(self) -> None:
        config = load_config(self.config_path)
        imported = import_run(config, self.run_dir)
        self.assertEqual(imported, 1)

        conn = sqlite3.connect(config.database.path)
        try:
            run = conn.execute("SELECT run_name, sample_count FROM runs").fetchone()
            sample = conn.execute(
                """
                SELECT sample_run_id, sample_results_relpath, sequencing_date, generated_date,
                       variant_af_count_005, variant_af_count_01, variant_af_count_015,
                       variant_af_count_02, variant_af_count_03, variant_af_count_04
                FROM samples
                """
            ).fetchone()
        finally:
            conn.close()

        self.assertEqual(tuple(run), ("fixture_run", 1))
        self.assertEqual(
            tuple(sample),
            (
                "SAMPLE001_fixture_run",
                "fixture_run/SAMPLE001/results",
                "2026-04-08",
                "2026-04-08",
                236,
                210,
                179,
                151,
                106,
                92,
            ),
        )

    def test_import_run_derives_sequencing_date_from_run_name_prefix(self) -> None:
        run_name = "260317_A00681_1225_AHJMKLDRX7"
        run_dir = self.root / run_name
        fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        sample = fixture[0]
        sample["run_name"] = run_name
        sample["sample_run_id"] = f"{sample['sample_id']}_{run_name}"
        sample["generated_at_utc"] = "2026-04-08T09:12:34Z"
        summary_path = run_dir / sample["sample_id"] / "results" / f"{sample['sample_id']}_qc_summary.json"
        summary_path.parent.mkdir(parents=True)
        summary_path.write_text(json.dumps(sample), encoding="utf-8")

        config = load_config(self.config_path)
        import_run(config, run_dir)

        conn = connect(config.database.path)
        try:
            stored = get_sample(conn, f"SAMPLE001_{run_name}")
        finally:
            conn.close()

        self.assertIsNotNone(stored)
        self.assertEqual(stored["sequencing_date"], "2026-03-17")
        self.assertEqual(stored["generated_date"], "2026-04-08")

    def test_init_db_backfills_sequencing_date_for_existing_rows(self) -> None:
        legacy_db_path = self.tmp_path / "legacy.sqlite3"
        conn = sqlite3.connect(legacy_db_path)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute(
                """
                CREATE TABLE samples (
                    sample_run_id TEXT PRIMARY KEY,
                    run_name TEXT NOT NULL,
                    generated_date TEXT,
                    sample_id TEXT NOT NULL,
                    lid TEXT,
                    source_root_name TEXT,
                    sample_results_relpath TEXT NOT NULL,
                    typing_report_subtype TEXT,
                    typing_main_blast_identity REAL,
                    host_filter_reads_in INTEGER,
                    host_filter_reads_removed_proportion REAL,
                    qc_coverage_pct REAL,
                    qc_mean_depth REAL,
                    qc_coverage_1x_pct REAL,
                    qc_coverage_10x_pct REAL,
                    qc_coverage_100x_pct REAL,
                    qc_coverage_1000x_pct REAL,
                    sample_metadata_ct REAL,
                    sample_metadata_library_concentration_ng_ul REAL,
                    sample_metadata_library_fragment_length_bp INTEGER,
                    raw_json TEXT NOT NULL,
                    imported_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                INSERT INTO samples (
                    sample_run_id, run_name, generated_date, sample_id,
                    sample_results_relpath, raw_json, imported_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "SAMPLE001_260317_run",
                    "260317_run",
                    "2026-04-08",
                    "SAMPLE001",
                    "260317_run/SAMPLE001/results",
                    "{}",
                    "2026-04-08T09:12:34Z",
                ),
            )
            conn.execute(
                """
                INSERT INTO samples (
                    sample_run_id, run_name, generated_date, sample_id,
                    sample_results_relpath, raw_json, imported_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "FAILED001_manual_failed_samples",
                    "manual_failed_samples",
                    "2026-04-09",
                    "FAILED001",
                    "manual_failed_samples/FAILED001/results",
                    "{}",
                    "2026-04-09T09:12:34Z",
                ),
            )
            init_db(conn)
            rows = {
                row["sample_run_id"]: row["sequencing_date"]
                for row in conn.execute("SELECT sample_run_id, sequencing_date FROM samples").fetchall()
            }
        finally:
            conn.close()

        self.assertEqual(rows["SAMPLE001_260317_run"], "2026-03-17")
        self.assertEqual(rows["FAILED001_manual_failed_samples"], "2026-04-09")

    def test_import_sample_adds_failed_sample_without_qc_summary(self) -> None:
        clarity_path = self.write_clarity_sample_info(
            {
                "sample_1": {
                    "clarity_sample_id": "FAILED001",
                    "CT": 31.2,
                    "Library concentration (ng/ul)": 1.7,
                }
            }
        )

        config = load_config(self.config_path)
        sample_run_id = import_sample(
            config,
            "FAILED001",
            "LIDFAIL",
            run_dir=self.run_dir,
            clarity_sample_info_path=clarity_path,
        )
        self.assertEqual(sample_run_id, "FAILED001_fixture_run")

        conn = connect(config.database.path)
        try:
            sample = get_sample(conn, "FAILED001_fixture_run")
            run = conn.execute("SELECT run_name, sample_count FROM runs").fetchone()
        finally:
            conn.close()

        self.assertIsNotNone(sample)
        self.assertEqual(tuple(run), ("fixture_run", 1))
        self.assertEqual(sample["lid"], "LIDFAIL")
        self.assertEqual(sample["sample_results_relpath"], "fixture_run/FAILED001/results")
        self.assertEqual(sample["sequencing_date"], sample["generated_date"])
        self.assertIsNotNone(sample["generated_date"])
        self.assertIsNone(sample["qc_coverage_pct"])
        self.assertEqual(sample["sample_metadata_ct"], 31.2)
        self.assertEqual(sample["sample_metadata_library_concentration_ng_ul"], 1.7)
        raw = json.loads(sample["raw_json"])
        self.assertEqual(raw["analysis_status"], "failed")
        self.assertEqual(sample["generated_date"], raw["generated_at_utc"][:10])
        self.assertFalse(raw["resistance"]["analysis_present"])

    def test_import_run_keeps_manual_failed_samples_in_run_count(self) -> None:
        config = load_config(self.config_path)
        import_sample(config, "FAILED001", "LIDFAIL", run_dir=self.run_dir)
        import_run(config, self.run_dir)

        conn = connect(config.database.path)
        try:
            run = conn.execute("SELECT run_name, sample_count FROM runs").fetchone()
            samples = list_samples(conn)
        finally:
            conn.close()

        self.assertEqual(tuple(run), ("fixture_run", 2))
        self.assertEqual(
            {sample["sample_run_id"] for sample in samples},
            {"FAILED001_fixture_run", "SAMPLE001_fixture_run"},
        )

    def test_import_sample_uses_cli_metadata_without_run_dir_or_clarity_json(self) -> None:
        config = load_config(self.config_path)
        sample_run_id = import_sample(
            config,
            "FAILED001",
            "LIDFAIL",
            ct=29.4,
            library_concentration_ng_ul=2.3,
        )
        self.assertEqual(sample_run_id, "FAILED001_manual_failed_samples")

        conn = connect(config.database.path)
        try:
            sample = get_sample(conn, "FAILED001_manual_failed_samples")
            run = conn.execute("SELECT run_name, sample_count FROM runs").fetchone()
        finally:
            conn.close()

        self.assertIsNotNone(sample)
        self.assertEqual(tuple(run), ("manual_failed_samples", 1))
        self.assertEqual(sample["sample_results_relpath"], "manual_failed_samples/FAILED001/results")
        self.assertEqual(sample["sequencing_date"], sample["generated_date"])
        self.assertIsNotNone(sample["generated_date"])
        self.assertEqual(sample["sample_metadata_ct"], 29.4)
        self.assertEqual(sample["sample_metadata_library_concentration_ng_ul"], 2.3)
        raw = json.loads(sample["raw_json"])
        self.assertEqual(sample["generated_date"], raw["generated_at_utc"][:10])
        self.assertEqual(raw["sample_metadata"]["ct"], 29.4)
        self.assertEqual(raw["sample_metadata"]["library_concentration_ng_ul"], 2.3)

    def test_import_run_merges_missing_metadata_from_clarity_sample_info(self) -> None:
        fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        fixture[0]["sample_metadata"] = {}
        self.write_run_summaries([fixture[0]])
        clarity_path = self.write_clarity_sample_info(
            {
                "sample_1": {
                    "clarity_sample_id": "SAMPLE001",
                    "CT": 24.8,
                    "Library concentration (ng/ul)": 5.6,
                    "Library fragment length (bp)": 387,
                }
            }
        )

        config = load_config(self.config_path)
        imported = import_run(config, self.run_dir, clarity_path)
        self.assertEqual(imported, 1)

        conn = connect(config.database.path)
        try:
            sample = get_sample(conn, "SAMPLE001_fixture_run")
        finally:
            conn.close()

        self.assertIsNotNone(sample)
        self.assertEqual(sample["sample_metadata_ct"], 24.8)
        self.assertEqual(sample["sample_metadata_library_concentration_ng_ul"], 5.6)
        self.assertEqual(sample["sample_metadata_library_fragment_length_bp"], 387)
        raw = json.loads(sample["raw_json"])
        self.assertEqual(raw["sample_metadata"]["ct"], 24.8)
        self.assertEqual(raw["sample_metadata"]["library_concentration_ng_ul"], 5.6)
        self.assertEqual(raw["sample_metadata"]["library_fragment_length_bp"], 387)

    def test_import_run_prefers_qc_summary_metadata_over_clarity_sample_info(self) -> None:
        clarity_path = self.write_clarity_sample_info(
            {
                "sample_1": {
                    "clarity_sample_id": "SAMPLE001",
                    "CT": 99.1,
                    "Library concentration (ng/ul)": 88.8,
                    "Library fragment length (bp)": 777,
                }
            }
        )

        config = load_config(self.config_path)
        import_run(config, self.run_dir, clarity_path)

        conn = connect(config.database.path)
        try:
            sample = get_sample(conn, "SAMPLE001_fixture_run")
        finally:
            conn.close()

        self.assertIsNotNone(sample)
        raw = json.loads(sample["raw_json"])
        self.assertNotEqual(raw["sample_metadata"]["ct"], 99.1)
        self.assertNotEqual(raw["sample_metadata"]["library_concentration_ng_ul"], 88.8)
        self.assertNotEqual(raw["sample_metadata"]["library_fragment_length_bp"], 777)

    def test_sample_field_overrides_survive_reimport_without_changing_imported_values(self) -> None:
        config = load_config(self.config_path)
        import_run(config, self.run_dir)

        conn = connect(config.database.path)
        try:
            imported_before = get_sample(conn, "SAMPLE001_fixture_run")
            changes = set_sample_field_overrides(
                conn,
                "SAMPLE001_fixture_run",
                {
                    "lid": "LIDOVERRIDE",
                    "sequencing_date": "2026-02-03",
                    "sample_metadata_ct": 19.8,
                    "sample_metadata_library_concentration_ng_ul": 4.4,
                    "typing_report_subtype": "2b",
                },
            )
            sample = get_sample(conn, "SAMPLE001_fixture_run")
            stored = conn.execute(
                """
                SELECT lid, sequencing_date, sample_metadata_ct, sample_metadata_library_concentration_ng_ul,
                       typing_report_subtype
                FROM samples
                WHERE sample_run_id = ?
                """,
                ("SAMPLE001_fixture_run",),
            ).fetchone()
        finally:
            conn.close()

        self.assertEqual(len(changes), 5)
        self.assertEqual(sample["lid"], "LIDOVERRIDE")
        self.assertEqual(sample["imported_lid"], "LID001")
        self.assertTrue(sample["lid_overridden"])
        self.assertEqual(sample["sequencing_date"], "2026-02-03")
        self.assertEqual(sample["sample_metadata_ct"], 19.8)
        self.assertEqual(sample["sample_metadata_library_concentration_ng_ul"], 4.4)
        self.assertEqual(sample["typing_report_subtype"], "2b")
        self.assertEqual(
            tuple(stored),
            (
                imported_before["lid"],
                imported_before["sequencing_date"],
                imported_before["sample_metadata_ct"],
                imported_before["sample_metadata_library_concentration_ng_ul"],
                imported_before["typing_report_subtype"],
            ),
        )

        import_run(config, self.run_dir)
        conn = connect(config.database.path)
        try:
            sample = get_sample(conn, "SAMPLE001_fixture_run")
            raw = json.loads(sample["raw_json"])
        finally:
            conn.close()

        self.assertEqual(sample["lid"], "LIDOVERRIDE")
        self.assertEqual(sample["sample_metadata_ct"], 19.8)
        self.assertEqual(raw["lid"], imported_before["lid"])
        self.assertEqual(raw["sample_metadata"]["ct"], imported_before["sample_metadata_ct"])

    def test_sample_field_overrides_are_used_for_listing_filters_and_lims_export(self) -> None:
        config = load_config(self.config_path)
        import_run(config, self.run_dir)

        conn = connect(config.database.path)
        try:
            set_sample_field_overrides(
                conn,
                "SAMPLE001_fixture_run",
                {
                    "lid": "LIDOVERRIDE",
                    "sample_metadata_ct": 19.8,
                    "typing_report_subtype": "2b",
                },
            )
            update_qc_status(conn, ["SAMPLE001_fixture_run"], "pass")
            rows_by_search = list_samples(conn, search="LIDOVERRIDE")
            rows_by_subtype = list_samples(conn, subtype="2b")
            rows_by_ct = list_samples(conn, max_ct=20.0)
            sample = get_sample(conn, "SAMPLE001_fixture_run")
        finally:
            conn.close()

        self.assertEqual([row["sample_run_id"] for row in rows_by_search], ["SAMPLE001_fixture_run"])
        self.assertEqual([row["sample_run_id"] for row in rows_by_subtype], ["SAMPLE001_fixture_run"])
        self.assertEqual([row["sample_run_id"] for row in rows_by_ct], ["SAMPLE001_fixture_run"])
        content = build_lims_export_content(config, [sample])
        self.assertIn("LIDOVERRIDE\thcvtyp\tHCV genotyp 2b\t", content)
        self.assertIn("LIDOVERRIDE\thcvqc\tPassed\t", content)

    def test_setting_override_to_imported_value_clears_override(self) -> None:
        config = load_config(self.config_path)
        import_run(config, self.run_dir)

        conn = connect(config.database.path)
        try:
            set_sample_field_overrides(conn, "SAMPLE001_fixture_run", {"lid": "LIDOVERRIDE"})
            changes = set_sample_field_overrides(conn, "SAMPLE001_fixture_run", {"lid": "LID001"})
            sample = get_sample(conn, "SAMPLE001_fixture_run")
        finally:
            conn.close()

        self.assertEqual(changes[0]["field_name"], "lid")
        self.assertTrue(changes[0]["cleared"])
        self.assertEqual(sample["lid"], "LID001")
        self.assertFalse(sample["lid_overridden"])

    def test_backfill_variant_af_counts_updates_existing_rows_from_raw_json(self) -> None:
        config = load_config(self.config_path)
        import_run(config, self.run_dir)

        conn = connect(config.database.path)
        try:
            conn.execute(
                """
                UPDATE samples
                SET
                    variant_af_count_005 = NULL,
                    variant_af_count_01 = NULL,
                    variant_af_count_015 = NULL,
                    variant_af_count_02 = NULL,
                    variant_af_count_03 = NULL,
                    variant_af_count_04 = NULL
                WHERE sample_run_id = ?
                """,
                ("SAMPLE001_fixture_run",),
            )
            conn.commit()
            updated = backfill_variant_af_counts(conn)
            sample = conn.execute(
                """
                SELECT
                    variant_af_count_005,
                    variant_af_count_01,
                    variant_af_count_015,
                    variant_af_count_02,
                    variant_af_count_03,
                    variant_af_count_04
                FROM samples
                WHERE sample_run_id = ?
                """,
                ("SAMPLE001_fixture_run",),
            ).fetchone()
        finally:
            conn.close()

        self.assertEqual(updated, 1)
        self.assertEqual(tuple(sample), (236, 210, 179, 151, 106, 92))

    def test_cli_parser_accepts_clarity_sample_info_flag(self) -> None:
        args = build_parser().parse_args(
            [
                "import-run",
                "--config",
                "virtitta.toml",
                "--run-dir",
                "/tmp/run",
                "--clarity-sample-info",
                "/tmp/clarity_sample_info.json",
            ]
        )

        self.assertEqual(args.command, "import-run")
        self.assertEqual(args.run_dir, "/tmp/run")
        self.assertEqual(args.clarity_sample_info, "/tmp/clarity_sample_info.json")

    def test_cli_parser_accepts_import_sample(self) -> None:
        args = build_parser().parse_args(
            [
                "import-sample",
                "--config",
                "virtitta.toml",
                "--sample-id",
                "FAILED001",
                "--lid",
                "LIDFAIL",
                "--ct",
                "29.4",
                "--library-concentration",
                "2.3",
                "--run-dir",
                "/tmp/run",
                "--clarity-sample-info",
                "/tmp/clarity_sample_info.json",
            ]
        )

        self.assertEqual(args.command, "import-sample")
        self.assertEqual(args.run_dir, "/tmp/run")
        self.assertEqual(args.sample_id, "FAILED001")
        self.assertEqual(args.lid, "LIDFAIL")
        self.assertEqual(args.ct, 29.4)
        self.assertEqual(args.library_concentration, 2.3)
        self.assertEqual(args.clarity_sample_info, "/tmp/clarity_sample_info.json")

    def test_cli_parser_accepts_minimal_import_sample(self) -> None:
        args = build_parser().parse_args(
            [
                "import-sample",
                "--config",
                "virtitta.toml",
                "--sample-id",
                "FAILED001",
                "--lid",
                "LIDFAIL",
            ]
        )

        self.assertEqual(args.command, "import-sample")
        self.assertEqual(args.config, "virtitta.toml")
        self.assertEqual(args.run_dir, "")
        self.assertEqual(args.sample_id, "FAILED001")
        self.assertEqual(args.lid, "LIDFAIL")
        self.assertIsNone(args.ct)
        self.assertIsNone(args.library_concentration)

    def test_cli_parser_accepts_backfill_af_counts(self) -> None:
        args = build_parser().parse_args(
            [
                "backfill-af-counts",
                "--config",
                "virtitta.toml",
            ]
        )

        self.assertEqual(args.command, "backfill-af-counts")
        self.assertEqual(args.config, "virtitta.toml")

    def test_load_config_reads_annotation_categories_and_default_category_column(self) -> None:
        config = load_config(self.config_path)
        self.assertEqual(config.annotations.sample_categories, ["production", "validation", "EQA"])
        self.assertEqual(config.ui.column_labels["sequencing_date"], "Date")
        self.assertEqual(config.ui.column_labels["generated_date"], "Import Date")
        self.assertEqual(config.ui.column_labels["variant_af_count_005"], "af 0.05")
        self.assertEqual(config.ui.column_labels["variant_af_count_01"], "af 0.1")
        self.assertEqual(config.ui.column_labels["variant_af_count_015"], "af 0.15")
        self.assertEqual(config.ui.column_labels["variant_af_count_02"], "af 0.2")
        self.assertEqual(config.ui.column_labels["variant_af_count_03"], "af 0.3")
        self.assertEqual(config.ui.column_labels["variant_af_count_04"], "af 0.4")
        self.assertIn("sequencing_date", config.ui.visible_columns)
        self.assertIn("sample_category", config.ui.visible_columns)
        self.assertIn("manual_groups", config.ui.visible_columns)
        self.assertNotIn("qc_coverage_1000x_pct", config.ui.visible_columns)
        self.assertNotIn("variant_af_count_005", config.ui.visible_columns)
        self.assertNotIn("variant_af_count_01", config.ui.visible_columns)
        self.assertNotIn("variant_af_count_015", config.ui.visible_columns)
        self.assertNotIn("variant_af_count_02", config.ui.visible_columns)
        self.assertNotIn("variant_af_count_03", config.ui.visible_columns)
        self.assertNotIn("variant_af_count_04", config.ui.visible_columns)

    def test_index_route_returns_template_response(self) -> None:
        config = load_config(self.config_path)
        import_run(config, self.run_dir)
        app = create_app(self.config_path)
        route = next(route for route in app.router.routes if getattr(route, "path", None) == "/")

        request = Request(
            {
                "type": "http",
                "http_version": "1.1",
                "method": "GET",
                "scheme": "http",
                "path": "/",
                "raw_path": b"/",
                "query_string": b"",
                "headers": [],
                "client": ("127.0.0.1", 12345),
                "server": ("testserver", 80),
                "app": app,
                "router": app.router,
            }
        )

        response = route.endpoint(
            request,
            search="",
            run_name="",
            subtype="",
            qc_status="",
            min_coverage_pct="",
            min_mean_depth="",
            min_blast_identity="",
            max_ct="",
            sort="run_name",
            desc=True,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.template.name, "index.html")
        self.assertEqual(len(response.context["rows"]), 1)

    def test_index_route_renders_visible_selection_checkbox(self) -> None:
        config = load_config(self.config_path)
        import_run(config, self.run_dir)
        app = create_app(self.config_path)
        route = next(route for route in app.router.routes if getattr(route, "path", None) == "/")

        request = Request(
            {
                "type": "http",
                "http_version": "1.1",
                "method": "GET",
                "scheme": "http",
                "path": "/",
                "raw_path": b"/",
                "query_string": b"",
                "headers": [],
                "client": ("127.0.0.1", 12345),
                "server": ("testserver", 80),
                "app": app,
                "router": app.router,
            }
        )

        response = route.endpoint(
            request,
            search="",
            run_name="",
            subtype="",
            qc_status="",
            min_coverage_pct="",
            min_mean_depth="",
            min_blast_identity="",
            max_ct="",
            sort="run_name",
            desc=True,
        )

        rendered = response.body.decode("utf-8")
        self.assertIn('aria-label="Go to sample table"', rendered)
        self.assertIn('id="table-search-input"', rendered)
        self.assertIn('id="select-visible-samples"', rendered)
        self.assertIn('aria-label="Select visible samples"', rendered)

    def test_index_route_renders_annotation_filters_and_optional_groups_column_toggle(self) -> None:
        config = load_config(self.config_path)
        import_run(config, self.run_dir)
        conn = connect(config.database.path)
        try:
            set_sample_category(conn, ["SAMPLE001_fixture_run"], "production")
            add_samples_to_group(conn, ["SAMPLE001_fixture_run"], "cluster-A")
            set_sample_field_overrides(conn, "SAMPLE001_fixture_run", {"lid": "LIDOVERRIDE"})
        finally:
            conn.close()

        app = create_app(self.config_path)
        route = next(route for route in app.router.routes if getattr(route, "path", None) == "/")
        request = Request(
            {
                "type": "http",
                "http_version": "1.1",
                "method": "GET",
                "scheme": "http",
                "path": "/",
                "raw_path": b"/",
                "query_string": b"",
                "headers": [],
                "client": ("127.0.0.1", 12345),
                "server": ("testserver", 80),
                "app": app,
                "router": app.router,
            }
        )

        response = route.endpoint(
            request,
            search="",
            run_name="",
            subtype="",
            qc_status="",
            min_coverage_pct="",
            min_mean_depth="",
            min_blast_identity="",
            max_ct="",
            sort="run_name",
            desc=True,
        )

        rendered = response.body.decode("utf-8")
        self.assertIn("Categories", rendered)
        self.assertIn("Groups", rendered)
        self.assertIn('data-col="manual_groups"', rendered)
        self.assertIn('data-col="manual_groups" checked', rendered)
        self.assertIn('data-col="qc_coverage_1000x_pct"', rendered)
        self.assertNotIn('data-col="qc_coverage_1000x_pct" checked', rendered)
        self.assertIn('data-col="variant_af_count_005"', rendered)
        self.assertIn('data-col="variant_af_count_01"', rendered)
        self.assertIn('data-col="variant_af_count_015"', rendered)
        self.assertIn('data-col="variant_af_count_02"', rendered)
        self.assertIn('data-col="variant_af_count_03"', rendered)
        self.assertIn('data-col="variant_af_count_04"', rendered)
        self.assertNotIn('data-col="variant_af_count_005" checked', rendered)
        self.assertNotIn('data-col="variant_af_count_01" checked', rendered)
        self.assertNotIn('data-col="variant_af_count_015" checked', rendered)
        self.assertNotIn('data-col="variant_af_count_02" checked', rendered)
        self.assertNotIn('data-col="variant_af_count_03" checked', rendered)
        self.assertNotIn('data-col="variant_af_count_04" checked', rendered)
        self.assertIn(">Cat<", rendered)
        self.assertIn(">QC<", rendered)
        self.assertIn(">Cov %<", rendered)
        self.assertIn(">Depth<", rendered)
        self.assertIn(">Frag bp<", rendered)
        self.assertIn(">af 0.05<", rendered)
        self.assertIn(">af 0.1<", rendered)
        self.assertIn('id="client-empty-state" hidden', rendered)
        self.assertIn('data-total-count="1"', rendered)
        self.assertIn("window.history.replaceState", rendered)
        self.assertIn("virtitta.columnVisibility.v1", rendered)
        self.assertIn("applySavedColumnVisibility();", rendered)
        self.assertIn("Apply category", rendered)
        self.assertIn("Add group", rendered)

    def test_igv_url_contains_expected_files(self) -> None:
        config = load_config(self.config_path)
        import_run(config, self.run_dir)
        conn = connect(config.database.path)
        try:
            rows = list_samples(conn)
            sample = get_sample(conn, rows[0]["sample_run_id"])
        finally:
            conn.close()

        self.assertIsNotNone(sample)
        igv_url = build_igv_url(config, sample)
        self.assertIn("genome=%2FQ%3Avirtitta-test%2Ffixture_run%2FSAMPLE001%2Fresults%2FSAMPLE001.fasta", igv_url)
        self.assertIn("file=%2FQ%3Avirtitta-test%2Ffixture_run%2FSAMPLE001%2Fresults%2FSAMPLE001.cram%2C", igv_url)
        self.assertIn("SAMPLE001_resistance.gff", igv_url)
        self.assertIn("merge=false", igv_url)

    def test_igv_url_updates_after_run_reimport(self) -> None:
        fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        fixture[0]["outputs"]["selected_vadr_gff"] = "SAMPLE001.vadr.fail_mod.gff"
        fixture[0]["outputs"]["vadr_fail_gff"] = "SAMPLE001.vadr.fail_mod.gff"
        fixture[0]["outputs"]["vadr_gff"] = "SAMPLE001.vadr.fail_mod.gff"
        self.write_run_summaries([fixture[0]])

        config = load_config(self.config_path)
        import_run(config, self.run_dir)
        conn = connect(config.database.path)
        try:
            rows = list_samples(conn)
            sample = get_sample(conn, rows[0]["sample_run_id"])
        finally:
            conn.close()

        self.assertIsNotNone(sample)
        stale_igv_url = build_igv_url(config, sample)
        self.assertIn("SAMPLE001.vadr.fail_mod.gff", stale_igv_url)

        refreshed_fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        self.write_run_summaries([refreshed_fixture[0]])
        import_run(config, self.run_dir)

        conn = connect(config.database.path)
        try:
            rows = list_samples(conn)
            sample = get_sample(conn, rows[0]["sample_run_id"])
        finally:
            conn.close()

        refreshed_igv_url = build_igv_url(config, sample)
        self.assertIn("SAMPLE001.vadr.pass_mod.gff", refreshed_igv_url)
        self.assertNotIn("SAMPLE001.vadr.fail_mod.gff", refreshed_igv_url)

    def test_igv_goto_url_contains_mutation_locus(self) -> None:
        config = load_config(self.config_path)
        url = build_igv_goto_url(config, "SAMPLE001:6550-6552")
        self.assertIn("/goto?", url)
        self.assertIn("locus=SAMPLE001%3A6550-6552", url)

    def test_list_samples_supports_subtype_and_numeric_filters(self) -> None:
        config = load_config(self.config_path)
        import_run(config, self.run_dir)
        conn = connect(config.database.path)
        try:
            rows = list_samples(
                conn,
                subtype="3a",
                min_coverage_pct=90.0,
                min_mean_depth=4.0,
                min_blast_identity=91.0,
                max_ct=25.0,
            )
        finally:
            conn.close()

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["sample_run_id"], "SAMPLE001_fixture_run")

    def test_list_samples_supports_category_and_group_filters(self) -> None:
        fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        extra = json.loads(json.dumps(fixture[0]))
        extra["sample_id"] = "SAMPLE002"
        extra["sample_run_id"] = "SAMPLE002_fixture_run"
        extra["lid"] = "LID002"
        extra["outputs"] = dict(extra["outputs"])
        for key, value in list(extra["outputs"].items()):
            if isinstance(value, str):
                extra["outputs"][key] = value.replace("SAMPLE001", "SAMPLE002").replace("LID001", "LID002")
        self.write_run_summaries([fixture[0], extra])
        sample2_dir = self.run_dir / "SAMPLE002" / "results"
        sample2_dir.mkdir(parents=True, exist_ok=True)
        for filename in [
            "SAMPLE002_rug_kde_plot.png",
            "SAMPLE002.fasta",
            "SAMPLE002.cram",
            "SAMPLE002-pilon-m0.05.vcf.gz",
            "SAMPLE002-pilon-m0.1.vcf.gz",
            "SAMPLE002-pilon-m0.15.vcf.gz",
            "SAMPLE002-pilon-m0.2.vcf.gz",
            "SAMPLE002-pilon-m0.3.vcf.gz",
            "SAMPLE002-pilon-m0.4.vcf.gz",
            "SAMPLE002.vadr.bed",
            "SAMPLE002_resistance.gff",
            "SAMPLE002.vadr.pass_mod.gff",
        ]:
            (sample2_dir / filename).write_text("placeholder", encoding="utf-8")
        (sample2_dir / "lid").mkdir(parents=True)
        (sample2_dir / "lid" / "LID002-2limsrs.txt").write_text(
            "sample_id\tparameter_name\tparameter_value\tcomment\n"
            "LID002\thcvtyp\tHCV genotyp 3a\t\n",
            encoding="utf-8",
        )
        (sample2_dir / "lid" / "LID002.fasta").write_text(">LID002\nACGT\n", encoding="utf-8")
        (sample2_dir / "lid" / "LID002-0.15-iupac.fasta").write_text(">LID002-0.15-iupac\nARYT\n", encoding="utf-8")

        config = load_config(self.config_path)
        import_run(config, self.run_dir)
        conn = connect(config.database.path)
        try:
            set_sample_category(conn, ["SAMPLE001_fixture_run"], "production")
            set_sample_category(conn, ["SAMPLE002_fixture_run"], "validation")
            add_samples_to_group(conn, ["SAMPLE001_fixture_run"], "outbreak-17")
            add_samples_to_group(conn, ["SAMPLE001_fixture_run"], "cluster-A")
            add_samples_to_group(conn, ["SAMPLE002_fixture_run"], "cluster-B")

            production_rows = list_samples(conn, sample_categories=["production"])
            unassigned_rows = list_samples(conn, sample_categories=["__unassigned__"])
            grouped_rows = list_samples(conn, manual_groups=["cluster-A", "cluster-B"])
            sample1 = get_sample(conn, "SAMPLE001_fixture_run")
        finally:
            conn.close()

        self.assertEqual([row["sample_run_id"] for row in production_rows], ["SAMPLE001_fixture_run"])
        self.assertEqual(unassigned_rows, [])
        self.assertEqual(
            {row["sample_run_id"] for row in grouped_rows},
            {"SAMPLE001_fixture_run", "SAMPLE002_fixture_run"},
        )
        self.assertEqual(sample1["sample_category"], "production")
        self.assertEqual(sample1["manual_groups"], "cluster-A, outbreak-17")

    def test_annotations_survive_reimport(self) -> None:
        config = load_config(self.config_path)
        import_run(config, self.run_dir)
        conn = connect(config.database.path)
        try:
            set_sample_category(conn, ["SAMPLE001_fixture_run"], "EQA")
            add_samples_to_group(conn, ["SAMPLE001_fixture_run"], "outbreak-22")
        finally:
            conn.close()

        import_run(config, self.run_dir)

        conn = connect(config.database.path)
        try:
            sample = get_sample(conn, "SAMPLE001_fixture_run")
        finally:
            conn.close()

        self.assertEqual(sample["sample_category"], "EQA")
        self.assertEqual(sample["manual_groups"], "outbreak-22")

    def test_distinct_category_and_group_lists_include_manual_annotations(self) -> None:
        config = load_config(self.config_path)
        import_run(config, self.run_dir)
        conn = connect(config.database.path)
        try:
            set_sample_category(conn, ["SAMPLE001_fixture_run"], "production")
            add_samples_to_group(conn, ["SAMPLE001_fixture_run"], "cluster-A")
            add_samples_to_group(conn, ["SAMPLE001_fixture_run"], "cluster-A")
            stored_categories = list_stored_sample_categories(conn)
            stored_groups = list_manual_groups(conn)
        finally:
            conn.close()

        self.assertEqual(stored_categories, ["production"])
        self.assertEqual(stored_groups, ["cluster-A"])

    def test_list_samples_includes_comment_preview(self) -> None:
        config = load_config(self.config_path)
        import_run(config, self.run_dir)
        conn = connect(config.database.path)
        try:
            add_comment(conn, "SAMPLE001_fixture_run", "First comment body", "alice")
            add_comment(conn, "SAMPLE001_fixture_run", "Second comment body", "bob")
            rows = list_samples(conn)
        finally:
            conn.close()

        self.assertEqual(rows[0]["comment_count"], 2)
        self.assertIn("bob: Second comment body", rows[0]["comment_preview"])

    def test_comment_link_label_uses_latest_comment_snippet(self) -> None:
        self.assertEqual(comment_link_label({"comment_count": 0, "comment_preview": ""}), "None")
        self.assertEqual(
            comment_link_label({"comment_count": 2, "comment_preview": "bob: failing sample badly\n---\nalice: older note"}),
            "2 - failing sam..."
        )

    def test_format_value_applies_column_specific_rounding(self) -> None:
        self.assertEqual(format_value(123456, "host_filter_reads_in"), "123 456")
        self.assertEqual(format_value(0.0123, "host_filter_reads_removed_proportion"), "1.2%")
        self.assertEqual(format_value(91.514, "typing_main_blast_identity"), "91.5")
        self.assertEqual(format_value(92.4598, "qc_coverage_pct"), "92.46")
        self.assertEqual(format_value(4.42121, "qc_mean_depth"), "4")

    def test_human_column_style_uses_data_bar_width(self) -> None:
        self.assertEqual(cell_style("host_filter_reads_removed_proportion", 0.0123), "--data-bar-width:1.230%;")
        self.assertEqual(cell_style("qc_mean_depth", 4.0), "")

    def test_resistance_cells_render_detected_and_clear_states(self) -> None:
        fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))[0]
        cells = build_resistance_cells(fixture)
        self.assertEqual(len(cells), 16)
        by_short = {cell["short"]: cell for cell in cells}
        self.assertEqual(by_short["DCV"]["status"], "resistant")
        self.assertIn("NS5A:Y93H", by_short["DCV"]["mutations"])
        self.assertEqual(by_short["ASV"]["status"], "clear")
        self.assertEqual(by_short["SOF"]["status"], "clear")

    def test_resistance_tooltip_only_lists_positive_calls(self) -> None:
        fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))[0]
        tooltip = resistance_tooltip_text(fixture)
        self.assertIn("DCV: NS5A:Y93H", tooltip)
        self.assertIn("EBR: NS5A:Y93H", tooltip)
        self.assertNotIn("SOF", tooltip)
        self.assertNotIn("No resistance detected", tooltip)

    def test_resistance_mutations_include_igv_locus(self) -> None:
        fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))[0]
        mutations = build_resistance_mutations(fixture, fixture["sample_id"])
        self.assertEqual(mutations[0]["mutation_label"], "NS5A:Y93H")
        self.assertEqual(mutations[0]["locus"], "SAMPLE001:6550-6552")

    def test_search_matches_resistance_content_and_sort_prioritizes_positive_profiles(self) -> None:
        fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        resistant = fixture[0]
        clear = json.loads(json.dumps(resistant))
        clear["sample_id"] = "SAMPLE002"
        clear["sample_run_id"] = "SAMPLE002_fixture_run"
        clear["lid"] = "LID002"
        clear["resistance"]["has_resistance"] = False
        clear["resistance"]["mutation_count"] = 0
        clear["resistance"]["by_drug"] = []
        clear["resistance"]["mutations"] = []
        self.write_run_summaries([resistant, clear])

        config = load_config(self.config_path)
        import_run(config, self.run_dir)
        conn = connect(config.database.path)
        try:
            rows = list_samples(conn, search="Y93H")
        finally:
            conn.close()

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["sample_id"], "SAMPLE001")

        app = create_app(self.config_path)
        route = next(route for route in app.router.routes if getattr(route, "path", None) == "/")
        request = Request(
            {
                "type": "http",
                "http_version": "1.1",
                "method": "GET",
                "scheme": "http",
                "path": "/",
                "raw_path": b"/",
                "query_string": b"",
                "headers": [],
                "client": ("127.0.0.1", 12345),
                "server": ("testserver", 80),
                "app": app,
                "router": app.router,
            }
        )
        response = route.endpoint(
            request,
            search="",
            run_name="",
            subtype="",
            qc_status="",
            min_coverage_pct="",
            min_mean_depth="",
            min_blast_identity="",
            max_ct="",
            sort="resistance_summary",
            desc=True,
        )
        rows = response.context["rows"]
        self.assertEqual(rows[0]["sample_id"], "SAMPLE001")
        self.assertEqual(rows[1]["sample_id"], "SAMPLE002")

    def test_lims_export_reuses_existing_rows_and_appends_qc(self) -> None:
        config = load_config(self.config_path)
        import_run(config, self.run_dir)
        conn = connect(config.database.path)
        try:
            update_qc_status(conn, ["SAMPLE001_fixture_run"], "pass")
            sample = get_sample(conn, "SAMPLE001_fixture_run")
        finally:
            conn.close()

        self.assertIsNotNone(sample)
        export_text = build_lims_export_content(config, [sample])
        self.assertEqual(
            export_text,
            (
                "sample_id\tparameter_name\tparameter_value\tcomment\n"
                "LID001\thcvtyp\tHCV genotyp 3a\t\n"
                "LID001\thcvqc\tPassed\t\n"
            ),
        )

    def test_fasta_clipboard_content_uses_selected_output(self) -> None:
        config = load_config(self.config_path)
        import_run(config, self.run_dir)
        conn = connect(config.database.path)
        try:
            sample = get_sample(conn, "SAMPLE001_fixture_run")
        finally:
            conn.close()

        self.assertIsNotNone(sample)
        assert sample is not None
        self.assertEqual(
            build_fasta_clipboard_content(config, [sample], "export_fasta"),
            ">LID001\nACGT\n",
        )
        self.assertEqual(
            build_fasta_clipboard_content(config, [sample], "export_iupac_fasta"),
            ">LID001-0.15-iupac\nARYT\n",
        )

    def test_lims_export_is_written_to_server_export_root(self) -> None:
        config = load_config(self.config_path)
        import_run(config, self.run_dir)
        conn = connect(config.database.path)
        try:
            update_qc_status(conn, ["SAMPLE001_fixture_run"], "pass")
            sample = get_sample(conn, "SAMPLE001_fixture_run")
        finally:
            conn.close()

        self.assertIsNotNone(sample)
        export_text = build_lims_export_content(config, [sample])

        from virtitta.app import write_server_lims_export

        export_path = write_server_lims_export(config, [sample], export_text)
        self.assertIsNotNone(export_path)
        assert export_path is not None
        self.assertTrue(export_path.exists())
        self.assertEqual(export_path.parent.name, __import__("datetime").datetime.now().date().isoformat())
        self.assertEqual(export_path.read_text(encoding="utf-8"), export_text)

    def test_single_sample_lims_export_blocks_unreviewed(self) -> None:
        config = load_config(self.config_path)
        import_run(config, self.run_dir)
        app = create_app(self.config_path)
        route = next(
            route for route in app.router.routes if getattr(route, "path", None) == "/samples/{sample_run_id}/lims-export"
        )

        response = route.endpoint("SAMPLE001_fixture_run")

        self.assertEqual(response.status_code, 303)
        self.assertIn("/samples/SAMPLE001_fixture_run", response.headers["location"])
        self.assertIn("warning=", response.headers["location"])

    def test_single_sample_lims_export_writes_server_file_and_redirects_with_notice(self) -> None:
        config = load_config(self.config_path)
        import_run(config, self.run_dir)
        conn = connect(config.database.path)
        try:
            update_qc_status(conn, ["SAMPLE001_fixture_run"], "pass")
        finally:
            conn.close()

        app = create_app(self.config_path)
        route = next(
            route for route in app.router.routes if getattr(route, "path", None) == "/samples/{sample_run_id}/lims-export"
        )
        response = route.endpoint("SAMPLE001_fixture_run")

        self.assertEqual(response.status_code, 303)
        self.assertIn("notice=", response.headers["location"])

        export_root = self.root / "lims_exports"
        exported = list(export_root.glob("*/*.txt"))
        self.assertEqual(len(exported), 1)
        self.assertIn("LID001\thcvqc\tPassed\t", exported[0].read_text(encoding="utf-8"))

    def test_single_sample_lims_export_download_returns_attachment(self) -> None:
        config = load_config(self.config_path)
        import_run(config, self.run_dir)
        conn = connect(config.database.path)
        try:
            update_qc_status(conn, ["SAMPLE001_fixture_run"], "pass")
        finally:
            conn.close()

        app = create_app(self.config_path)
        route = next(
            route for route in app.router.routes if getattr(route, "path", None) == "/samples/{sample_run_id}/lims-export/download"
        )
        response = route.endpoint("SAMPLE001_fixture_run")

        self.assertEqual(response.status_code, 200)
        self.assertIn('attachment; filename="LID001-2limsrs.txt"', response.headers["content-disposition"])

    def test_bulk_lims_export_blocks_unreviewed(self) -> None:
        config = load_config(self.config_path)
        import_run(config, self.run_dir)
        app = create_app(self.config_path)
        route = next(route for route in app.router.routes if getattr(route, "path", None) == "/samples/lims-export")

        response = asyncio.run(
            route.endpoint(
                sample_run_id=["SAMPLE001_fixture_run"],
                redirect_to="/?run_name=fixture_run",
            )
        )

        self.assertEqual(response.status_code, 303)
        self.assertIn("/?run_name=fixture_run", response.headers["location"])
        self.assertIn("warning=", response.headers["location"])

    def test_bulk_lims_export_writes_server_file_and_redirects_with_notice(self) -> None:
        config = load_config(self.config_path)
        import_run(config, self.run_dir)
        conn = connect(config.database.path)
        try:
            update_qc_status(conn, ["SAMPLE001_fixture_run"], "pass")
        finally:
            conn.close()

        app = create_app(self.config_path)
        route = next(route for route in app.router.routes if getattr(route, "path", None) == "/samples/lims-export")
        response = asyncio.run(
            route.endpoint(
                sample_run_id=["SAMPLE001_fixture_run"],
                redirect_to="/?run_name=fixture_run",
            )
        )

        self.assertEqual(response.status_code, 303)
        self.assertIn("notice=", response.headers["location"])

    def test_bulk_lims_export_download_returns_attachment(self) -> None:
        config = load_config(self.config_path)
        import_run(config, self.run_dir)
        conn = connect(config.database.path)
        try:
            update_qc_status(conn, ["SAMPLE001_fixture_run"], "pass")
        finally:
            conn.close()

        app = create_app(self.config_path)
        route = next(route for route in app.router.routes if getattr(route, "path", None) == "/samples/lims-export/download")
        response = asyncio.run(
            route.endpoint(
                sample_run_id=["SAMPLE001_fixture_run"],
                redirect_to="/?run_name=fixture_run",
            )
        )

        self.assertEqual(response.status_code, 200)
        self.assertIn('attachment; filename="LID001-2limsrs.txt"', response.headers["content-disposition"])

    def test_bulk_fasta_clipboard_export_returns_text(self) -> None:
        config = load_config(self.config_path)
        import_run(config, self.run_dir)
        app = create_app(self.config_path)
        route = next(route for route in app.router.routes if getattr(route, "path", None) == "/samples/clipboard/fasta")
        response = asyncio.run(route.endpoint(sample_run_id=["SAMPLE001_fixture_run"]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.body.decode("utf-8"), ">LID001\nACGT\n")

    def test_bulk_iupac_fasta_clipboard_export_returns_text(self) -> None:
        config = load_config(self.config_path)
        import_run(config, self.run_dir)
        app = create_app(self.config_path)
        route = next(route for route in app.router.routes if getattr(route, "path", None) == "/samples/clipboard/iupac-fasta")
        response = asyncio.run(route.endpoint(sample_run_id=["SAMPLE001_fixture_run"]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.body.decode("utf-8"), ">LID001-0.15-iupac\nARYT\n")

    def test_fail_qc_requires_comment(self) -> None:
        config = load_config(self.config_path)
        import_run(config, self.run_dir)
        app = create_app(self.config_path)
        route = next(route for route in app.router.routes if getattr(route, "path", None) == "/samples/qc")

        response = asyncio.run(
            route.endpoint(
                sample_run_id=["SAMPLE001_fixture_run"],
                qc_status="fail",
                comment_body="",
                comment_author="",
                redirect_to="/samples/SAMPLE001_fixture_run",
            )
        )

        self.assertEqual(response.status_code, 303)
        self.assertIn("warning=", response.headers["location"])

    def test_fail_qc_with_comment_adds_comment(self) -> None:
        config = load_config(self.config_path)
        import_run(config, self.run_dir)
        app = create_app(self.config_path)
        route = next(route for route in app.router.routes if getattr(route, "path", None) == "/samples/qc")

        response = asyncio.run(
            route.endpoint(
                sample_run_id=["SAMPLE001_fixture_run"],
                qc_status="fail",
                comment_body="Coverage too low",
                comment_author="tester",
                redirect_to="/samples/SAMPLE001_fixture_run",
            )
        )

        self.assertEqual(response.status_code, 303)
        conn = connect(config.database.path)
        try:
            sample = get_sample(conn, "SAMPLE001_fixture_run")
            comments = get_comments(conn, "SAMPLE001_fixture_run")
        finally:
            conn.close()

        self.assertEqual(sample["qc_status"], "fail")
        self.assertEqual(len(comments), 1)
        self.assertEqual(comments[0]["body"], "Coverage too low")
        self.assertEqual(comments[0]["author"], "tester")

    def test_bulk_category_route_assigns_and_clears_category(self) -> None:
        config = load_config(self.config_path)
        import_run(config, self.run_dir)
        app = create_app(self.config_path)
        route = next(route for route in app.router.routes if getattr(route, "path", None) == "/samples/category")

        response = asyncio.run(
            route.endpoint(
                sample_run_id=["SAMPLE001_fixture_run"],
                sample_category="validation",
                redirect_to="/?run_name=fixture_run",
            )
        )

        self.assertEqual(response.status_code, 303)
        conn = connect(config.database.path)
        try:
            sample = get_sample(conn, "SAMPLE001_fixture_run")
        finally:
            conn.close()
        self.assertEqual(sample["sample_category"], "validation")

        asyncio.run(
            route.endpoint(
                sample_run_id=["SAMPLE001_fixture_run"],
                sample_category="",
                redirect_to="/?run_name=fixture_run",
            )
        )
        conn = connect(config.database.path)
        try:
            sample = get_sample(conn, "SAMPLE001_fixture_run")
        finally:
            conn.close()
        self.assertIsNone(sample["sample_category"])

    def test_bulk_group_routes_add_and_remove_membership(self) -> None:
        config = load_config(self.config_path)
        import_run(config, self.run_dir)
        app = create_app(self.config_path)
        add_route = next(route for route in app.router.routes if getattr(route, "path", None) == "/samples/groups/add")
        remove_route = next(route for route in app.router.routes if getattr(route, "path", None) == "/samples/groups/remove")

        response = asyncio.run(
            add_route.endpoint(
                sample_run_id=["SAMPLE001_fixture_run"],
                group_name="outbreak-19",
                redirect_to="/?run_name=fixture_run",
            )
        )
        self.assertEqual(response.status_code, 303)

        conn = connect(config.database.path)
        try:
            sample = get_sample(conn, "SAMPLE001_fixture_run")
        finally:
            conn.close()
        self.assertEqual(sample["manual_groups"], "outbreak-19")

        response = asyncio.run(
            remove_route.endpoint(
                sample_run_id=["SAMPLE001_fixture_run"],
                group_name="outbreak-19",
                redirect_to="/?run_name=fixture_run",
            )
        )
        self.assertEqual(response.status_code, 303)

        conn = connect(config.database.path)
        try:
            sample = get_sample(conn, "SAMPLE001_fixture_run")
        finally:
            conn.close()
        self.assertIsNone(sample["manual_groups"])

    def test_sample_detail_renders_category_and_groups_read_only(self) -> None:
        config = load_config(self.config_path)
        import_run(config, self.run_dir)
        conn = connect(config.database.path)
        try:
            set_sample_category(conn, ["SAMPLE001_fixture_run"], "production")
            add_samples_to_group(conn, ["SAMPLE001_fixture_run"], "cluster-A")
            set_sample_field_overrides(conn, "SAMPLE001_fixture_run", {"lid": "LIDOVERRIDE"})
        finally:
            conn.close()

        app = create_app(self.config_path)
        route = next(route for route in app.router.routes if getattr(route, "path", None) == "/samples/{sample_run_id}")
        request = Request(
            {
                "type": "http",
                "http_version": "1.1",
                "method": "GET",
                "scheme": "http",
                "path": "/samples/SAMPLE001_fixture_run",
                "raw_path": b"/samples/SAMPLE001_fixture_run",
                "query_string": b"",
                "headers": [],
                "client": ("127.0.0.1", 12345),
                "server": ("testserver", 80),
                "app": app,
                "router": app.router,
            }
        )

        response = route.endpoint(request, "SAMPLE001_fixture_run")
        rendered = response.body.decode("utf-8")
        self.assertIn("Category:</strong> production", rendered)
        self.assertIn("Groups:</strong> cluster-A", rendered)
        self.assertIn("overridden-value", rendered)
        self.assertIn("Edit metadata", rendered)
        self.assertIn("LIDOVERRIDE", rendered)
        self.assertIn('name="sequencing_date" value="2026-04-08" placeholder="YYYY-MM-DD"', rendered)
        self.assertNotIn('type="date" name="sequencing_date"', rendered)

    def test_sample_override_route_updates_values_and_adds_comments(self) -> None:
        config = load_config(self.config_path)
        import_run(config, self.run_dir)
        app = create_app(self.config_path)
        route = next(
            route
            for route in app.router.routes
            if getattr(route, "path", None) == "/samples/{sample_run_id}/overrides"
        )

        response = asyncio.run(
            route.endpoint(
                "SAMPLE001_fixture_run",
                lid="LIDEDIT",
                sequencing_date="2026-02-03",
                sample_metadata_ct="28.7",
                sample_metadata_library_concentration_ng_ul="",
                typing_report_subtype="4a",
            )
        )
        self.assertEqual(response.status_code, 303)

        conn = connect(config.database.path)
        try:
            sample = get_sample(conn, "SAMPLE001_fixture_run")
            comments = get_comments(conn, "SAMPLE001_fixture_run")
        finally:
            conn.close()

        self.assertEqual(sample["lid"], "LIDEDIT")
        self.assertEqual(sample["sequencing_date"], "2026-02-03")
        self.assertEqual(sample["sample_metadata_ct"], 28.7)
        self.assertEqual(sample["typing_report_subtype"], "4a")
        comment_bodies = [comment["body"] for comment in comments]
        self.assertTrue(any("Manual override: LID changed from LID001 to LIDEDIT." in body for body in comment_bodies))
        self.assertTrue(any("Manual override: Date changed from 2026-04-08 to 2026-02-03." in body for body in comment_bodies))
        self.assertTrue(all(comment["author"] == "Virtitta" for comment in comments))

    def test_override_comment_text_for_cleared_override(self) -> None:
        text = override_comment_text(
            {
                "field_name": "sample_metadata_ct",
                "old_value": 28.7,
                "new_value": 31.2,
                "cleared": True,
            }
        )
        self.assertEqual(text, "Manual override cleared: CT now uses imported value 31.2.")

    def test_delete_comment_route_removes_comment(self) -> None:
        config = load_config(self.config_path)
        import_run(config, self.run_dir)
        conn = connect(config.database.path)
        try:
            add_comment(conn, "SAMPLE001_fixture_run", "Delete me", "tester")
            comment_id = get_comments(conn, "SAMPLE001_fixture_run")[0]["id"]
        finally:
            conn.close()

        app = create_app(self.config_path)
        route = next(
            route
            for route in app.router.routes
            if getattr(route, "path", None) == "/samples/{sample_run_id}/comments/{comment_id}/delete"
        )

        response = asyncio.run(route.endpoint("SAMPLE001_fixture_run", comment_id))
        self.assertEqual(response.status_code, 303)

        conn = connect(config.database.path)
        try:
            comments = get_comments(conn, "SAMPLE001_fixture_run")
        finally:
            conn.close()

        self.assertEqual(comments, [])

    def test_delete_single_sample_route_removes_sample_and_run(self) -> None:
        config = load_config(self.config_path)
        import_run(config, self.run_dir)
        app = create_app(self.config_path)
        route = next(
            route
            for route in app.router.routes
            if getattr(route, "path", None) == "/samples/{sample_run_id}/delete"
        )

        response = asyncio.run(route.endpoint("SAMPLE001_fixture_run"))
        self.assertEqual(response.status_code, 303)

        conn = connect(config.database.path)
        try:
            sample = get_sample(conn, "SAMPLE001_fixture_run")
            runs = list_runs(conn)
        finally:
            conn.close()

        self.assertIsNone(sample)
        self.assertEqual(runs, [])


if __name__ == "__main__":
    unittest.main()
