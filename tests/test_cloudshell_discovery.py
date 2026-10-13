"""Runs the hermetic Cloud Shell discovery tests under pytest, so `uv run
pytest` covers the Python logic and the shell discovery glue for all three
clouds. Each bash script puts fake CLIs (aws / az / gcloud + bq) on PATH; no
real cloud is used.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

_HERE = Path(__file__).parent


@pytest.mark.skipif(shutil.which("bash") is None, reason="bash not available")
@pytest.mark.parametrize(
    "script",
    ["test_cloudshell.sh", "test_azure_cloudshell.sh", "test_gcp_cloudshell.sh"],
)
def test_cloudshell_discovery(script):
    res = subprocess.run(["bash", str(_HERE / script)], capture_output=True, text=True)
    assert res.returncode == 0, res.stdout + res.stderr
