"""Source versions must describe the loaded package, never the shell's cwd."""
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import virtitta

ROOT = Path(__file__).resolve().parents[1]


class RuntimeVersionTests(unittest.TestCase):
    def setUp(self):
        environment = patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop("VIRTITTA_VERSION", None)

    def test_explicit_build_version_wins_without_running_git(self):
        os.environ["VIRTITTA_VERSION"] = " v0.9.0-docker "
        with patch("virtitta.subprocess.run") as run:
            self.assertEqual(virtitta.runtime_version(), "v0.9.0-docker")
            run.assert_not_called()

    def test_clean_dirty_and_worktree_versions_use_package_repository(self):
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            for marker in ("directory", "file"):
                git = repository / ".git"
                git.mkdir() if marker == "directory" else git.write_text("gitdir: unused-test-path\n")
                try:
                    for description in ("v0.9.0-2-gabcdef", "v0.9.0-2-gabcdef-dirty"):
                        with self.subTest(marker=marker, description=description), \
                             patch("virtitta.__file__", str(repository / "virtitta/__init__.py")), \
                             patch("virtitta.subprocess.run", return_value=subprocess.CompletedProcess([], 0, description+"\n")) as run:
                            os.environ["VIRTITTA_VERSION"] = "   "
                            self.assertEqual(virtitta.runtime_version(), description)
                            self.assertEqual(run.call_args.args[0], [
                                "git", "-C", str(repository), "describe", "--tags", "--always", "--dirty"])
                            self.assertEqual(run.call_args.kwargs["timeout"], 2)
                finally:
                    git.rmdir() if marker == "directory" else git.unlink()

    def test_installed_package_does_not_search_enclosing_repository(self):
        with tempfile.TemporaryDirectory() as temporary:
            parent = Path(temporary)
            (parent / ".git").mkdir()
            with patch("virtitta.__file__", str(parent / "site-packages/virtitta/__init__.py")), \
                 patch("virtitta.subprocess.run") as run:
                self.assertEqual(virtitta.runtime_version(), virtitta.__version__)
                run.assert_not_called()

    def test_missing_git_failure_timeout_and_empty_output_fall_back(self):
        failures = [FileNotFoundError("git"), subprocess.CalledProcessError(128, "git"),
                    subprocess.TimeoutExpired("git", 2)]
        with tempfile.TemporaryDirectory() as temporary:
            repository = Path(temporary)
            (repository / ".git").mkdir()
            with patch("virtitta.__file__", str(repository / "virtitta/__init__.py")):
                for error in failures:
                    with self.subTest(error=error), patch("virtitta.subprocess.run", side_effect=error):
                        self.assertEqual(virtitta.runtime_version(), virtitta.__version__)
                with patch("virtitta.subprocess.run", return_value=subprocess.CompletedProcess([], 0, "")):
                    self.assertEqual(virtitta.runtime_version(), virtitta.__version__)

    def test_startup_uses_checkout_from_unrelated_working_directory(self):
        expected = subprocess.run(["git", "-C", str(ROOT), "describe", "--tags", "--always", "--dirty"],
                                  check=True, capture_output=True, text=True).stdout.strip()
        environment = dict(os.environ, PYTHONPATH=str(ROOT), PYTHONDONTWRITEBYTECODE="1")
        with tempfile.TemporaryDirectory() as temporary:
            for override in (None, "docker-test"):
                if override is None:
                    environment.pop("VIRTITTA_VERSION", None)
                    version = expected
                else:
                    environment["VIRTITTA_VERSION"] = override
                    version = override
                result = subprocess.run(
                    [sys.executable, "-c", "from virtitta.app import templates; print(templates.env.globals['app_version'])"],
                    cwd=temporary, env=environment, check=True, capture_output=True, text=True, timeout=15)
                self.assertEqual(result.stdout.strip(), version if version.startswith("v") else "v"+version)


if __name__ == "__main__":
    unittest.main()
