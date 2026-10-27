import os
import subprocess
from pathlib import Path

__all__ = ["__version__", "runtime_version"]

__version__ = "0.1.0"


def runtime_version() -> str:
    """Prefer an explicit build version, then this checkout's Git description."""
    override = os.environ.get("VIRTITTA_VERSION", "").strip()
    if override:
        return override
    repository = Path(__file__).resolve().parent.parent
    # A worktree has a .git file, while an ordinary clone has a .git directory.
    # Do not accidentally describe an enclosing, unrelated repository.
    if (repository / ".git").exists():
        try:
            result = subprocess.run(
                ["git", "-C", str(repository), "describe", "--tags", "--always", "--dirty"],
                check=True, capture_output=True, text=True, timeout=2,
            )
            return result.stdout.strip() or __version__
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            pass
    return __version__

