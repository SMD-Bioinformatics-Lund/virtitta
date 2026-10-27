"""The example dev configuration must not share production state."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from virtitta.auth import authenticate_local_user, create_login_session, get_user_from_cookie
from virtitta.config import load_config
from virtitta.outputs import required_sidecars
from virtitta.repository import connect, get_sample, update_qc_status

ROOT = Path(__file__).resolve().parents[1]


class DevelopmentTests(unittest.TestCase):
    def test_example_isolates_state_and_disables_production_destinations(self):
        config = load_config(ROOT / "virtitta.dev.example.toml")
        self.assertEqual(config.app.title, "Virtitta DEV")
        self.assertEqual(config.app.host, "127.0.0.1")
        self.assertEqual(config.app.port, 5812)
        self.assertTrue(config.auth.enabled)
        self.assertEqual(config.auth.cookie_name, "virtitta_dev_session")
        self.assertFalse(config.auth.cookie_secure)
        self.assertTrue(config.webigv.enabled)
        self.assertFalse(config.cluster.enabled)
        self.assertEqual(config.cluster.public_base_url, "")
        self.assertIsNone(config.exports.lims_ingest_root)
        for path in (config.database.path, config.cache.outputs_root,
                     config.cluster.output_root, config.exports.lims_root):
            self.assertTrue(path.is_relative_to(ROOT / "data_dev"), path)
        self.assertEqual(len(config.results_roots), 1)
        self.assertEqual(config.results_roots[0].linux_path, Path("/fs1/results_dev/virpipa/hcv"))

    def test_local_cli_login_import_and_reimport_use_dev_database(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            results = root / "results_dev"
            sample_dir = results / "DEV_RUN" / "SAMPLE001"
            sample_dir.mkdir(parents=True)
            config_path = root / "dev.toml"
            template = (ROOT / "virtitta.dev.example.toml").read_text()
            config_path.write_text(template.replace("/fs1/results_dev/virpipa/hcv", str(results)))
            fixture = ROOT.parent / "virpipa/assets/test_data/qc_summary/qc_summary.json"
            sample = json.loads(fixture.read_text())[0]
            sample["run_name"] = "DEV_RUN"
            sample["sample_run_id"] = "SAMPLE001_DEV_RUN"
            for relname in sample["outputs"].values():
                if not relname:
                    continue
                path = sample_dir / relname
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(">SAMPLE001\nACGT\n" if path.suffix == ".fasta" else "synthetic fixture\n")
            for _, relname in required_sidecars(sample["outputs"]):
                (sample_dir / relname).write_text("synthetic index\n")
            (sample_dir / "SAMPLE001_qc_summary.json").write_text(json.dumps(sample))
            command = [sys.executable, "-m", "virtitta.cli"]
            environment = dict(os.environ, PYTHONPATH=str(ROOT), PYTHONDONTWRITEBYTECODE="1")

            def cli(*args):
                result = subprocess.run(command+list(args), cwd=root, env=environment,
                                        capture_output=True, text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
                return result

            cli("init-db", "--config", str(config_path))
            cli("create-user", "--config", str(config_path), "--username", "devadmin",
                "--role", "admin", "--password", "synthetic-test-password")
            config = load_config(config_path)
            self.assertTrue(config.database.path.is_relative_to(root / "data_dev"))
            self.assertIsNotNone(authenticate_local_user(config, "devadmin", "synthetic-test-password"))
            token, user = create_login_session(config, "devadmin")
            self.assertEqual(get_user_from_cookie(config, token).username, user.username)
            self.assertIn("Imported 1", cli("import-run", "--config", str(config_path),
                                         "--run-dir", str(results / "DEV_RUN")).stdout)
            connection = connect(config.database.path)
            try:
                update_qc_status(connection, ["SAMPLE001_DEV_RUN"], "pass", "devadmin")
                connection.commit()
            finally:
                connection.close()
            cli("import-run", "--config", str(config_path), "--run-dir", str(results / "DEV_RUN"))
            connection = connect(config.database.path)
            try:
                imported = get_sample(connection, "SAMPLE001_DEV_RUN")
            finally:
                connection.close()
            self.assertEqual(imported["qc_status"], "pass")
            self.assertEqual(imported["source_root_name"], "hcv_dev_results")
            self.assertEqual(imported["sample_results_relpath"], "DEV_RUN/SAMPLE001")
            self.assertFalse((root / "data").exists())


if __name__ == "__main__":
    unittest.main()
