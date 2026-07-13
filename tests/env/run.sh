#!/usr/bin/env bash
# Build + run the Amazon Linux 2023 environment-fidelity test.
# Usage: bash tests/env/run.sh
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$REPO"
IMG="kion-sizer-envtest"
docker build -f tests/env/Dockerfile.al2023 -t "$IMG" .
docker run --rm "$IMG"
