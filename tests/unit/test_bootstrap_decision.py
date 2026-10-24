"""ADR-0010 bootstrap rule (BUILD_PLAN §4.7): target, hard floor, fallback."""

from __future__ import annotations

import pytest

from gridcast.jobs.train import decide_bootstrap


@pytest.mark.parametrize(
    ("mape", "sn", "cov", "leak_ok", "fallback", "expected"),
    [
        (3.9, 6.6, 0.80, True, False, ("lgbm", "bootstrap", "ok")),
        (4.1, 6.6, 0.80, True, False, ("lgbm", "bootstrap-below-target", "below_target")),
        (3.9, 5.0, 0.80, True, False, ("lgbm", "bootstrap-below-target", "below_target")),  # 22%
        (3.9, 6.6, 0.86, True, False, ("lgbm", "bootstrap-below-target", "below_target")),
        (3.9, 6.6, 0.91, True, False, (None, None, None)),  # coverage outside the floor
        (6.7, 6.6, 0.80, True, False, (None, None, None)),  # not better than sn168
        (3.9, 6.6, 0.80, False, False, (None, None, None)),  # leakage
        (6.7, 6.6, 0.80, True, True, ("seasonal_naive_168", "bootstrap-fallback", "fallback")),
    ],
)
def test_decide_bootstrap(
    mape: float,
    sn: float,
    cov: float,
    leak_ok: bool,
    fallback: bool,
    expected: tuple[str | None, str | None, str | None],
) -> None:
    d = decide_bootstrap(mape, sn, cov, leak_ok, fallback)
    assert (d.promote, d.reason, d.status) == expected
