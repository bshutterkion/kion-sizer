"""progress.track/phase must be transparent no-ops when disabled (tests, pipes)."""

from kion_sizer import progress


def test_track_passthrough_when_disabled(monkeypatch):
    monkeypatch.setenv("KION_SIZER_NO_PROGRESS", "1")
    assert list(progress.track(iter([1, 2, 3]), "x")) == [1, 2, 3]


def test_track_passthrough_no_total(monkeypatch):
    monkeypatch.setenv("KION_SIZER_NO_PROGRESS", "1")

    def gen():
        yield from range(5)

    assert list(progress.track(gen(), "x", unit="file")) == [0, 1, 2, 3, 4]


def test_phase_is_a_noop_context(monkeypatch):
    monkeypatch.setenv("KION_SIZER_NO_PROGRESS", "1")
    with progress.phase("doing thing"):
        pass  # must not raise
