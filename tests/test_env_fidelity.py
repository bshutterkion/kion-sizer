"""Environment-fidelity test on amazonlinux:2023 (CloudShell's OS).

Opt-in and slow: builds a container that provisions uv + Python + pyarrow from
scratch and runs the tool end-to-end. Skipped unless RUN_ENV_TEST=1 and Docker
is available. Run directly with: bash tests/env/run.sh
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(
    os.environ.get("RUN_ENV_TEST") != "1",
    reason="set RUN_ENV_TEST=1 to run the AL2023 container fidelity test",
)
@pytest.mark.skipif(shutil.which("docker") is None, reason="docker not available")
def test_al2023_env_fidelity():
    build = subprocess.run(
        ["docker", "build", "-f", "tests/env/Dockerfile.al2023", "-t",
         "kion-sizer-envtest", "."],
        cwd=_REPO, capture_output=True, text=True,
    )
    assert build.returncode == 0, build.stdout + build.stderr
    run = subprocess.run(
        ["docker", "run", "--rm", "kion-sizer-envtest"],
        cwd=_REPO, capture_output=True, text=True,
    )
    assert run.returncode == 0, run.stdout + run.stderr
    assert "ENV FIDELITY PASSED" in run.stdout
