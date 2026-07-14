"""Progress indication for the slow AWS/parquet passes.

All progress goes to **stderr** so it never mixes into the recommendation on
stdout (including `--json`). It is disabled automatically when stderr is not a
TTY — pipes, CI, and the test suite stay silent — or when KION_SIZER_NO_PROGRESS
is set. Import stays cheap: tqdm is only touched when progress is actually shown.
"""

from __future__ import annotations

import os
import sys
from contextlib import contextmanager


def _disabled() -> bool:
    if os.environ.get("KION_SIZER_NO_PROGRESS"):
        return True
    try:
        return not sys.stderr.isatty()
    except Exception:  # noqa: BLE001 — a weird stderr just means "no progress"
        return False


def track(iterable, desc: str, total: int | None = None, unit: str = "it"):
    """Yield from `iterable`, drawing a tqdm bar on stderr when enabled.

    When disabled it is a transparent passthrough (no tqdm import, no output),
    so callers can always wrap their loops without branching.
    """
    if _disabled():
        yield from iterable
        return
    from tqdm import tqdm

    if total is None:
        try:
            total = len(iterable)
        except TypeError:
            total = None
    yield from tqdm(
        iterable, desc=desc, total=total, unit=unit, file=sys.stderr, leave=False
    )


@contextmanager
def phase(desc: str):
    """A one-line 'doing X…' / 'X ✓' marker on stderr for an indeterminate step."""
    if _disabled():
        yield
        return
    print(f"  {desc}…", file=sys.stderr, flush=True)
    yield
