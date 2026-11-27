import os
import subprocess
import sys
from pathlib import Path


def test_canonical_imports_do_not_create_log_or_output_state(tmp_path):
    repo_root = Path(__file__).parents[1]
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(repo_root / "src")
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import mailforge.application.sorting, mailforge.application.transform, "
            "mailforge.interfaces.cli",
        ],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout == ""
    assert result.stderr == ""
    assert list(tmp_path.iterdir()) == []
