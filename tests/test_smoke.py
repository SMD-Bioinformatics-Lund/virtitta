from __future__ import annotations

import asyncio
import json
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from starlette.requests import Request

from virtitta.app import (
    build_igv_url,
    build_lims_export_content,
    cell_style,
    comment_link_label,
    create_app,
    format_value,
)
from virtitta.config import load_config
from virtitta.importer import import_run
from virtitta.repository import add_comment, connect, get_comments, get_sample, list_runs, list_samples, update_qc_status


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
                "[igv]",
                "enabled = true",
                'base_url = "http://localhost:60151/load"',
                "",
                "[features]",
                "comments = true",
                "bulk_qc = true",
                "igv = true",
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
    def setUp(self) -> None:
        self.temp_dir = TemporaryDirectory()
        self.tmp_path = Path(self.temp_dir.name)
        self.root = self.tmp_path / "results_root"
        self.run_dir = self.root / "fixture_run"
        self.sample_dir = self.run_dir / "SAMPLE001" / "results"
        (self.run_dir / "pipeline_info").mkdir(parents=True)
        self.sample_dir.mkdir(parents=True)

        fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        (self.run_dir / "pipeline_info" / "qc_summary.json").write_text(
            json.dumps(fixture),
            encoding="utf-8",
        )

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
                "SELECT sample_run_id, sample_results_relpath FROM samples"
            ).fetchone()
        finally:
            conn.close()

        self.assertEqual(run, ("fixture_run", 1))
        self.assertEqual(sample, ("SAMPLE001_fixture_run", "fixture_run/SAMPLE001/results"))

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
        self.assertEqual(format_value(0.0123, "host_filter_reads_removed_proportion"), "1.2%")
        self.assertEqual(format_value(91.514, "typing_main_blast_identity"), "91.5")
        self.assertEqual(format_value(92.4598, "qc_coverage_pct"), "92.46")
        self.assertEqual(format_value(4.42121, "qc_mean_depth"), "4")

    def test_human_column_style_uses_data_bar_width(self) -> None:
        self.assertEqual(cell_style("host_filter_reads_removed_proportion", 0.0123), "--data-bar-width:1.230%;")
        self.assertEqual(cell_style("qc_mean_depth", 4.0), "")

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
                "LID001\thcvqc\tpassed\t\n"
            ),
        )

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
