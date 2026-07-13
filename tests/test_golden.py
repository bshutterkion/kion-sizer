"""Self-contained golden tests.

The internal repo diffs Python output against the Go binary byte-for-byte; this
public repo has no Go binary, so instead we freeze the tool's own output for a
set of deterministic fixtures under tests/golden/. Any change to rendering or
the calibration constants must be reflected there on purpose.

Regenerate after an intentional change:
    KION_SIZER_UPDATE_GOLDEN=1 uv run --extra dev pytest tests/test_golden.py
and eyeball the diff (the committed .out files are human-readable).
"""

import gzip
import io
import os
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from kion_sizer import cli

GOLDEN = Path(__file__).parent / "golden"


def _run(args):
    out = io.StringIO()
    code = cli.run(args, out)
    return out.getvalue(), code


def _sparse_parquet(path, size):
    with open(path, "wb") as f:
        f.truncate(size)


def _check(name, args, output):
    GOLDEN.mkdir(exist_ok=True)
    path = GOLDEN / f"{name}.out"
    if os.environ.get("KION_SIZER_UPDATE_GOLDEN") == "1":
        path.write_text(output)
        return
    assert path.exists(), (
        f"missing golden {path}; regenerate with KION_SIZER_UPDATE_GOLDEN=1"
    )
    assert output == path.read_text(), f"output drift for {name}"


# --- byte-path fixtures (sparse parquet sized by truncation) ----------------


@pytest.mark.parametrize(
    "name,extra",
    [
        ("bytes_accounts40", ["--accounts", "40"]),
        ("bytes_accounts40_json", ["--accounts", "40", "--json"]),
        ("bytes_json", ["--json"]),
        ("bytes_accounts5000_json", ["--accounts", "5000", "--json"]),
    ],
)
def test_golden_bytes_path(tmp_path, name, extra):
    _sparse_parquet(tmp_path / "x.parquet", 1 << 30)
    output, code = _run(["--dir", str(tmp_path), *extra])
    assert code == 0
    _check(name, extra, output)


def test_golden_exceeds_tiers(tmp_path):
    # ~220 GiB of parquet drives required RAM past the largest tier.
    _sparse_parquet(tmp_path / "big.parquet", 220 * (1 << 30))
    output, code = _run(["--dir", str(tmp_path), "--accounts", "700", "--json"])
    assert code == 0
    assert '"rds_exceeds_tiers": true' in output
    _check("exceeds_tiers_json", None, output)


def test_golden_read_footers(tmp_path):
    pq.write_table(pa.table({"a": list(range(1000))}), str(tmp_path / "a.parquet"))
    pq.write_table(pa.table({"a": list(range(500))}), str(tmp_path / "b.parquet"))
    output, code = _run(
        ["--dir", str(tmp_path), "--accounts", "100", "--read-footers", "--json"]
    )
    assert code == 0
    _check("read_footers_json", None, output)


def test_golden_csv_sampling(tmp_path):
    (tmp_path / "a.csv").write_bytes(b"h1,h2\n" + b"x,y\n" * 5000)
    with gzip.open(tmp_path / "b.csv.gz", "wb") as w:
        w.write(b"h1,h2\n" + b"x,y\n" * 9000)
    output, code = _run(["--dir", str(tmp_path), "--accounts", "60", "--json"])
    assert code == 0
    _check("csv_sampling_json", None, output)


def test_golden_no_source_error():
    output, code = _run([])
    assert code == 2
    _check("no_source_error", None, output)


# --- output carries only the neutral calibration marker, no caveat ----------
# Positive check (deliberately embeds no sensitive strings): the calibration
# provenance shipped to users is the neutral marker, and there is no caveat
# block. Any reintroduced customer name or caveat text would also break the
# committed, human-readable golden files above.

_NEUTRAL_CALIBRATION = "reference-cur-2026-03 / aws-parquet"


def test_output_is_neutral(tmp_path):
    import json

    _sparse_parquet(tmp_path / "x.parquet", 1 << 30)
    text, _ = _run(["--dir", str(tmp_path), "--accounts", "40"])
    js, _ = _run(["--dir", str(tmp_path), "--accounts", "40", "--json"])
    obj = json.loads(js)
    assert obj["calibration_version"] == _NEUTRAL_CALIBRATION
    assert "caveat" not in obj
    assert f"calibration: {_NEUTRAL_CALIBRATION}" in text
