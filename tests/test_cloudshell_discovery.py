"""Runs the hermetic cloudshell.sh discovery tests (tests/test_cloudshell.sh)
under pytest, so `uv run pytest` covers both the Python logic and the shell
discovery glue. The bash script puts a fake `aws` on PATH; no real AWS is used.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).parent / "test_cloudshell.sh"


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
def test_cloudshell_discovery():
    res = subprocess.run(
        ["bash", str(_SCRIPT)], capture_output=True, text=True
    )
    assert res.returncode == 0, res.stdout + res.stderr
