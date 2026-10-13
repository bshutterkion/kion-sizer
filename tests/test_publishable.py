"""This repository is published publicly; nothing internal may ship in it.

The tool already had a narrow neutrality check on its rendered output. That was
not enough: a calibration marker naming a real billing account reached the
`calibration` field of every run, so one customer would have seen another
customer's account id in their own sizing output.

These tests widen the guard to the whole tree and to every calibration marker,
so the next person who calibrates a pair cannot leak provenance by accident.
Design docs and anything citing Kion's internal source belong in the internal
twin repository.
"""

import pathlib
import re
import subprocess

import pytest

from kion_sizer import config

ROOT = pathlib.Path(__file__).resolve().parents[1]

# Substrings that must never appear in a published file. Each is either a real
# identifier (an account, a tenant, a storage account) or a pointer into Kion's
# private source or tracker.
FORBIDDEN = {
    "internal source path": (
        r"\busage\.go\b", r"\bpoller\.go\b", r"\bdomain\.Payer\b",
        r"financials-poller/logic", r"azure/shared/", r"GetExportManifest",
        r"GetExportBlobs", r"devops/platform/installer",
    ),
    "internal tracker / host": (
        r"git\.kion\.io", r"portal\.kion\.io", r"kion/devops#",
    ),
    "customer or tenant identifier": (
        r"dsdevenv", r"\b01847D[-_]", r"kionbillingexport",
        r"onmicrosoft\.(com|us)", r"\bKionDnS",
    ),
}

# Files that are allowed to describe the policy itself.
EXEMPT = {"tests/test_publishable.py"}


def _candidate_paths():
    """What would be published: tracked files plus new files git would pick up.

    Asking git means ignored directories (a virtualenv, a CI dependency cache
    full of vendored third-party source) are never scanned. Without git -- a
    CI image or an unpacked tarball -- fall back to walking the tree, skipping
    the directories .gitignore covers.
    """
    try:
        out = subprocess.run(
            ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
            cwd=ROOT,
            capture_output=True,
            check=True,
        ).stdout.decode()
        return sorted(r for r in out.split("\0") if r)
    except (OSError, subprocess.CalledProcessError):
        skip = (".git/", ".venv/", ".uv-cache/", "dist/", "build/")
        return sorted(
            rel
            for rel in (p.relative_to(ROOT).as_posix() for p in ROOT.rglob("*"))
            if not rel.startswith(skip)
        )


def _published_files():
    for rel in _candidate_paths():
        p = ROOT / rel
        if not p.is_file():
            continue
        if rel in EXEMPT:
            continue
        if "__pycache__" in rel or rel.endswith((".pyc", ".lock")):
            continue
        yield rel, p


@pytest.mark.parametrize("category", sorted(FORBIDDEN))
def test_no_internal_references_in_published_files(category):
    hits = []
    for rel, p in _published_files():
        try:
            text = p.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for pattern in FORBIDDEN[category]:
            for m in re.finditer(pattern, text):
                line = text[: m.start()].count("\n") + 1
                hits.append(f"{rel}:{line}: {m.group(0)}")
    assert not hits, (
        f"{category} must not ship in the public repo:\n  " + "\n  ".join(hits)
    )


def test_design_docs_are_not_published():
    # They cite internal repositories and unfixed installer findings.
    assert not (ROOT / "docs" / "superpowers").exists(), (
        "docs/superpowers belongs in the internal repo, not the public one"
    )


def test_every_calibration_marker_is_customer_neutral():
    # The marker is printed in the `calibration` field of every run, so a real
    # account id here would be shown to unrelated customers.
    bad = []
    for key, cal in sorted(config.default().sources.items()):
        for patterns in FORBIDDEN.values():
            for pattern in patterns:
                if re.search(pattern, cal.calibration):
                    bad.append(f"sources.{key}: {cal.calibration!r}")
    assert not bad, "calibration markers reach customer output:\n  " + "\n  ".join(bad)


def test_measured_markers_still_carry_provenance():
    # Neutral must not mean uninformative: a measured pair should still say
    # what it was measured against and roughly when.
    for key, cal in config.default().sources.items():
        if cal.inherited:
            continue
        assert re.search(r"20\d\d", cal.calibration), (
            f"sources.{key} is measured but its marker names no date: {cal.calibration!r}"
        )
