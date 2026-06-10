from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi import HTTPException
from starlette.requests import Request

from virtitta.app import (
    build_grapetree_url,
    build_fasta_clipboard_content,
    build_igv_goto_url,
    build_igv_url,
    build_webigv_browser_config,
    build_lims_export_content,
    build_resistance_cells,
    build_resistance_mutations,
    column_visibility_storage_key,
    column_style,
    resistance_tooltip_text,
    cell_style,
    comment_link_label,
    create_app,
    format_value,
    is_public_request_path,
    override_comment_text,
    table_columns,
)
from virtitta.artifact_cache import CACHE_OK, CACHE_STALE, verify_sample_cache
from virtitta.auth import create_login_session, hash_password
from virtitta.cli import build_parser
from virtitta.cluster import ClusterError, cluster_artifacts, prepare_cluster_files, run_cluster_job
from virtitta.config import load_config
from virtitta.importer import MANUAL_FAILED_RUN_NAME, import_run, import_run_with_report, import_sample
from virtitta.repository import (
    add_comment,
    add_samples_to_group,
    backfill_variant_af_counts,
    connect,
    create_auth_user,
    create_cluster_job,
    get_cluster_job,
    get_cluster_job_samples,
    get_comments,
    get_output_cache_entry,
    get_sample,
    init_db,
    list_manual_groups,
    list_runs,
    list_samples_for_cache_verification,
    list_samples,
    list_stored_sample_categories,
    remove_samples_from_group,
    set_sample_category,
    set_sample_field_overrides,
    update_qc_status,
    utc_now,
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
                'sample_categories = ["production", "validation", "EQA", "test"]',
                'restricted_sample_categories = ["test"]',
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
    def make_request(self, app, *, path: str = "/", method: str = "GET", query_string: bytes = b"") -> Request:
        return Request(
            {
                "type": "http",
                "http_version": "1.1",
                "method": method,
                "scheme": "http",
                "path": path,
                "raw_path": path.encode("utf-8"),
                "query_string": query_string,
                "headers": [],
                "client": ("127.0.0.1", 12345),
                "server": ("testserver", 80),
                "app": app,
                "router": app.router,
            }
        )

    def make_user_request(self, app, user, *, path: str = "/", method: str = "GET") -> Request:
        request = self.make_request(app, path=path, method=method)
        request.state.current_user = user
        return request

    def asgi_request(self, app, *, path: str = "/", method: str = "GET") -> list[dict]:
        messages = []
        request_sent = False

        async def receive():
            nonlocal request_sent
            if request_sent:
                return {"type": "http.disconnect"}
            request_sent = True
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message):
            messages.append(message)

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": path,
            "raw_path": path.encode("utf-8"),
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 12345),
            "server": ("testserver", 80),
        }
        asyncio.run(app(scope, receive, send))
        return messages

    def enable_auth(self) -> None:
        with self.config_path.open("a", encoding="utf-8") as handle:
            handle.write(
                "\n"
                "[auth]\n"
                "enabled = true\n"
                'provider = "local"\n'
                "session_days = 7\n"
                'cookie_name = "virtitta_session"\n'
                "cookie_secure = false\n"
                "pbkdf2_iterations = 600000\n"
            )

    def enable_webigv(self) -> None:
        with self.config_path.open("a", encoding="utf-8") as handle:
            handle.write(
                "\n"
                "[webigv]\n"
                "enabled = true\n"
                'igv_js_url = "/static/igv.min.js"\n'
            )

    def enable_cluster(
        self,
        *,
        mafft_command: str = "mafft",
        iqtree_command: str = "iqtree3",
        public_base_url: str = "",
        iqtree_threads: int = 4,
        five_prime_trim: int = 50,
    ) -> None:
        public_base_url_line = f'public_base_url = "{public_base_url}"\n' if public_base_url else ""
        with self.config_path.open("a", encoding="utf-8") as handle:
            handle.write(
                "\n"
                "[cluster]\n"
                "enabled = true\n"
                f'output_root = "{(self.tmp_path / "clusters").as_posix()}"\n'
                'grapetree_url = "https://mtlucmds1.lund.skane.se/grapetree/"\n'
                f"{public_base_url_line}"
                "max_concurrent_jobs = 1\n"
                "timeout_seconds = 60\n"
                'input_output_key = "iupac_fasta"\n'
                f"five_prime_trim = {five_prime_trim}\n"
                "poly_t = true\n"
                "poly_t_min_length = 10\n"
                "poly_t_seed_length = 12\n"
                "poly_t_seed_min_t = 10\n"
                "poly_t_max_trailing_bases = 100\n"
                f'mafft_command = "{mafft_command}"\n'
                f'iqtree_command = "{iqtree_command}"\n'
                'mafft_args = ["--auto"]\n'
                f"iqtree_threads = {iqtree_threads}\n"
                "iqtree_args = []\n"
            )

    def add_second_sample_summary(
        self,
        *,
        subtype: str = "1a",
        lid: str = "LID002",
        tree_id: str = "LID002-0.15-iupac",
        sequence: str = "ACGTAAAA",
    ) -> None:
        fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))[0]
        sample = json.loads(json.dumps(fixture))
        sample["sample_id"] = "SAMPLE002"
        sample["sample_run_id"] = "SAMPLE002_fixture_run"
        sample["lid"] = lid
        sample["typing"]["main_blast_genotype"] = subtype
        sample["typing"]["report_subtype"] = subtype
        sample["outputs"] = dict(sample["outputs"])
        for key, value in list(sample["outputs"].items()):
            if isinstance(value, str):
                sample["outputs"][key] = value.replace("SAMPLE001", "SAMPLE002").replace("LID001", lid)
        sample["outputs"]["export_iupac_fasta"] = f"lid/{lid}-0.15-iupac.fasta"
        self.write_sample_summary(sample)
        sample2_dir = self.run_dir / "SAMPLE002" / "results"
        for filename in [
            "SAMPLE002_rug_kde_plot.png",
            "SAMPLE002.fasta",
            "SAMPLE002.fasta.fai",
            "SAMPLE002.cram",
            "SAMPLE002.cram.crai",
            "SAMPLE002-pilon-m0.05.vcf.gz",
            "SAMPLE002-pilon-m0.05.vcf.gz.csi",
            "SAMPLE002-pilon-m0.1.vcf.gz",
            "SAMPLE002-pilon-m0.1.vcf.gz.csi",
            "SAMPLE002-pilon-m0.15.vcf.gz",
            "SAMPLE002-pilon-m0.15.vcf.gz.csi",
            "SAMPLE002-pilon-m0.2.vcf.gz",
            "SAMPLE002-pilon-m0.2.vcf.gz.csi",
            "SAMPLE002-pilon-m0.3.vcf.gz",
            "SAMPLE002-pilon-m0.3.vcf.gz.csi",
            "SAMPLE002-pilon-m0.4.vcf.gz",
            "SAMPLE002-pilon-m0.4.vcf.gz.csi",
            "SAMPLE002.vadr.bed",
            "SAMPLE002_resistance.gff",
            "SAMPLE002.vadr.pass_mod.gff",
            "SAMPLE002.fasta.blast",
        ]:
            (sample2_dir / filename).write_text("placeholder", encoding="utf-8")
        (sample2_dir / "SAMPLE002-0.15-iupac.fasta").write_text(
            f">{tree_id}\n{sequence}\n",
            encoding="utf-8",
        )
        sample2_lid_dir = self.run_dir / "SAMPLE002" / "results" / "lid"
        sample2_lid_dir.mkdir(parents=True, exist_ok=True)
        (sample2_lid_dir / f"{lid}-0.15-iupac.fasta").write_text(
            f">{tree_id}\n{sequence}\n",
            encoding="utf-8",
        )

    def write_fake_cluster_tools(self) -> tuple[Path, Path]:
        bin_dir = self.tmp_path / "bin"
        bin_dir.mkdir()
        mafft = bin_dir / "mafft"
        iqtree = bin_dir / "iqtree3"
        mafft.write_text(
            "#!/usr/bin/env python3\n"
            "import pathlib, sys\n"
            "print(pathlib.Path(sys.argv[-1]).read_text(), end='')\n",
            encoding="utf-8",
        )
        iqtree.write_text(
            "#!/usr/bin/env python3\n"
            "import pathlib, sys\n"
            "prefix = pathlib.Path(sys.argv[sys.argv.index('-pre') + 1])\n"
            "prefix.with_suffix('.treefile').write_text('(LID001:0.1,LID002:0.1);\\n')\n",
            encoding="utf-8",
        )
        for tool in (mafft, iqtree):
            tool.chmod(0o755)
        return mafft, iqtree

    def create_local_user(self, username: str, role: str, password: str = "secret") -> None:
        config = load_config(self.config_path)
        conn = connect(config.database.path)
        try:
            init_db(conn)
            create_auth_user(
                conn,
                username,
                hash_password(password, iterations=config.auth.pbkdf2_iterations),
                role,
                username,
            )
        finally:
            conn.close()

    def login_local_user(self, username: str):
        config = load_config(self.config_path)
        return create_login_session(config, username)

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

    def write_required_sidecar_outputs(self, sample_dir: Path, sample_id: str) -> None:
        for filename in [
            f"{sample_id}.fasta",
            f"{sample_id}.fasta.fai",
            f"{sample_id}.cram",
            f"{sample_id}.cram.crai",
            f"{sample_id}-pilon-m0.05.vcf.gz",
            f"{sample_id}-pilon-m0.05.vcf.gz.csi",
            f"{sample_id}-pilon-m0.1.vcf.gz",
            f"{sample_id}-pilon-m0.1.vcf.gz.csi",
            f"{sample_id}-pilon-m0.15.vcf.gz",
            f"{sample_id}-pilon-m0.15.vcf.gz.csi",
            f"{sample_id}-pilon-m0.2.vcf.gz",
            f"{sample_id}-pilon-m0.2.vcf.gz.csi",
            f"{sample_id}-pilon-m0.3.vcf.gz",
            f"{sample_id}-pilon-m0.3.vcf.gz.csi",
            f"{sample_id}-pilon-m0.4.vcf.gz",
            f"{sample_id}-pilon-m0.4.vcf.gz.csi",
        ]:
            (sample_dir / filename).write_text("placeholder", encoding="utf-8")

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
            "SAMPLE001.fasta.fai",
            "SAMPLE001.cram",
            "SAMPLE001.cram.crai",
            "SAMPLE001-pilon-m0.05.vcf.gz",
            "SAMPLE001-pilon-m0.05.vcf.gz.csi",
            "SAMPLE001-pilon-m0.1.vcf.gz",
            "SAMPLE001-pilon-m0.1.vcf.gz.csi",
            "SAMPLE001-pilon-m0.15.vcf.gz",
            "SAMPLE001-pilon-m0.15.vcf.gz.csi",
            "SAMPLE001-pilon-m0.2.vcf.gz",
            "SAMPLE001-pilon-m0.2.vcf.gz.csi",
            "SAMPLE001-pilon-m0.3.vcf.gz",
            "SAMPLE001-pilon-m0.3.vcf.gz.csi",
            "SAMPLE001-pilon-m0.4.vcf.gz",
            "SAMPLE001-pilon-m0.4.vcf.gz.csi",
            "SAMPLE001-coverage.tsv",
            "SAMPLE001.vadr.bed",
            "SAMPLE001_resistance.gff",
            "SAMPLE001.vadr.pass_mod.gff",
            "SAMPLE001.fasta.blast",
        ]:
            (self.sample_dir / filename).write_text("placeholder", encoding="utf-8")
        (self.sample_dir / "SAMPLE001.fasta").write_text(">SAMPLE001\nACGT\n", encoding="utf-8")
        (self.sample_dir / "SAMPLE001-0.15-iupac.fasta").write_text(
            ">SAMPLE001-0.15-iupac\nARYT\n",
            encoding="utf-8",
        )
        (self.sample_dir / "lid").mkdir(parents=True)
        (self.sample_dir / "lid" / "LID001-2limsrs.txt").write_text(
            "sample_id\tparameter_name\tparameter_value\tcomment\n"
            "LID001\thcvtyp\tHCV genotyp 3a\t\n",
            encoding="utf-8",
        )
        (self.sample_dir / "lid" / "LID001_rug_kde_plot.png").write_text(
            "cached image",
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

    def test_import_run_accepts_flat_sample_layout(self) -> None:
        run_dir = self.root / "flat_run"
        sample_dir = run_dir / "SAMPLE001"
        sample_dir.mkdir(parents=True)
        sample = json.loads(json.dumps(json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))[0]))
        sample["run_name"] = "flat_run"
        sample["sample_run_id"] = "SAMPLE001_flat_run"
        (sample_dir / "SAMPLE001_qc_summary.json").write_text(json.dumps(sample), encoding="utf-8")
        self.write_required_sidecar_outputs(sample_dir, "SAMPLE001")

        config = load_config(self.config_path)
        imported = import_run(config, run_dir)

        conn = connect(config.database.path)
        try:
            sample_row = get_sample(conn, "SAMPLE001_flat_run")
        finally:
            conn.close()

        self.assertEqual(imported, 1)
        self.assertIsNotNone(sample_row)
        assert sample_row is not None
        self.assertEqual(sample_row["sample_results_relpath"], "flat_run/SAMPLE001")

    def test_import_run_rejects_ambiguous_flat_and_legacy_layouts(self) -> None:
        flat_summary = self.run_dir / "SAMPLE001" / "SAMPLE001_qc_summary.json"
        flat_summary.write_text((self.sample_dir / "SAMPLE001_qc_summary.json").read_text(encoding="utf-8"), encoding="utf-8")

        config = load_config(self.config_path)
        with self.assertRaisesRegex(ValueError, "Ambiguous QC summary layout"):
            import_run(config, self.run_dir)

    def test_import_run_fails_when_required_sidecar_is_missing(self) -> None:
        (self.sample_dir / "SAMPLE001.cram.crai").unlink()

        config = load_config(self.config_path)
        with self.assertRaisesRegex(FileNotFoundError, "main_cram_index"):
            import_run(config, self.run_dir)

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
        self.write_required_sidecar_outputs(summary_path.parent, sample["sample_id"])

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

    def test_import_run_ignores_non_numeric_clarity_metadata_values(self) -> None:
        fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        fixture[0]["sample_metadata"] = {}
        self.write_run_summaries([fixture[0]])
        clarity_path = self.write_clarity_sample_info(
            {
                "sample_1": {
                    "clarity_sample_id": "SAMPLE001",
                    "CT": "Undetermined",
                    "Library concentration (ng/ul)": 5.6,
                    "Library fragment length (bp)": "Undetermined",
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
        self.assertIsNone(sample["sample_metadata_ct"])
        self.assertEqual(sample["sample_metadata_library_concentration_ng_ul"], 5.6)
        self.assertIsNone(sample["sample_metadata_library_fragment_length_bp"])

    def test_import_run_uses_run_local_clarity_sample_info_when_metadata_is_missing(self) -> None:
        fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        fixture[0]["sample_metadata"] = {}
        self.write_run_summaries([fixture[0]])
        (self.run_dir / "clarity_sample_info.json").write_text(
            json.dumps(
                {
                    "sample_1": {
                        "clarity_sample_id": "SAMPLE001",
                        "CT": 24.8,
                        "Library concentration (ng/ul)": 5.6,
                        "Library fragment length (bp)": 387,
                    }
                }
            ),
            encoding="utf-8",
        )

        config = load_config(self.config_path)
        imported = import_run(config, self.run_dir)
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

    def test_import_run_uses_pipeline_info_clarity_sample_info_when_metadata_is_missing(self) -> None:
        fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        fixture[0]["sample_metadata"] = {}
        self.write_run_summaries([fixture[0]])
        pipeline_info_dir = self.run_dir / "pipeline_info"
        pipeline_info_dir.mkdir()
        (pipeline_info_dir / "clarity_sample_info.json").write_text(
            json.dumps(
                {
                    "sample_1": {
                        "clarity_sample_id": "SAMPLE001",
                        "CT": 24.8,
                        "Library concentration (ng/ul)": 5.6,
                        "Library fragment length (bp)": 387,
                    }
                }
            ),
            encoding="utf-8",
        )

        config = load_config(self.config_path)
        report = import_run_with_report(config, self.run_dir)
        self.assertEqual(report.imported, 1)
        self.assertEqual(report.warnings, [])

        conn = connect(config.database.path)
        try:
            sample = get_sample(conn, "SAMPLE001_fixture_run")
        finally:
            conn.close()

        self.assertIsNotNone(sample)
        self.assertEqual(sample["sample_metadata_ct"], 24.8)
        self.assertEqual(sample["sample_metadata_library_concentration_ng_ul"], 5.6)
        self.assertEqual(sample["sample_metadata_library_fragment_length_bp"], 387)

    def test_import_run_matches_configured_clarity_metadata_without_run_number(self) -> None:
        run_name = "260601_A01932_0123_AHFNTGDMX2"
        run_dir = self.root / run_name
        sample_dir = run_dir / "SAMPLE001" / "results"
        sample_dir.mkdir(parents=True)
        fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        sample_summary = fixture[0]
        sample_summary["run_name"] = run_name
        sample_summary["sample_run_id"] = f"SAMPLE001_{run_name}"
        sample_summary["sample_metadata"] = {}
        (sample_dir / "SAMPLE001_qc_summary.json").write_text(
            json.dumps(sample_summary),
            encoding="utf-8",
        )
        self.write_required_sidecar_outputs(sample_dir, "SAMPLE001")
        (sample_dir / "SAMPLE001-0.15-iupac.fasta").write_text(
            ">SAMPLE001-0.15-iupac\nACGT\n",
            encoding="utf-8",
        )
        (sample_dir / "SAMPLE001_display_rug_kde_plot.png").write_text(
            "placeholder",
            encoding="utf-8",
        )
        clarity_root = self.tmp_path / "clarity_metadata"
        clarity_root.mkdir()
        (clarity_root / "NovaSeqX_260601_A01932_AHFNTGDMX2.json").write_text(
            json.dumps(
                {
                    "sample_1": {
                        "clarity_sample_id": "SAMPLE001",
                        "CT": 24.8,
                        "Library concentration (ng/ul)": 5.6,
                        "Library fragment length (bp)": 387,
                    }
                }
            ),
            encoding="utf-8",
        )
        with self.config_path.open("a", encoding="utf-8") as handle:
            handle.write("\n[imports]\n")
            handle.write(f'clarity_metadata_root = "{clarity_root.as_posix()}"\n')

        config = load_config(self.config_path)
        report = import_run_with_report(config, run_dir)
        self.assertEqual(report.imported, 1)
        self.assertEqual(report.warnings, [])

        conn = connect(config.database.path)
        try:
            sample = get_sample(conn, f"SAMPLE001_{run_name}")
        finally:
            conn.close()

        self.assertIsNotNone(sample)
        self.assertEqual(sample["sample_metadata_ct"], 24.8)
        self.assertEqual(sample["sample_metadata_library_concentration_ng_ul"], 5.6)
        self.assertEqual(sample["sample_metadata_library_fragment_length_bp"], 387)

    def test_import_run_matches_configured_clarity_metadata_for_last_concatenated_run(self) -> None:
        run_name = "260531_A01932_0122_AHAAAAAA+260601_A01932_0123_AHFNTGDMX2"
        run_dir = self.root / run_name
        sample_dir = run_dir / "SAMPLE001" / "results"
        sample_dir.mkdir(parents=True)
        fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        sample_summary = fixture[0]
        sample_summary["run_name"] = run_name
        sample_summary["sample_run_id"] = f"SAMPLE001_{run_name}"
        sample_summary["sample_metadata"] = {}
        (sample_dir / "SAMPLE001_qc_summary.json").write_text(
            json.dumps(sample_summary),
            encoding="utf-8",
        )
        self.write_required_sidecar_outputs(sample_dir, "SAMPLE001")
        (sample_dir / "SAMPLE001-0.15-iupac.fasta").write_text(
            ">SAMPLE001-0.15-iupac\nACGT\n",
            encoding="utf-8",
        )
        (sample_dir / "SAMPLE001_display_rug_kde_plot.png").write_text(
            "placeholder",
            encoding="utf-8",
        )
        clarity_root = self.tmp_path / "clarity_metadata"
        clarity_root.mkdir()
        (clarity_root / "NovaSeqX_260601_A01932_AHFNTGDMX2.json").write_text(
            json.dumps(
                {
                    "sample_1": {
                        "clarity_sample_id": "SAMPLE001",
                        "CT": 24.8,
                        "Library concentration (ng/ul)": 5.6,
                        "Library fragment length (bp)": 387,
                    }
                }
            ),
            encoding="utf-8",
        )
        with self.config_path.open("a", encoding="utf-8") as handle:
            handle.write("\n[imports]\n")
            handle.write(f'clarity_metadata_root = "{clarity_root.as_posix()}"\n')

        config = load_config(self.config_path)
        report = import_run_with_report(config, run_dir)
        self.assertEqual(report.imported, 1)
        self.assertEqual(report.warnings, [])

        conn = connect(config.database.path)
        try:
            sample = get_sample(conn, f"SAMPLE001_{run_name}")
        finally:
            conn.close()

        self.assertIsNotNone(sample)
        self.assertEqual(sample["sample_metadata_ct"], 24.8)
        self.assertEqual(sample["sample_metadata_library_concentration_ng_ul"], 5.6)
        self.assertEqual(sample["sample_metadata_library_fragment_length_bp"], 387)

    def test_import_run_reports_warning_when_clarity_metadata_file_is_missing(self) -> None:
        fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        fixture[0]["sample_metadata"] = {}
        self.write_run_summaries([fixture[0]])

        config = load_config(self.config_path)
        report = import_run_with_report(config, self.run_dir)

        self.assertEqual(report.imported, 1)
        self.assertEqual(len(report.warnings), 1)
        self.assertIn("No Clarity metadata file found for run fixture_run", report.warnings[0])
        self.assertIn("SAMPLE001", report.warnings[0])
        self.assertIn("pipeline_info/clarity_sample_info.json", report.warnings[0])

    def test_import_run_reports_warning_when_clarity_entry_is_missing(self) -> None:
        fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        fixture[0]["sample_metadata"] = {}
        self.write_run_summaries([fixture[0]])
        clarity_path = self.write_clarity_sample_info(
            {
                "sample_1": {
                    "clarity_sample_id": "OTHER_SAMPLE",
                    "CT": 24.8,
                    "Library concentration (ng/ul)": 5.6,
                    "Library fragment length (bp)": 387,
                }
            }
        )

        config = load_config(self.config_path)
        report = import_run_with_report(config, self.run_dir, clarity_path)

        self.assertEqual(report.imported, 1)
        self.assertEqual(len(report.warnings), 1)
        self.assertIn("No Clarity metadata entry for SAMPLE001", report.warnings[0])
        self.assertIn(str(clarity_path), report.warnings[0])

    def test_import_run_reports_warning_when_clarity_entry_is_incomplete(self) -> None:
        fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        fixture[0]["sample_metadata"] = {}
        self.write_run_summaries([fixture[0]])
        clarity_path = self.write_clarity_sample_info(
            {
                "sample_1": {
                    "clarity_sample_id": "SAMPLE001",
                    "CT": "Undetermined",
                    "Library concentration (ng/ul)": 5.6,
                    "Library fragment length (bp)": "",
                }
            }
        )

        config = load_config(self.config_path)
        report = import_run_with_report(config, self.run_dir, clarity_path)

        self.assertEqual(report.imported, 1)
        self.assertEqual(len(report.warnings), 1)
        self.assertIn("Incomplete Clarity metadata for SAMPLE001", report.warnings[0])
        self.assertIn("CT", report.warnings[0])
        self.assertIn("library fragment length", report.warnings[0])

    def test_import_run_accepts_display_metadata_keys_from_qc_summary(self) -> None:
        fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        fixture[0]["sample_metadata"] = {
            "CT": "24.8",
            "Library concentration (ng/ul)": "5.6",
            "Library fragment length (bp)": "387",
        }
        self.write_run_summaries([fixture[0]])

        config = load_config(self.config_path)
        imported = import_run(config, self.run_dir)
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

    def test_import_run_uses_main_blast_genotype_as_subtype(self) -> None:
        fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        fixture[0]["typing"]["main_blast_genotype"] = "2i"
        fixture[0]["typing"]["report_subtype"] = "2a"
        self.write_run_summaries([fixture[0]])

        config = load_config(self.config_path)
        import_run(config, self.run_dir)

        conn = connect(config.database.path)
        try:
            sample = get_sample(conn, "SAMPLE001_fixture_run")
            rows = list_samples(conn, subtype="2i")
        finally:
            conn.close()

        self.assertIsNotNone(sample)
        self.assertEqual(sample["typing_report_subtype"], "2i")
        self.assertEqual([row["sample_run_id"] for row in rows], ["SAMPLE001_fixture_run"])

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

    def test_cli_parser_accepts_verify_cache_all_runs(self) -> None:
        args = build_parser().parse_args(
            [
                "verify-cache",
                "--config",
                "virtitta.toml",
                "--all-runs",
                "--refresh",
            ]
        )

        self.assertEqual(args.command, "verify-cache")
        self.assertEqual(args.config, "virtitta.toml")
        self.assertTrue(args.all_runs)
        self.assertTrue(args.refresh)

    def test_cli_parser_accepts_local_user_commands(self) -> None:
        create_args = build_parser().parse_args(
            [
                "create-user",
                "--config",
                "virtitta.toml",
                "--username",
                "alice",
                "--role",
                "reviewer",
                "--password",
                "secret",
            ]
        )
        role_args = build_parser().parse_args(
            [
                "set-user-role",
                "--config",
                "virtitta.toml",
                "--username",
                "alice",
                "--role",
                "viewer",
            ]
        )

        self.assertEqual(create_args.command, "create-user")
        self.assertEqual(create_args.role, "reviewer")
        self.assertEqual(role_args.command, "set-user-role")
        self.assertEqual(role_args.role, "viewer")

    def test_load_config_reads_annotation_categories_and_default_category_column(self) -> None:
        config = load_config(self.config_path)
        self.assertEqual(config.annotations.sample_categories, ["production", "validation", "EQA", "test"])
        self.assertEqual(config.annotations.restricted_sample_categories, ["test"])
        self.assertEqual(config.ui.column_labels["sequencing_date"], "Date")
        self.assertEqual(config.ui.column_labels["generated_date"], "Import Date")
        self.assertEqual(config.ui.column_labels["variant_af_count_005"], "af 0.05")
        self.assertEqual(config.ui.column_labels["variant_af_count_01"], "af 0.1")
        self.assertEqual(config.ui.column_labels["variant_af_count_015"], "af 0.15")
        self.assertEqual(config.ui.column_labels["variant_af_count_02"], "af 0.2")
        self.assertEqual(config.ui.column_labels["variant_af_count_03"], "af 0.3")
        self.assertEqual(config.ui.column_labels["variant_af_count_04"], "af 0.4")
        self.assertEqual(config.ui.column_max_widths, {})
        self.assertEqual(
            config.cache.output_keys,
            ["main_fasta", "iupac_fasta", "display_rug_kde_plot"],
        )
        self.assertEqual(config.cache.outputs_root, self.tmp_path / "data" / "output_cache")
        self.assertFalse(config.auth.enabled)
        self.assertEqual(config.auth.provider, "local")
        self.assertEqual(config.auth.session_days, 7)
        self.assertEqual(config.auth.cookie_name, "virtitta_session")
        self.assertIn("qc_coverage_1000x_pct", config.ui.table_columns)
        self.assertIn("variant_af_count_005", config.ui.table_columns)
        self.assertEqual(table_columns(config), config.ui.table_columns)
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

    def test_load_config_preserves_legacy_column_order_without_table_columns(self) -> None:
        config = load_config(self.config_path)
        self.assertEqual(config.ui.table_columns[: len(config.ui.visible_columns)], config.ui.visible_columns)
        self.assertEqual(config.ui.table_columns[-7], "qc_coverage_1000x_pct")
        self.assertEqual(config.ui.table_columns[-6:], [
            "variant_af_count_005",
            "variant_af_count_01",
            "variant_af_count_015",
            "variant_af_count_02",
            "variant_af_count_03",
            "variant_af_count_04",
        ])

    def test_column_visibility_storage_key_changes_with_column_defaults(self) -> None:
        config = load_config(self.config_path)
        original_key = column_visibility_storage_key(config)

        self.config_path.write_text(
            self.config_path.read_text(encoding="utf-8")
            + "\n".join(
                [
                    "",
                    "[ui]",
                    'table_columns = ["lid", "variant_af_count_005", "sample_id"]',
                    'visible_columns = ["lid", "sample_id"]',
                    'default_sort = "run_name"',
                    "default_sort_desc = true",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        changed_config = load_config(self.config_path)

        self.assertNotEqual(original_key, column_visibility_storage_key(changed_config))

    def test_load_config_reads_column_max_widths(self) -> None:
        self.config_path.write_text(
            self.config_path.read_text(encoding="utf-8")
            + "\n".join(
                [
                    "",
                    "[ui.column_max_widths]",
                    'run_name = "180px"',
                    'sample_id = "12rem"',
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        config = load_config(self.config_path)

        self.assertEqual(config.ui.column_max_widths["run_name"], "180px")
        self.assertEqual(column_style(config, "run_name"), "--column-max-width:180px;")
        self.assertEqual(column_style(config, "lid"), "")

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
        self.assertIn('data-selected-only', rendered)
        self.assertIn('data-selected-count>Selected 0', rendered)
        self.assertIn("virtitta.selectedSampleIds.v1", rendered)
        self.assertIn('id="app-toast"', rendered)
        self.assertIn("window.showAppToast", rendered)
        self.assertNotIn('id="client-notice"', rendered)
        self.assertIn("applyTableViewFilter", rendered)
        self.assertIn("addHiddenInputsForFilteredSelection", rendered)

    def test_index_route_renders_cluster_duplicate_option_when_enabled(self) -> None:
        self.enable_cluster()
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
        self.assertIn("<summary>Cluster...</summary>", rendered)
        self.assertIn(">Cluster selected</button>", rendered)
        self.assertIn('name="allow_duplicate_ids" value="true"', rendered)
        self.assertIn(">Cluster selected (allow duplicates)</button>", rendered)

    def test_index_route_renders_server_messages_as_toasts(self) -> None:
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
                "query_string": b"notice=Saved",
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
            warning="",
            notice="Saved",
            min_coverage_pct="",
            min_mean_depth="",
            min_blast_identity="",
            max_ct="",
            sort="run_name",
            desc=True,
        )

        rendered = response.body.decode("utf-8")
        self.assertIn('class="toast-region"', rendered)
        self.assertIn('{ message: "Saved", isError: false }', rendered)
        self.assertIn("window.history.replaceState", rendered)
        self.assertNotIn("data-flash-message", rendered)

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
        self.assertIn("virtitta.columnVisibility.v2.", rendered)
        self.assertIn("applySavedColumnVisibility();", rendered)
        self.assertIn('data-filter-form', rendered)
        self.assertIn('name="run_name" class="js-auto-submit-filter"', rendered)
        self.assertIn("filterForm.requestSubmit();", rendered)
        self.assertIn('data-export-value="Run"', rendered)
        self.assertIn('data-export-value="Comments"', rendered)
        self.assertIn("<summary>Manage...</summary>", rendered)
        self.assertIn(">Mark pass</button>", rendered)
        self.assertIn(">Clear category</button>", rendered)
        self.assertIn(">Set category: production</button>", rendered)
        self.assertIn(">Set category: validation</button>", rendered)
        self.assertIn('name="sample_category" value="production"', rendered)
        self.assertIn("data-confirm-multiple=", rendered)
        self.assertIn("data-confirm-always=", rendered)
        self.assertIn("confirmBulkAction(event.submitter, selectedSampleIds().length)", rendered)
        self.assertNotIn("Apply category", rendered)
        self.assertIn("Add group", rendered)

    def test_index_route_uses_configured_table_column_order_for_hidden_columns(self) -> None:
        self.config_path.write_text(
            self.config_path.read_text(encoding="utf-8")
            + "\n".join(
                [
                    "",
                    "[ui]",
                    'table_columns = ["lid", "qc_coverage_1000x_pct", "sample_id", "run_name"]',
                    'visible_columns = ["lid", "sample_id", "run_name"]',
                    'default_sort = "run_name"',
                    "default_sort_desc = true",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
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
        header = rendered.split("<thead>", 1)[1].split("</thead>", 1)[0]
        self.assertLess(header.index("col-lid"), header.index("col-qc_coverage_1000x_pct"))
        self.assertLess(header.index("col-qc_coverage_1000x_pct"), header.index("col-sample_id"))
        self.assertIn('col-qc_coverage_1000x_pct  col-hidden', header)

    def test_index_route_renders_column_width_cap_and_hover_title(self) -> None:
        self.config_path.write_text(
            self.config_path.read_text(encoding="utf-8")
            + "\n".join(
                [
                    "",
                    "[ui.column_max_widths]",
                    'run_name = "180px"',
                    'comment_count = "120px"',
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        config = load_config(self.config_path)
        import_run(config, self.run_dir)
        conn = connect(config.database.path)
        try:
            add_comment(conn, "SAMPLE001_fixture_run", "First comment body", "alice")
            add_comment(conn, "SAMPLE001_fixture_run", "Second comment body", "bob")
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
        self.assertIn('class="col-run_name', rendered)
        self.assertIn('style="--column-max-width:180px;"', rendered)
        self.assertIn('class="col-comment_count"', rendered)
        self.assertIn('style="--column-max-width:120px;" data-export-value="bob: Second comment body | alice: First comment body"', rendered)
        self.assertIn('class="cell-content " title="fixture_run">fixture_run</span>', rendered)

    def test_index_route_exports_full_category_and_clean_header_labels(self) -> None:
        config = load_config(self.config_path)
        import_run(config, self.run_dir)
        conn = connect(config.database.path)
        try:
            set_sample_category(conn, ["SAMPLE001_fixture_run"], "production")
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
        self.assertIn('data-export-value="Run"', rendered)
        self.assertIn('data-export-value="Cat"', rendered)
        self.assertIn('data-export-value="production"', rendered)
        self.assertIn('title="production">Prod</span>', rendered)

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

    def test_webigv_config_uses_indexed_core_tracks(self) -> None:
        self.enable_webigv()
        config = load_config(self.config_path)
        import_run(config, self.run_dir)
        app = create_app(self.config_path)
        request = self.make_request(app)
        conn = connect(config.database.path)
        try:
            sample = get_sample(conn, "SAMPLE001_fixture_run")
        finally:
            conn.close()

        self.assertIsNotNone(sample)
        browser_config = build_webigv_browser_config(
            config,
            request,
            sample,
            locus="SAMPLE001:6550-6552",
        )
        self.assertEqual(browser_config["locus"], "SAMPLE001:6550-6552")
        self.assertIn(
            "/samples/SAMPLE001_fixture_run/webigv/files/main_fasta/SAMPLE001.fasta",
            browser_config["reference"]["fastaURL"],
        )
        self.assertIn(
            "/samples/SAMPLE001_fixture_run/webigv/files/main_fasta_index/SAMPLE001.fasta.fai",
            browser_config["reference"]["indexURL"],
        )
        track_names = [track["name"] for track in browser_config["tracks"]]
        self.assertIn("Main CRAM", track_names)
        cram_track = next(track for track in browser_config["tracks"] if track["name"] == "Main CRAM")
        self.assertIs(cram_track["checkSequenceMD5"], False)
        self.assertIs(cram_track["showSoftClips"], True)
        self.assertIn("VADR BED", track_names)
        self.assertIn("VCF m0.05", track_names)

    def test_webigv_config_loads_vcf_with_existing_csi_sidecar(self) -> None:
        (self.sample_dir / "SAMPLE001-pilon-m0.05.vcf.gz.csi").write_text("placeholder", encoding="utf-8")

        self.enable_webigv()
        config = load_config(self.config_path)
        import_run(config, self.run_dir)
        app = create_app(self.config_path)
        request = self.make_request(app)
        conn = connect(config.database.path)
        try:
            sample = get_sample(conn, "SAMPLE001_fixture_run")
        finally:
            conn.close()

        self.assertIsNotNone(sample)
        browser_config = build_webigv_browser_config(config, request, sample)
        vcf_tracks = [track for track in browser_config["tracks"] if track["name"] == "VCF m0.05"]
        self.assertEqual(len(vcf_tracks), 1)
        self.assertIn(
            "/samples/SAMPLE001_fixture_run/webigv/files/filtered_vcf_m005/SAMPLE001-pilon-m0.05.vcf.gz",
            vcf_tracks[0]["url"],
        )
        self.assertIn(
            "/samples/SAMPLE001_fixture_run/webigv/files/filtered_vcf_m005_index/SAMPLE001-pilon-m0.05.vcf.gz.csi",
            vcf_tracks[0]["indexURL"],
        )

    def test_webigv_config_loads_vcf_only_with_explicit_index_output(self) -> None:
        fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        fixture[0]["outputs"]["filtered_vcf_m005_index"] = "SAMPLE001-pilon-m0.05.vcf.gz.csi"
        self.write_run_summaries([fixture[0]])
        (self.sample_dir / "SAMPLE001-pilon-m0.05.vcf.gz.csi").write_text("placeholder", encoding="utf-8")

        self.enable_webigv()
        config = load_config(self.config_path)
        import_run(config, self.run_dir)
        app = create_app(self.config_path)
        request = self.make_request(app)
        conn = connect(config.database.path)
        try:
            sample = get_sample(conn, "SAMPLE001_fixture_run")
        finally:
            conn.close()

        self.assertIsNotNone(sample)
        browser_config = build_webigv_browser_config(config, request, sample)
        vcf_tracks = [track for track in browser_config["tracks"] if track["name"] == "VCF m0.05"]
        self.assertEqual(len(vcf_tracks), 1)
        self.assertIn(
            "/samples/SAMPLE001_fixture_run/webigv/files/filtered_vcf_m005/SAMPLE001-pilon-m0.05.vcf.gz",
            vcf_tracks[0]["url"],
        )
        self.assertIn(
            "/samples/SAMPLE001_fixture_run/webigv/files/filtered_vcf_m005_index/SAMPLE001-pilon-m0.05.vcf.gz.csi",
            vcf_tracks[0]["indexURL"],
        )

    def test_webigv_routes_are_registered_when_enabled(self) -> None:
        self.enable_webigv()
        config = load_config(self.config_path)
        import_run(config, self.run_dir)
        app = create_app(self.config_path)

        route_paths = {route.path for route in app.routes}
        self.assertIn("/samples/{sample_run_id}/webigv", route_paths)
        self.assertIn("/samples/{sample_run_id}/webigv/files/{output_key}", route_paths)
        self.assertIn("/samples/{sample_run_id}/webigv/files/{output_key}/{filename}", route_paths)

    def test_cluster_config_parses_defaults_and_grapetree_url(self) -> None:
        self.enable_cluster()
        config = load_config(self.config_path)

        self.assertTrue(config.cluster.enabled)
        self.assertEqual(config.cluster.output_root, self.tmp_path / "clusters")
        self.assertEqual(config.cluster.grapetree_url, "https://mtlucmds1.lund.skane.se/grapetree/")
        self.assertEqual(config.cluster.public_base_url, "")
        self.assertEqual(config.cluster.input_output_key, "iupac_fasta")
        self.assertEqual(config.cluster.iqtree_command, "iqtree3")
        self.assertEqual(config.cluster.mafft_args, ["--auto"])
        self.assertEqual(config.cluster.iqtree_threads, 4)
        self.assertTrue(config.cluster.poly_t)
        self.assertEqual(config.cluster.poly_t_min_length, 10)
        self.assertEqual(config.cluster.poly_t_seed_length, 12)
        self.assertEqual(config.cluster.poly_t_seed_min_t, 10)
        self.assertEqual(config.cluster.poly_t_max_trailing_bases, 100)

    def test_prepare_cluster_files_strips_header_suffix_and_writes_metadata(self) -> None:
        self.enable_cluster()
        self.add_second_sample_summary(subtype="1a")
        config = load_config(self.config_path)
        import_run(config, self.run_dir)
        conn = connect(config.database.path)
        try:
            rows = [
                get_sample(conn, "SAMPLE001_fixture_run"),
                get_sample(conn, "SAMPLE002_fixture_run"),
            ]
            sample_records, warning_text = prepare_cluster_files(config, conn, rows, "job1")
        finally:
            conn.close()

        self.assertEqual([record["tree_id"] for record in sample_records], ["LID001", "LID002"])
        self.assertIn("multiple subtypes", warning_text)
        prepared_input = (config.cluster.output_root / "job1" / "input.raw.fasta").read_text(encoding="utf-8")
        metadata = (config.cluster.output_root / "job1" / "metadata.tsv").read_text(encoding="utf-8")
        self.assertIn(">LID001\nARYT\n", prepared_input)
        self.assertIn(">LID002\nACGTAAAA\n", prepared_input)
        self.assertIn("ID\tlid\tsample_id\tsequencing_date\tgenerated_date\tsample_category\tqc_status\tmanual_groups", metadata)
        self.assertIn("typing_report_subtype\ttyping_main_blast_identity\tresistance_summary", metadata)
        self.assertIn("comment_count", metadata)
        self.assertIn("LID002\tLID002\tSAMPLE002\t2026-04-08\t2026-04-08\t\tunreviewed\t\t1a", metadata)

    def test_prepare_cluster_files_blocks_duplicate_metadata_tree_ids(self) -> None:
        self.enable_cluster()
        self.add_second_sample_summary(lid="LID001", tree_id="LID002-0.15-iupac")
        config = load_config(self.config_path)
        import_run(config, self.run_dir)
        conn = connect(config.database.path)
        try:
            rows = [
                get_sample(conn, "SAMPLE001_fixture_run"),
                get_sample(conn, "SAMPLE002_fixture_run"),
            ]
            with self.assertRaisesRegex(ClusterError, "Duplicate FASTA tree ID"):
                prepare_cluster_files(config, conn, rows, "job1")
        finally:
            conn.close()

    def test_prepare_cluster_files_can_suffix_duplicate_tree_ids_with_run_name(self) -> None:
        self.enable_cluster()
        second_run_dir = self.root / "fixture_run_2"
        fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))[0]
        sample = json.loads(json.dumps(fixture))
        sample["run_name"] = "fixture_run_2"
        sample["sample_id"] = "SAMPLE002"
        sample["sample_run_id"] = "SAMPLE002_fixture_run_2"
        sample["lid"] = "LID001"
        sample["outputs"] = dict(sample["outputs"])
        for key, value in list(sample["outputs"].items()):
            if isinstance(value, str):
                sample["outputs"][key] = value.replace("SAMPLE001", "SAMPLE002")
        sample["outputs"]["export_iupac_fasta"] = "lid/LID001-0.15-iupac.fasta"
        summary_path = second_run_dir / "SAMPLE002" / "results" / "SAMPLE002_qc_summary.json"
        summary_path.parent.mkdir(parents=True, exist_ok=True)
        summary_path.write_text(json.dumps(sample), encoding="utf-8")
        sample2_dir = second_run_dir / "SAMPLE002" / "results"
        for filename in [
            "SAMPLE002_rug_kde_plot.png",
            "SAMPLE002.fasta",
            "SAMPLE002.fasta.fai",
            "SAMPLE002.cram",
            "SAMPLE002.cram.crai",
            "SAMPLE002-pilon-m0.05.vcf.gz",
            "SAMPLE002-pilon-m0.05.vcf.gz.csi",
            "SAMPLE002-pilon-m0.1.vcf.gz",
            "SAMPLE002-pilon-m0.1.vcf.gz.csi",
            "SAMPLE002-pilon-m0.15.vcf.gz",
            "SAMPLE002-pilon-m0.15.vcf.gz.csi",
            "SAMPLE002-pilon-m0.2.vcf.gz",
            "SAMPLE002-pilon-m0.2.vcf.gz.csi",
            "SAMPLE002-pilon-m0.3.vcf.gz",
            "SAMPLE002-pilon-m0.3.vcf.gz.csi",
            "SAMPLE002-pilon-m0.4.vcf.gz",
            "SAMPLE002-pilon-m0.4.vcf.gz.csi",
            "SAMPLE002.vadr.bed",
            "SAMPLE002_resistance.gff",
            "SAMPLE002.vadr.pass_mod.gff",
            "SAMPLE002.fasta.blast",
        ]:
            (sample2_dir / filename).write_text("placeholder", encoding="utf-8")
        (sample2_dir / "SAMPLE002-0.15-iupac.fasta").write_text(
            ">SAMPLE002-0.15-iupac\nACGTAAAA\n",
            encoding="utf-8",
        )
        lid_dir = second_run_dir / "SAMPLE002" / "results" / "lid"
        lid_dir.mkdir(parents=True, exist_ok=True)
        (lid_dir / "LID001-0.15-iupac.fasta").write_text(">LID001-0.15-iupac\nACGTAAAA\n", encoding="utf-8")

        config = load_config(self.config_path)
        import_run(config, self.run_dir)
        import_run(config, second_run_dir)
        conn = connect(config.database.path)
        try:
            rows = [
                get_sample(conn, "SAMPLE001_fixture_run"),
                get_sample(conn, "SAMPLE002_fixture_run_2"),
            ]
            sample_records, warning_text = prepare_cluster_files(
                config,
                conn,
                rows,
                "job1",
                allow_duplicate_ids=True,
            )
        finally:
            conn.close()

        self.assertEqual(
            [record["tree_id"] for record in sample_records],
            ["LID001-fixture_run", "LID001-fixture_run_2"],
        )
        self.assertIn("renamed with run name suffixes: LID001", warning_text)
        prepared_input = (config.cluster.output_root / "job1" / "input.raw.fasta").read_text(encoding="utf-8")
        metadata = (config.cluster.output_root / "job1" / "metadata.tsv").read_text(encoding="utf-8")
        self.assertIn(">LID001-fixture_run\nARYT\n", prepared_input)
        self.assertIn(">LID001-fixture_run_2\nACGTAAAA\n", prepared_input)
        self.assertIn("LID001-fixture_run_2\tLID001\tSAMPLE002", metadata)

    def test_run_cluster_job_uses_configured_tools_and_completes(self) -> None:
        mafft, iqtree = self.write_fake_cluster_tools()
        self.enable_cluster(
            mafft_command=mafft.as_posix(),
            iqtree_command=iqtree.as_posix(),
            five_prime_trim=0,
        )
        self.add_second_sample_summary(subtype="3a", sequence="ACGTTTTTCTTTTTTACGT")
        config = load_config(self.config_path)
        import_run(config, self.run_dir)
        conn = connect(config.database.path)
        try:
            rows = [
                get_sample(conn, "SAMPLE001_fixture_run"),
                get_sample(conn, "SAMPLE002_fixture_run"),
            ]
            sample_records, warning_text = prepare_cluster_files(config, conn, rows, "job1")
            create_cluster_job(
                conn,
                {
                    "id": "job1",
                    "status": "queued",
                    "created_at": utc_now(),
                    "started_at": None,
                    "completed_at": None,
                    "selected_count": 2,
                    "warning_text": warning_text,
                    "error_text": None,
                    "output_relpath": "job1",
                    "artifacts_json": json.dumps(cluster_artifacts("job1"), sort_keys=True),
                    "config_json": "{}",
                    "public_token": "public-token",
                },
                sample_records,
            )
        finally:
            conn.close()

        run_cluster_job(config, "job1")

        conn = connect(config.database.path)
        try:
            job = get_cluster_job(conn, "job1")
            samples = get_cluster_job_samples(conn, "job1")
        finally:
            conn.close()
        self.assertEqual(job["status"], "completed")
        self.assertEqual(job["error_text"], None)
        self.assertEqual([sample["tree_id"] for sample in samples], ["LID001", "LID002"])
        self.assertEqual(
            (config.cluster.output_root / "job1" / "iqtree.treefile").read_text(encoding="utf-8"),
            "(LID001:0.1,LID002:0.1);\n",
        )
        self.assertIn(">LID002\nACG\n", (config.cluster.output_root / "job1" / "aligned.fasta").read_text(encoding="utf-8"))
        grapetree = json.loads((config.cluster.output_root / "job1" / "grapetree.json").read_text(encoding="utf-8"))
        self.assertEqual(grapetree["nwk"], "(LID001:0.1,LID002:0.1);")
        self.assertEqual(grapetree["layout_algorithm"], "greedy")
        self.assertEqual(sorted(grapetree["metadata"]), ["LID001", "LID002"])
        self.assertIn("sample_category", grapetree["metadata_options"])
        log_text = (config.cluster.output_root / "job1" / "cluster.log").read_text(encoding="utf-8")
        self.assertIn("--poly-t-min-length 10", log_text)
        self.assertIn("--poly-t-seed-length 12", log_text)
        self.assertIn("--poly-t-seed-min-t 10", log_text)
        self.assertIn("prepare-fasta summary:", log_text)
        self.assertIn("records: 2", log_text)
        self.assertIn("five-prime trim: disabled, 0 records, 0 bases", log_text)
        self.assertIn("poly-T trim: enabled, 1/2 records, 16 bases (0 exact-run, 1 fuzzy-seed)", log_text)
        self.assertIn("poly-T trim lengths: min=16, median=16, max=16", log_text)
        self.assertIn("poly-T event: LID002 19 -> 3 (-16 bases, fuzzy-seed)", log_text)
        self.assertIn("-T 4", log_text)

    def test_run_cluster_job_records_recent_command_output_on_failure(self) -> None:
        mafft, iqtree = self.write_fake_cluster_tools()
        iqtree.write_text(
            "#!/usr/bin/env python3\n"
            "import sys\n"
            "print('iqtree detail line 1', file=sys.stderr)\n"
            "print('iqtree detail line 2', file=sys.stderr)\n"
            "raise SystemExit(2)\n",
            encoding="utf-8",
        )
        self.enable_cluster(
            mafft_command=mafft.as_posix(),
            iqtree_command=iqtree.as_posix(),
            five_prime_trim=0,
        )
        self.add_second_sample_summary(subtype="3a", sequence="ACGT")
        config = load_config(self.config_path)
        import_run(config, self.run_dir)
        conn = connect(config.database.path)
        try:
            rows = [
                get_sample(conn, "SAMPLE001_fixture_run"),
                get_sample(conn, "SAMPLE002_fixture_run"),
            ]
            sample_records, warning_text = prepare_cluster_files(config, conn, rows, "job1")
            create_cluster_job(
                conn,
                {
                    "id": "job1",
                    "status": "queued",
                    "created_at": utc_now(),
                    "started_at": None,
                    "completed_at": None,
                    "selected_count": 2,
                    "warning_text": warning_text,
                    "error_text": None,
                    "output_relpath": "job1",
                    "artifacts_json": json.dumps(cluster_artifacts("job1"), sort_keys=True),
                    "config_json": "{}",
                    "public_token": "public-token",
                },
                sample_records,
            )
        finally:
            conn.close()

        run_cluster_job(config, "job1")

        conn = connect(config.database.path)
        try:
            job = get_cluster_job(conn, "job1")
        finally:
            conn.close()
        self.assertEqual(job["status"], "failed")
        self.assertIn("Command failed with exit code 2", job["error_text"])
        self.assertIn("Recent output: iqtree detail line 1 | iqtree detail line 2", job["error_text"])

    def test_cluster_routes_are_registered_and_grapetree_url_uses_public_artifacts(self) -> None:
        self.enable_cluster()
        config = load_config(self.config_path)
        conn = connect(config.database.path)
        try:
            init_db(conn)
            create_cluster_job(
                conn,
                {
                    "id": "job1",
                    "status": "completed",
                    "created_at": utc_now(),
                    "started_at": utc_now(),
                    "completed_at": utc_now(),
                    "selected_count": 2,
                    "warning_text": None,
                    "error_text": None,
                    "output_relpath": "job1",
                    "artifacts_json": json.dumps(cluster_artifacts("job1"), sort_keys=True),
                    "config_json": "{}",
                    "public_token": "public-token",
                },
                [],
            )
        finally:
            conn.close()
        app = create_app(self.config_path)
        request = self.make_request(app)
        job = {"id": "job1", "status": "completed", "public_token": "public-token", "selected_count": 2}
        grapetree_url = build_grapetree_url(config, request, job)
        route_paths = {route.path for route in app.routes}
        detail_route = next(route for route in app.routes if getattr(route, "path", None) == "/clusters/{job_id}")
        detail_response = detail_route.endpoint(request, "job1")
        public_route = next(
            route for route in app.routes if getattr(route, "path", None) == "/clusters/public/{public_token}/{artifact_key}"
        )
        artifact_route = next(
            route for route in app.routes if getattr(route, "path", None) == "/clusters/{job_id}/artifacts/{artifact_key}"
        )
        artifact_dir = config.cluster.output_root / "job1"
        artifact_dir.mkdir(parents=True)
        (artifact_dir / "iqtree.treefile").write_text("(LID001:0.1);\n", encoding="utf-8")
        (artifact_dir / "metadata.tsv").write_text("ID\tlid\nLID001\tLID001\n", encoding="utf-8")
        (artifact_dir / "grapetree.json").write_text('{"nwk":"(LID001:0.1);","metadata":{}}\n', encoding="utf-8")
        artifact_response = artifact_route.endpoint(request, "job1", "treefile")
        public_metadata_response = public_route.endpoint("public-token", "metadata.txt")
        public_grapetree_response = public_route.endpoint("public-token", "grapetree.json")

        self.assertIn("/clusters", route_paths)
        self.assertIn("/clusters/{job_id}", route_paths)
        self.assertIn("/clusters/{job_id}/artifacts/{artifact_key}", route_paths)
        self.assertIn("/clusters/public/{public_token}/{artifact_key}", route_paths)
        self.assertEqual(detail_response.status_code, 200)
        self.assertEqual(artifact_response.media_type, "text/plain; charset=utf-8")
        self.assertIn("inline", artifact_response.headers["content-disposition"])
        self.assertEqual(public_metadata_response.media_type, "text/plain; charset=utf-8")
        self.assertEqual(public_grapetree_response.media_type, "text/plain; charset=utf-8")
        rendered_detail = detail_response.body.decode("utf-8")
        self.assertIn("Cluster job1", rendered_detail)
        self.assertIn("cluster-clipboard-modal", rendered_detail)
        self.assertIn("Clipboard write was blocked. Manual copy dialog opened.", rendered_detail)
        self.assertTrue(grapetree_url.startswith("https://mtlucmds1.lund.skane.se/grapetree/?"))
        self.assertIn("tree=http%3A%2F%2Ftestserver%2Fclusters%2Fpublic%2Fpublic-token%2Fgrapetree.json", grapetree_url)
        self.assertNotIn("metadata=", grapetree_url)

    def test_grapetree_url_uses_configured_public_base_url(self) -> None:
        self.enable_cluster(public_base_url="https://virtitta.example.org/review")
        config = load_config(self.config_path)
        app = create_app(self.config_path)
        request = self.make_request(app)
        job = {"id": "job1", "status": "completed", "public_token": "public-token", "selected_count": 2}

        grapetree_url = build_grapetree_url(config, request, job)

        self.assertIn(
            "tree=https%3A%2F%2Fvirtitta.example.org%2Freview%2Fclusters%2Fpublic%2Fpublic-token%2Fgrapetree.json",
            grapetree_url,
        )

    def test_cluster_detail_renders_queued_job_status_script(self) -> None:
        self.enable_cluster()
        config = load_config(self.config_path)
        app = create_app(self.config_path)
        conn = connect(config.database.path)
        try:
            create_cluster_job(
                conn,
                {
                    "id": "job1",
                    "status": "queued",
                    "created_at": utc_now(),
                    "started_at": None,
                    "completed_at": None,
                    "selected_count": 2,
                    "warning_text": None,
                    "error_text": None,
                    "output_relpath": "job1",
                    "artifacts_json": json.dumps(cluster_artifacts("job1"), sort_keys=True),
                    "config_json": "{}",
                    "public_token": "public-token",
                },
                [],
            )
        finally:
            conn.close()
        route = next(route for route in app.routes if getattr(route, "path", None) == "/clusters/{job_id}")
        response = route.endpoint(self.make_request(app), "job1")

        self.assertEqual(response.status_code, 200)
        rendered = response.body.decode("utf-8")
        self.assertIn("Cluster job1", rendered)
        self.assertIn('const statusUrl = "http://testserver/clusters/job1/status";', rendered)

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
            "SAMPLE002.fasta.fai",
            "SAMPLE002.cram",
            "SAMPLE002.cram.crai",
            "SAMPLE002-pilon-m0.05.vcf.gz",
            "SAMPLE002-pilon-m0.05.vcf.gz.csi",
            "SAMPLE002-pilon-m0.1.vcf.gz",
            "SAMPLE002-pilon-m0.1.vcf.gz.csi",
            "SAMPLE002-pilon-m0.15.vcf.gz",
            "SAMPLE002-pilon-m0.15.vcf.gz.csi",
            "SAMPLE002-pilon-m0.2.vcf.gz",
            "SAMPLE002-pilon-m0.2.vcf.gz.csi",
            "SAMPLE002-pilon-m0.3.vcf.gz",
            "SAMPLE002-pilon-m0.3.vcf.gz.csi",
            "SAMPLE002-pilon-m0.4.vcf.gz",
            "SAMPLE002-pilon-m0.4.vcf.gz.csi",
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
        clear["outputs"] = dict(clear["outputs"])
        for key, value in list(clear["outputs"].items()):
            if isinstance(value, str):
                clear["outputs"][key] = value.replace("SAMPLE001", "SAMPLE002").replace("LID001", "LID002")
        self.write_run_summaries([resistant, clear])
        sample2_dir = self.run_dir / "SAMPLE002" / "results"
        self.write_required_sidecar_outputs(sample2_dir, "SAMPLE002")
        (sample2_dir / "SAMPLE002-0.15-iupac.fasta").write_text(">SAMPLE002-0.15-iupac\nARYT\n", encoding="utf-8")

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

    def test_lims_export_uses_database_values_and_appends_qc(self) -> None:
        config = load_config(self.config_path)
        import_run(config, self.run_dir)
        (self.sample_dir / "lid" / "LID001-2limsrs.txt").write_text(
            "sample_id\tparameter_name\tparameter_value\tcomment\n"
            "LID001\thcvtyp\tHCV genotyp stale\t\n"
            "LID001\tlegacy_extra\tshould not export\t\n",
            encoding="utf-8",
        )
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
        self.assertNotIn("legacy_extra", export_text)

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
        self.assertEqual(
            build_fasta_clipboard_content(config, [sample], "export_iupac_fasta", "sample_id"),
            ">SAMPLE001-0.15-iupac\nARYT\n",
        )

    def test_import_run_caches_configured_outputs(self) -> None:
        config = load_config(self.config_path)
        import_run(config, self.run_dir)
        conn = connect(config.database.path)
        try:
            entries = [
                get_output_cache_entry(conn, "SAMPLE001_fixture_run", output_key)
                for output_key in config.cache.output_keys
            ]
        finally:
            conn.close()

        self.assertTrue(all(entry is not None for entry in entries))
        for entry in entries:
            assert entry is not None
            cache_path = config.cache.outputs_root / entry["cached_relpath"]
            self.assertTrue(cache_path.is_file())
        self.assertEqual(
            (config.cache.outputs_root / entries[0]["cached_relpath"]).read_text(encoding="utf-8"),
            ">SAMPLE001\nACGT\n",
        )

    def test_fasta_clipboard_content_reads_cached_output(self) -> None:
        config = load_config(self.config_path)
        import_run(config, self.run_dir)
        (self.sample_dir / "SAMPLE001.fasta").write_text(">REMOTE\nTTTT\n", encoding="utf-8")
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

    def test_reimport_replaces_cached_output_path_without_leaving_old_file(self) -> None:
        config = load_config(self.config_path)
        import_run(config, self.run_dir)
        conn = connect(config.database.path)
        try:
            old_entry = get_output_cache_entry(conn, "SAMPLE001_fixture_run", "main_fasta")
        finally:
            conn.close()
        assert old_entry is not None
        old_cache_path = config.cache.outputs_root / old_entry["cached_relpath"]
        self.assertTrue(old_cache_path.is_file())

        fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        updated = fixture[0]
        updated["generated_at_utc"] = "2026-04-08T09:12:34Z"
        updated["outputs"] = dict(updated["outputs"])
        updated["outputs"]["main_fasta"] = "SAMPLE001-v2.fasta"
        updated["outputs"]["main_fasta_index"] = "SAMPLE001-v2.fasta.fai"
        self.write_run_summaries([updated])
        (self.sample_dir / "SAMPLE001-v2.fasta").write_text(">SAMPLE001\nTGCA\n", encoding="utf-8")
        (self.sample_dir / "SAMPLE001-v2.fasta.fai").write_text("placeholder", encoding="utf-8")

        import_run(config, self.run_dir)
        conn = connect(config.database.path)
        try:
            new_entry = get_output_cache_entry(conn, "SAMPLE001_fixture_run", "main_fasta")
        finally:
            conn.close()

        assert new_entry is not None
        self.assertFalse(old_cache_path.exists())
        self.assertEqual(new_entry["remote_relpath"], "SAMPLE001-v2.fasta")
        self.assertEqual(
            (config.cache.outputs_root / new_entry["cached_relpath"]).read_text(encoding="utf-8"),
            ">SAMPLE001\nTGCA\n",
        )

    def test_sample_file_serves_cached_kde_image(self) -> None:
        config = load_config(self.config_path)
        import_run(config, self.run_dir)
        (self.sample_dir / "lid" / "LID001_rug_kde_plot.png").unlink()

        app = create_app(self.config_path)
        route = next(route for route in app.router.routes if getattr(route, "path", None) == "/samples/{sample_run_id}/files/{output_key}")
        response = route.endpoint(self.make_request(app), "SAMPLE001_fixture_run", "display_rug_kde_plot")

        self.assertEqual(
            Path(response.path).read_text(encoding="utf-8"),
            "cached image",
        )

    def test_sample_file_view_serves_blast_inline(self) -> None:
        config = load_config(self.config_path)
        import_run(config, self.run_dir)

        app = create_app(self.config_path)
        route = next(route for route in app.router.routes if getattr(route, "path", None) == "/samples/{sample_run_id}/files/{output_key}/view")
        response = route.endpoint(self.make_request(app), "SAMPLE001_fixture_run", "main_blast")

        self.assertEqual(Path(response.path).read_text(encoding="utf-8"), "placeholder")
        self.assertIn("inline", response.headers["content-disposition"])
        self.assertIn("SAMPLE001.fasta.blast", response.headers["content-disposition"])

    def test_sample_file_view_serves_bed_and_tsv_as_inline_text(self) -> None:
        config = load_config(self.config_path)
        import_run(config, self.run_dir)

        app = create_app(self.config_path)
        route = next(route for route in app.router.routes if getattr(route, "path", None) == "/samples/{sample_run_id}/files/{output_key}/view")

        for output_key, filename in [
            ("vadr_bed", "SAMPLE001.vadr.bed"),
            ("coverage_tsv", "SAMPLE001-coverage.tsv"),
        ]:
            response = route.endpoint(self.make_request(app), "SAMPLE001_fixture_run", output_key)

            self.assertIn("text/plain", response.headers["content-type"])
            self.assertIn("inline", response.headers["content-disposition"])
            self.assertIn(filename, response.headers["content-disposition"])

    def test_sample_file_view_blocks_cram(self) -> None:
        config = load_config(self.config_path)
        import_run(config, self.run_dir)

        app = create_app(self.config_path)
        route = next(route for route in app.router.routes if getattr(route, "path", None) == "/samples/{sample_run_id}/files/{output_key}/view")
        with self.assertRaises(HTTPException) as raised:
            route.endpoint(self.make_request(app), "SAMPLE001_fixture_run", "main_cram")

        self.assertEqual(raised.exception.status_code, 404)

    def test_sample_detail_renders_blast_view_and_download_actions(self) -> None:
        config = load_config(self.config_path)
        import_run(config, self.run_dir)

        app = create_app(self.config_path)
        route = next(route for route in app.router.routes if getattr(route, "path", None) == "/samples/{sample_run_id}")
        response = route.endpoint(self.make_request(app, path="/samples/SAMPLE001_fixture_run"), "SAMPLE001_fixture_run")
        rendered = response.body.decode("utf-8")

        self.assertIn("Main BLAST", rendered)
        self.assertIn("/samples/SAMPLE001_fixture_run/files/main_blast/view", rendered)
        self.assertIn("/samples/SAMPLE001_fixture_run/files/main_blast", rendered)
        self.assertIn("SAMPLE001.fasta.blast", rendered)
        self.assertNotIn("/samples/SAMPLE001_fixture_run/files/main_cram/view", rendered)
        self.assertNotIn("/samples/SAMPLE001_fixture_run/files/display_rug_kde_plot/view", rendered)

    def test_auth_enabled_redirects_anonymous_user_to_login(self) -> None:
        self.enable_auth()
        app = create_app(self.config_path)

        messages = self.asgi_request(app)
        start = next(message for message in messages if message["type"] == "http.response.start")
        headers = {key.decode("ascii"): value.decode("ascii") for key, value in start["headers"]}

        self.assertEqual(start["status"], 303)
        self.assertIn("/login?next=%2F", headers["location"])

    def test_auth_public_path_exemption_includes_cluster_artifacts(self) -> None:
        self.assertTrue(is_public_request_path("/clusters/public/public-token/grapetree.json"))
        self.assertTrue(is_public_request_path("/clusters/public/public-token/metadata.txt"))
        self.assertFalse(is_public_request_path("/clusters/job1"))

    def test_viewer_login_can_view_but_not_see_mutating_controls(self) -> None:
        self.enable_auth()
        config = load_config(self.config_path)
        import_run(config, self.run_dir)
        self.create_local_user("viewer", "viewer")
        app = create_app(self.config_path)
        _token, user = self.login_local_user("viewer")
        route = next(route for route in app.router.routes if getattr(route, "path", None) == "/")

        response = route.endpoint(
            self.make_user_request(app, user),
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
        rendered = response.body.decode("utf-8")
        self.assertIn("viewer", rendered)
        self.assertIn("Table to clipboard", rendered)
        self.assertNotIn("Mark pass", rendered)
        self.assertNotIn("Delete samples", rendered)
        self.assertNotIn("Add group", rendered)

    def test_restricted_category_samples_are_hidden_from_viewer_table(self) -> None:
        self.enable_auth()
        config = load_config(self.config_path)
        import_run(config, self.run_dir)
        conn = connect(config.database.path)
        try:
            set_sample_category(conn, ["SAMPLE001_fixture_run"], "test")
        finally:
            conn.close()
        self.create_local_user("viewer", "viewer")
        self.create_local_user("reviewer", "reviewer")
        app = create_app(self.config_path)
        _viewer_token, viewer = self.login_local_user("viewer")
        _reviewer_token, reviewer = self.login_local_user("reviewer")
        route = next(route for route in app.router.routes if getattr(route, "path", None) == "/")

        viewer_response = route.endpoint(
            self.make_user_request(app, viewer),
            search="",
            run_name="",
            subtype="",
            qc_status="",
            sample_category=["test"],
            min_coverage_pct="",
            min_mean_depth="",
            min_blast_identity="",
            max_ct="",
            sort="run_name",
            desc=True,
        )
        reviewer_response = route.endpoint(
            self.make_user_request(app, reviewer),
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

        viewer_rendered = viewer_response.body.decode("utf-8")
        reviewer_rendered = reviewer_response.body.decode("utf-8")
        self.assertEqual(viewer_response.context["rows"], [])
        self.assertNotIn('name="sample_category" value="test"', viewer_rendered)
        self.assertNotIn("Set category: test", viewer_rendered)
        self.assertEqual(len(reviewer_response.context["rows"]), 1)
        self.assertIn('name="sample_category" value="test"', reviewer_rendered)
        self.assertIn("Set category: test", reviewer_rendered)
        self.assertIn('title="test">Test</span>', reviewer_rendered)

    def test_restricted_category_sample_detail_and_exports_are_hidden_from_viewer(self) -> None:
        self.enable_auth()
        config = load_config(self.config_path)
        import_run(config, self.run_dir)
        conn = connect(config.database.path)
        try:
            set_sample_category(conn, ["SAMPLE001_fixture_run"], "test")
        finally:
            conn.close()
        self.create_local_user("viewer", "viewer")
        self.create_local_user("reviewer", "reviewer")
        app = create_app(self.config_path)
        _viewer_token, viewer = self.login_local_user("viewer")
        _reviewer_token, reviewer = self.login_local_user("reviewer")
        detail_route = next(route for route in app.router.routes if getattr(route, "path", None) == "/samples/{sample_run_id}")
        fasta_route = next(route for route in app.router.routes if getattr(route, "path", None) == "/samples/clipboard/fasta")

        with self.assertRaises(HTTPException) as detail_error:
            detail_route.endpoint(
                self.make_user_request(app, viewer, path="/samples/SAMPLE001_fixture_run"),
                "SAMPLE001_fixture_run",
            )
        self.assertEqual(detail_error.exception.status_code, 404)
        reviewer_response = detail_route.endpoint(
            self.make_user_request(app, reviewer, path="/samples/SAMPLE001_fixture_run"),
            "SAMPLE001_fixture_run",
        )
        self.assertEqual(reviewer_response.status_code, 200)

        with self.assertRaises(HTTPException) as export_error:
            asyncio.run(
                fasta_route.endpoint(
                    self.make_user_request(app, viewer, path="/samples/clipboard/fasta", method="POST"),
                    sample_run_id=["SAMPLE001_fixture_run"],
                    header_id="lid",
                    csrf_token=viewer.csrf_token,
                )
            )
        self.assertEqual(export_error.exception.status_code, 404)

    def test_reviewer_qc_update_requires_csrf_and_records_authenticated_author(self) -> None:
        self.enable_auth()
        config = load_config(self.config_path)
        import_run(config, self.run_dir)
        self.create_local_user("reviewer", "reviewer")
        app = create_app(self.config_path)
        _token, user = self.login_local_user("reviewer")
        detail_route = next(route for route in app.router.routes if getattr(route, "path", None) == "/samples/{sample_run_id}")
        qc_route = next(route for route in app.router.routes if getattr(route, "path", None) == "/samples/qc")

        with self.assertRaises(Exception) as blocked:
            asyncio.run(
                qc_route.endpoint(
                    self.make_user_request(app, user, method="POST"),
                    sample_run_id=["SAMPLE001_fixture_run"],
                    qc_status="fail",
                    comment_body="Coverage too low",
                    redirect_to="/samples/SAMPLE001_fixture_run",
                    csrf_token="",
                )
            )
        self.assertEqual(getattr(blocked.exception, "status_code", None), 403)

        page = detail_route.endpoint(self.make_user_request(app, user, path="/samples/SAMPLE001_fixture_run"), "SAMPLE001_fixture_run")
        token = re.search(r'name="csrf_token" value="([^"]+)"', page.body.decode("utf-8"))
        self.assertIsNotNone(token)
        assert token is not None
        response = asyncio.run(
            qc_route.endpoint(
                self.make_user_request(app, user, method="POST"),
                sample_run_id=["SAMPLE001_fixture_run"],
                qc_status="fail",
                comment_body="Coverage too low",
                redirect_to="/samples/SAMPLE001_fixture_run",
                csrf_token=token.group(1),
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
        self.assertEqual(comments[0]["author"], "reviewer")

    def test_commenter_can_add_but_not_delete_comments(self) -> None:
        self.enable_auth()
        config = load_config(self.config_path)
        import_run(config, self.run_dir)
        self.create_local_user("commenter", "commenter")
        app = create_app(self.config_path)
        _token, user = self.login_local_user("commenter")
        detail_route = next(route for route in app.router.routes if getattr(route, "path", None) == "/samples/{sample_run_id}")
        add_route = next(route for route in app.router.routes if getattr(route, "path", None) == "/samples/{sample_run_id}/comments")
        delete_route = next(
            route
            for route in app.router.routes
            if getattr(route, "path", None) == "/samples/{sample_run_id}/comments/{comment_id}/delete"
        )

        page = detail_route.endpoint(self.make_user_request(app, user, path="/samples/SAMPLE001_fixture_run"), "SAMPLE001_fixture_run")
        rendered = page.body.decode("utf-8")
        self.assertIn("Add comment", rendered)
        self.assertNotIn("Delete</button>", rendered)
        token = re.search(r'name="csrf_token" value="([^"]+)"', rendered)
        self.assertIsNotNone(token)
        assert token is not None

        response = asyncio.run(
            add_route.endpoint(
                self.make_user_request(app, user, path="/samples/SAMPLE001_fixture_run/comments", method="POST"),
                "SAMPLE001_fixture_run",
                body="Looks useful",
                csrf_token=token.group(1),
            )
        )
        self.assertEqual(response.status_code, 303)

        conn = connect(config.database.path)
        try:
            comment = get_comments(conn, "SAMPLE001_fixture_run")[0]
        finally:
            conn.close()

        with self.assertRaises(Exception) as blocked_delete:
            asyncio.run(
                delete_route.endpoint(
                    self.make_user_request(app, user, method="POST"),
                    "SAMPLE001_fixture_run",
                    comment["id"],
                    csrf_token=token.group(1),
                )
            )
        self.assertEqual(getattr(blocked_delete.exception, "status_code", None), 403)

    def test_verify_cache_detects_stale_remote_output(self) -> None:
        config = load_config(self.config_path)
        import_run(config, self.run_dir)
        (self.sample_dir / "SAMPLE001.fasta").write_text(">SAMPLE001\nTGCA\n", encoding="utf-8")

        conn = connect(config.database.path)
        try:
            sample = get_sample(conn, "SAMPLE001_fixture_run")
            assert sample is not None
            results = verify_sample_cache(config, conn, sample)
        finally:
            conn.close()

        statuses = {item["output_key"]: item["status"] for item in results}
        self.assertEqual(statuses["main_fasta"], CACHE_STALE)
        self.assertEqual(statuses["iupac_fasta"], CACHE_OK)

    def test_all_run_cache_verification_rows_skip_manual_failed_samples(self) -> None:
        config = load_config(self.config_path)
        import_run(config, self.run_dir)
        import_sample(config, "FAILED001", "LIDFAILED")
        conn = connect(config.database.path)
        try:
            rows = list_samples_for_cache_verification(conn, all_runs=True)
        finally:
            conn.close()

        self.assertEqual([row["sample_run_id"] for row in rows], ["SAMPLE001_fixture_run"])
        self.assertNotIn(MANUAL_FAILED_RUN_NAME, {row["run_name"] for row in rows})

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
        self.assertRegex(export_path.name, r"^LID001-2limsrs-\d{8}T\d{12}\.txt$")
        self.assertEqual(export_path.read_text(encoding="utf-8"), export_text)

    def test_server_lims_export_uses_distinct_timestamped_filenames(self) -> None:
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

        first_path = write_server_lims_export(config, [sample], export_text)
        second_path = write_server_lims_export(config, [sample], export_text)
        self.assertIsNotNone(first_path)
        self.assertIsNotNone(second_path)
        assert first_path is not None
        assert second_path is not None
        self.assertNotEqual(first_path.name, second_path.name)
        self.assertRegex(first_path.name, r"^LID001-2limsrs-\d{8}T\d{12}\.txt$")
        self.assertRegex(second_path.name, r"^LID001-2limsrs-\d{8}T\d{12}\.txt$")
        self.assertNotRegex(second_path.name, r"-2\.txt$")

    def test_single_sample_lims_export_blocks_unreviewed(self) -> None:
        config = load_config(self.config_path)
        import_run(config, self.run_dir)
        app = create_app(self.config_path)
        route = next(
            route for route in app.router.routes if getattr(route, "path", None) == "/samples/{sample_run_id}/lims-export"
        )

        response = route.endpoint(self.make_request(app), "SAMPLE001_fixture_run")

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
        response = route.endpoint(self.make_request(app), "SAMPLE001_fixture_run")

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
        response = route.endpoint(self.make_request(app), "SAMPLE001_fixture_run")

        self.assertEqual(response.status_code, 200)
        self.assertIn('attachment; filename="LID001-2limsrs.txt"', response.headers["content-disposition"])

    def test_bulk_lims_export_blocks_unreviewed(self) -> None:
        config = load_config(self.config_path)
        import_run(config, self.run_dir)
        app = create_app(self.config_path)
        route = next(route for route in app.router.routes if getattr(route, "path", None) == "/samples/lims-export")

        response = asyncio.run(
            route.endpoint(
                self.make_request(app, method="POST"),
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
                self.make_request(app, method="POST"),
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
                self.make_request(app, method="POST"),
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
        response = asyncio.run(route.endpoint(self.make_request(app, method="POST"), sample_run_id=["SAMPLE001_fixture_run"]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.body.decode("utf-8"), ">LID001\nACGT\n")

    def test_bulk_iupac_fasta_clipboard_export_returns_text(self) -> None:
        config = load_config(self.config_path)
        import_run(config, self.run_dir)
        app = create_app(self.config_path)
        route = next(route for route in app.router.routes if getattr(route, "path", None) == "/samples/clipboard/iupac-fasta")
        response = asyncio.run(route.endpoint(self.make_request(app, method="POST"), sample_run_id=["SAMPLE001_fixture_run"]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.body.decode("utf-8"), ">LID001-0.15-iupac\nARYT\n")

    def test_fail_qc_requires_comment(self) -> None:
        config = load_config(self.config_path)
        import_run(config, self.run_dir)
        app = create_app(self.config_path)
        route = next(route for route in app.router.routes if getattr(route, "path", None) == "/samples/qc")

        response = asyncio.run(
            route.endpoint(
                self.make_request(app, method="POST"),
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
                self.make_request(app, method="POST"),
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
                self.make_request(app, method="POST"),
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
                self.make_request(app, method="POST"),
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
                self.make_request(app, method="POST"),
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
                self.make_request(app, method="POST"),
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
                self.make_request(app, method="POST"),
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

        response = asyncio.run(route.endpoint(self.make_request(app, method="POST"), "SAMPLE001_fixture_run", comment_id))
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

        response = asyncio.run(route.endpoint(self.make_request(app, method="POST"), "SAMPLE001_fixture_run"))
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
