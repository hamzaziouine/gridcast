"""C15: chart_7d.svg data rules (fixed 24 h horizon, best baseline, scored-hours MAPE) and SVG."""

from __future__ import annotations

import xml.etree.ElementTree as ET  # noqa: S405 - parsing our own output
from datetime import UTC, datetime, timedelta

import numpy as np
import pandas as pd
import pytest

from gridcast.status import chart

NOW = datetime(2026, 11, 3, 14, 10, tzinfo=UTC)
H0 = NOW.replace(minute=0)


def _rows(issues: pd.DatetimeIndex, bias: dict[str, float]) -> pd.DataFrame:
    out = []
    for name, b in bias.items():
        for i in issues:
            t = i + pd.Timedelta(hours=chart.HORIZON)
            p50 = 15000.0 + b
            band = name == "champion"
            out.append({"target_time": t, "model_name": name, "model_version": f"{name}-v",
                        "p10": p50 - 500 if band else np.nan, "p50": p50,
                        "p90": p50 + 500 if band else np.nan})  # fmt: skip
    return pd.DataFrame(out)


def _data() -> chart.ChartData:
    issues = pd.date_range(H0 - timedelta(days=3), H0, freq="h")
    bias = {"champion": 150.0, "ieso_adequacy": 300.0, "seasonal_naive_168": 900.0,
            "seasonal_naive_24": 600.0, "persistence": 2000.0}  # fmt: skip
    idx = pd.date_range(H0 - timedelta(days=8), H0 - timedelta(hours=30), freq="h")
    actuals = pd.Series(15000.0, index=idx)
    return chart.build(_rows(issues, bias), actuals, NOW)


def test_window_horizon_and_best_baseline() -> None:
    d = _data()
    assert d.start == datetime(2026, 10, 27, 14, tzinfo=UTC)  # floor(now) - 7 days
    assert d.end == datetime(2026, 11, 4, 14, tzinfo=UTC)  # newest issue + 24 h
    assert d.baseline == "seasonal_naive_24"  # lowest MAPE of the three on the scored hours
    # scored = hours with a champion forecast AND a final actual
    assert d.n_scored == 19  # 11-01 14:00 .. 11-02 08:00
    assert d.mape["champion"] == pytest.approx(1.0)  # |15150 - 15000| / 15000
    assert d.mape["ieso_adequacy"] == pytest.approx(2.0)
    assert d.mape["seasonal_naive_24"] == pytest.approx(4.0)
    assert d.model_version == "champion-v"
    assert d.frame["actual"].notna().sum() == len(
        pd.date_range(d.start, H0 - timedelta(hours=30), freq="h")
    )


def test_no_forecasts_falls_back_to_the_headline_baseline() -> None:
    empty = pd.DataFrame(
        columns=["target_time", "model_name", "model_version", "p10", "p50", "p90"]
    )
    d = chart.build(empty, pd.Series(dtype=float), NOW)
    assert d.baseline == chart.DEFAULT_BASELINE and d.n_scored == 0
    svg = chart.render_svg(d)
    ET.fromstring(svg)  # noqa: S314 - well-formed
    assert "No forecasts in this window yet" in svg
    assert "No day-ahead forecast has a final actual yet" in svg


def test_svg_is_well_formed_themed_and_attributed() -> None:
    svg = chart.render_svg(_data(), repo_url="https://github.com/x/y")
    root = ET.fromstring(svg)  # noqa: S314
    assert root.tag.endswith("svg") and root.get("viewBox") == f"0 0 {chart.W} {chart.H}"
    assert "prefers-color-scheme:dark" in svg and ".bg{fill:#ffffff" in svg  # opaque, themed
    assert "IESO" in svg and "Open-Meteo, CC BY 4.0" in svg and "github.com/x/y" in svg
    assert "MAPE on the 19 hours with a final actual: GridCast 1.00 %" in svg
    assert "same hour yesterday 4.00 %" in svg
    assert "EST (UTC−5" in svg  # honest time axis
    assert svg.count('class="band"') >= 2  # legend swatch + at least one band polygon
    assert len(svg.encode()) < 40_000


def test_gaps_break_the_lines() -> None:
    d = _data()
    d.frame.loc[d.frame.index[-10], "champion"] = np.nan
    svg = chart.render_svg(d)
    champ = next(e for e in ET.fromstring(svg) if e.get("class") == "champ" and e.get("d"))  # noqa: S314
    assert champ.get("d", "").count("M") == 2
