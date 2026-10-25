"""`chart_7d.svg`: 7 days of day-ahead forecasts against the final actuals (BUILD_PLAN §1.2, C15).

What is drawn, per target hour, over [now - 7 days, newest champion issue + 24 h]:

- **Actual**: final IESO Ontario Demand (`actuals`; scoring uses the same values).
- **GridCast**: the champion's p50 and its 80 % band (p10..p90) from the forecast issued
  `HORIZON` (24) hours before the hour. One fixed horizon for every series, so no line gets the
  benefit of a fresher issue; the last 24 hours to the right of "now" are forecasts not yet
  scored.
- **IESO**: the `ieso_adequacy` row of the same issue (its latest Adequacy3 snapshot at or before
  that issue's data cutoff).
- **Best baseline**: of seasonal_naive_168 / seasonal_naive_24 / persistence, the one with the
  lowest MAPE on the drawn hours that have a final actual and a champion forecast;
  seasonal_naive_168 (the plan's headline baseline) when nothing is scored yet.

The subtitle states the MAPE of each series over the same scored hours and how many there are.
Plain SVG, no fonts or scripts; colours come from a <style> block with a prefers-color-scheme
media query, on a transparent background, so it reads on light and dark pages.
"""

from __future__ import annotations

import html
import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import Engine, text

from gridcast.timeutil import EST, floor_hour

HORIZON = 24
WINDOW = timedelta(days=7)
BASELINES = ("seasonal_naive_168", "seasonal_naive_24", "persistence")
DEFAULT_BASELINE = "seasonal_naive_168"
LABELS = {
    "seasonal_naive_168": "same hour last week",
    "seasonal_naive_24": "same hour yesterday",
    "persistence": "persistence",
}
ATTRIBUTION = "Data: IESO, Terms of Use · Weather: Open-Meteo, CC BY 4.0"

W, H = 900, 380
LEFT, RIGHT, TOP, BOTTOM = 64, 20, 92, 58

# The background is opaque and follows the same media query as the text: the query reads the
# viewer's OS setting, not the page theme, so a transparent chart would put light text on a white
# page for an OS-dark / GitHub-light viewer.
STYLE = """
.bg{fill:#ffffff;stroke:#d1d9e0}
text{font-family:-apple-system,"Segoe UI",Helvetica,Arial,sans-serif;fill:#1f2328}
.t{font-size:15px;font-weight:600}.s{font-size:11.5px;fill:#59636e}.ax{font-size:11px;fill:#59636e}
.ft{font-size:10.5px;fill:#59636e}.lg{font-size:11.5px}
.grid{stroke:#d1d9e0;stroke-width:1}.now{stroke:#59636e;stroke-width:1;stroke-dasharray:3 3}
.fut{fill:#59636e;fill-opacity:.06}
.band{fill:#0969da;fill-opacity:.16;stroke:none}
.actual{stroke:#1f2328;stroke-width:2;fill:none}
.champ{stroke:#0969da;stroke-width:2;fill:none}
.ieso{stroke:#bc4c00;stroke-width:1.5;fill:none;stroke-dasharray:6 3}
.base{stroke:#8250df;stroke-width:1.3;fill:none;stroke-dasharray:2 3}
@media (prefers-color-scheme:dark){
.bg{fill:#0d1117;stroke:#3d444d}text{fill:#e6edf3}.s,.ax,.ft{fill:#9198a1}.grid{stroke:#3d444d}.now{stroke:#9198a1}
.fut{fill:#9198a1;fill-opacity:.08}.band{fill:#4493f8;fill-opacity:.22}
.actual{stroke:#e6edf3}.champ{stroke:#4493f8}.ieso{stroke:#f0883e}.base{stroke:#ab7df8}}
"""


@dataclass
class ChartData:
    """Hourly frame indexed by target_time (UTC) with columns actual, champion, p10, p90, ieso,
    baseline (NaN where missing), plus what the labels need."""

    frame: pd.DataFrame
    start: datetime
    end: datetime
    now: datetime
    baseline: str
    model_version: str | None
    mape: dict[str, float | None] = field(default_factory=dict)
    n_scored: int = 0
    first_forecast: datetime | None = None


def _mape(pred: pd.Series, actual: pd.Series) -> float | None:
    m = pred.notna() & actual.notna()
    if not m.any():
        return None
    return float((np.abs(pred[m] - actual[m]) / actual[m]).mean() * 100)


def build(rows: pd.DataFrame, actuals: pd.Series, now: datetime) -> ChartData:
    """`rows`: v_predictions rows at HORIZON (target_time, model_name, model_version, p10, p50,
    p90); `actuals`: final actuals by target_time. Pure: no I/O."""
    rows = rows.copy()
    rows["target_time"] = pd.to_datetime(rows["target_time"], utc=True)
    champ = rows[rows["model_name"] == "champion"]
    end = (
        champ["target_time"].max().to_pydatetime()
        if len(champ)
        else floor_hour(now) + timedelta(hours=HORIZON)
    )
    start = floor_hour(now) - WINDOW
    idx = pd.date_range(start, end, freq="h")
    frame = pd.DataFrame(index=idx)
    act = actuals.copy()
    act.index = pd.DatetimeIndex(pd.to_datetime(act.index, utc=True))
    frame["actual"] = act.reindex(idx).astype(float)

    def col(name: str, value: str = "p50") -> pd.Series:
        g = rows[rows["model_name"] == name].set_index("target_time")[value]
        return g[~g.index.duplicated()].reindex(idx).astype(float)

    frame["champion"], frame["p10"], frame["p90"] = (
        col("champion"),
        col("champion", "p10"),
        col("champion", "p90"),
    )
    frame["ieso"] = col("ieso_adequacy")
    scored = frame["champion"].notna() & frame["actual"].notna()
    best, best_mape = DEFAULT_BASELINE, math.inf
    for name in BASELINES:
        m = _mape(col(name)[scored], frame.loc[scored, "actual"])
        if m is not None and m < best_mape:
            best, best_mape = name, m
    frame["baseline"] = col(best)
    s = frame[scored]
    mape = {
        "champion": _mape(s["champion"], s["actual"]),
        "ieso_adequacy": _mape(s["ieso"], s["actual"]),
        best: _mape(s["baseline"], s["actual"]),
    }
    versions = champ["model_version"].dropna()
    first = champ["target_time"].min().to_pydatetime() if len(champ) else None
    return ChartData(
        frame=frame,
        start=start,
        end=end,
        now=now,
        baseline=best,
        model_version=str(versions.iloc[-1]) if len(versions) else None,
        mape=mape,
        n_scored=int(scored.sum()),
        first_forecast=first,
    )


def load(engine: Engine, now: datetime) -> ChartData:
    start = floor_hour(now) - WINDOW
    with engine.connect() as conn:
        rows = pd.read_sql(
            text(
                "SELECT target_time, model_name, model_version, p10, p50, p90 FROM v_predictions"
                " WHERE horizon = :h AND issue_time >= :lo AND model_name = ANY(:m)"
                " ORDER BY target_time"
            ),
            conn,
            params={
                "h": HORIZON,
                "lo": start - timedelta(hours=HORIZON),
                "m": ["champion", "ieso_adequacy", *BASELINES],
            },
        )
        act = pd.read_sql(
            text(
                "SELECT target_time, ontario_demand_mw::float8 AS v FROM actuals"
                " WHERE target_time >= :lo ORDER BY 1"
            ),
            conn,
            params={"lo": start},
        )
    actuals = pd.Series(
        act["v"].to_numpy(np.float64),
        index=pd.DatetimeIndex(pd.to_datetime(act["target_time"], utc=True)),
    )
    return build(rows, actuals, now)


# ------------------------------------------------------------------------------ SVG


def _nice_step(span: float) -> float:
    raw = span / 5
    mag = 10 ** math.floor(math.log10(raw))
    return float(next(m * mag for m in (1, 2, 2.5, 5, 10) if m * mag >= raw))


def _fmt_pct(x: float | None) -> str:
    return "n/a" if x is None else f"{x:.2f} %"


def render_svg(d: ChartData, repo_url: str = "", generated_at: datetime | None = None) -> str:
    f = d.frame
    t0, t1 = pd.Timestamp(d.start), pd.Timestamp(d.end)
    span_s = max((t1 - t0).total_seconds(), 3600.0)
    pw, ph = W - LEFT - RIGHT, H - TOP - BOTTOM
    vals = f[["actual", "champion", "p10", "p90", "ieso", "baseline"]].to_numpy(np.float64)
    finite = vals[np.isfinite(vals)]
    lo, hi = (float(finite.min()), float(finite.max())) if finite.size else (10000.0, 20000.0)
    if hi - lo < 1000:
        lo, hi = lo - 500, hi + 500
    step = _nice_step(hi - lo)
    y0, y1 = math.floor(lo / step) * step, math.ceil(hi / step) * step

    def x(t: pd.Timestamp) -> float:
        return LEFT + (t - t0).total_seconds() / span_s * pw

    def y(v: float) -> float:
        return TOP + (y1 - v) / (y1 - y0) * ph

    def path(col: str) -> str:
        parts: list[str] = []
        prev: pd.Timestamp | None = None
        idx = pd.DatetimeIndex(f.index)
        for ts, v in zip(idx, f[col].to_numpy(np.float64), strict=True):
            if not np.isfinite(v):
                prev = None
                continue
            cmd = "L" if prev is not None and ts - prev <= pd.Timedelta(hours=1) else "M"
            parts.append(f"{cmd}{x(ts):.1f},{y(float(v)):.1f}")
            prev = ts
        return "".join(parts)

    def band() -> list[str]:
        out = []
        ok = f["p10"].notna() & f["p90"].notna()
        run: list[pd.Timestamp] = []
        for t in [*f.index[ok], None]:
            ts = None if t is None else pd.Timestamp(t)
            if ts is not None and (not run or ts - run[-1] <= pd.Timedelta(hours=1)):
                run.append(ts)
                continue
            if len(run) > 1:
                hi_ = f.loc[run, "p90"].to_numpy(np.float64)
                lo_ = f.loc[run, "p10"].to_numpy(np.float64)
                up = " ".join(f"{x(r):.1f},{y(v):.1f}" for r, v in zip(run, hi_, strict=True))
                dn = " ".join(
                    f"{x(r):.1f},{y(v):.1f}" for r, v in zip(run[::-1], lo_[::-1], strict=True)
                )
                out.append(f'<polygon class="band" points="{up} {dn}"/>')
            run = [] if ts is None else [ts]
        return out

    e = html.escape
    base_label = LABELS[d.baseline]
    el: list[str] = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}"'
        f' role="img" aria-labelledby="ttl dsc">',
        '<title id="ttl">GridCast: day-ahead forecasts vs final actuals, last 7 days</title>',
        f'<desc id="dsc">Ontario Demand in MW, hourly, from {d.start:%Y-%m-%d %H:%MZ} to'
        f" {d.end:%Y-%m-%d %H:%MZ}. GridCast p50 with 80% band, IESO, and the {e(base_label)}"
        " baseline, each issued 24 hours ahead, against final IESO actuals.</desc>",
        f"<style>{STYLE}</style>",
        f'<rect class="bg" width="{W}" height="{H}"/>',
        '<text class="t" x="16" y="24">Ontario Demand: GridCast day-ahead forecast vs actual'
        " (last 7 days)</text>",
    ]
    since = (
        f" · live since {d.first_forecast.astimezone(EST):%Y-%m-%d}"
        if d.first_forecast is not None and d.first_forecast > d.start + timedelta(hours=12)
        else ""
    )
    scored = (
        f"MAPE on the {d.n_scored} hours with a final actual: GridCast"
        f" {_fmt_pct(d.mape.get('champion'))} · IESO {_fmt_pct(d.mape.get('ieso_adequacy'))} ·"
        f" {e(base_label)} {_fmt_pct(d.mape.get(d.baseline))}"
        if d.n_scored
        else "No day-ahead forecast has a final actual yet (actuals arrive the next morning)."
    )
    el.append(
        f'<text class="s" x="16" y="42">Every line is the forecast issued 24 h before the hour'
        f"{since}. {scored}</text>"
    )
    # legend
    lx, ly = 16, 66
    for cls, label in (
        ("actual", "Actual (IESO, final)"),
        ("champ", "GridCast p50"),
        ("band", "GridCast 80 % interval"),
        ("ieso", "IESO forecast"),
        ("base", f"Best baseline: {base_label}"),
    ):
        if cls == "band":
            el.append(f'<rect class="band" x="{lx}" y="{ly - 9}" width="22" height="10"/>')
        else:
            el.append(f'<line class="{cls}" x1="{lx}" y1="{ly - 4}" x2="{lx + 22}" y2="{ly - 4}"/>')
        el.append(f'<text class="lg" x="{lx + 28}" y="{ly}">{e(label)}</text>')
        lx += 34 + int(len(label) * 6.4)
    # grid + y axis
    v = y0
    while v <= y1 + 1e-9:
        yy = y(v)
        el.append(f'<line class="grid" x1="{LEFT}" y1="{yy:.1f}" x2="{W - RIGHT}" y2="{yy:.1f}"/>')
        el.append(
            f'<text class="ax" x="{LEFT - 6}" y="{yy + 4:.1f}" text-anchor="end">{v:,.0f}</text>'
        )
        v += step
    el.append(
        f'<text class="ax" x="14" y="{TOP + ph / 2:.0f}" transform="rotate(-90 14'
        f' {TOP + ph / 2:.0f})" text-anchor="middle">MW</text>'
    )
    # shading: hours after the newest final actual are not scored yet; then the "now" line
    have = f.index[f["actual"].notna()]
    unscored = pd.Timestamp(have.max()) + pd.Timedelta(hours=1) if len(have) else t0
    if unscored < t1:
        xs = x(max(unscored, t0))
        el.append(
            f'<rect class="fut" x="{xs:.1f}" y="{TOP}" width="{W - RIGHT - xs:.1f}" height="{ph}"/>'
        )
        el.append(f'<text class="ax" x="{xs + 4:.1f}" y="{TOP + 12}">not scored yet</text>')
    now = pd.Timestamp(d.now)
    if t0 < now < t1:
        xn = x(now)
        el.append(f'<line class="now" x1="{xn:.1f}" y1="{TOP}" x2="{xn:.1f}" y2="{TOP + ph}"/>')
        el.append(f'<text class="ax" x="{xn + 4:.1f}" y="{TOP + 26}">now</text>')
    # x axis: midnight EST of each day
    day = pd.Timestamp(d.start).tz_convert(EST).normalize() + pd.Timedelta(days=1)
    while day <= t1:
        xx = x(day)
        el.append(
            f'<line class="grid" x1="{xx:.1f}" y1="{TOP + ph}" x2="{xx:.1f}" y2="{TOP + ph + 4}"/>'
        )
        el.append(
            f'<text class="ax" x="{xx:.1f}" y="{TOP + ph + 17}" text-anchor="middle">'
            f"{day:%b %d}</text>"
        )
        day += pd.Timedelta(days=1)
    el.append(
        f'<text class="ax" x="{W - RIGHT}" y="{TOP + ph + 32}" text-anchor="end">'
        "hour start, EST (UTC−5, IESO market time)</text>"
    )
    el += band()
    for cls, col in (
        ("base", "baseline"),
        ("ieso", "ieso"),
        ("actual", "actual"),
        ("champ", "champion"),
    ):
        p = path(col)
        if p:
            el.append(f'<path class="{cls}" d="{p}"/>')
    if not np.isfinite(f["champion"].to_numpy(np.float64)).any():
        el.append(
            f'<text class="s" x="{LEFT + pw / 2:.0f}" y="{TOP + ph / 2:.0f}"'
            ' text-anchor="middle">No forecasts in this window yet</text>'
        )
    gen = generated_at or d.now
    model = f"model {e(d.model_version)} · " if d.model_version else ""
    repo = f" · {e(repo_url)}" if repo_url else ""
    el.append(
        f'<text class="ft" x="16" y="{H - 10}">{e(ATTRIBUTION)} · {model}generated'
        f" {gen:%Y-%m-%d %H:%M}Z{repo}</text>"
    )
    el.append("</svg>")
    return "\n".join(el) + "\n"


def summary(d: ChartData) -> dict[str, Any]:
    """The chart's numbers, for metrics.json."""

    def iso(t: datetime | None) -> str | None:
        return (
            None if t is None else pd.Timestamp(t).tz_convert("UTC").strftime("%Y-%m-%dT%H:%M:%SZ")
        )

    return {
        "window_start": iso(d.start),
        "window_end": iso(d.end),
        "horizon_h": HORIZON,
        "best_baseline": d.baseline,
        "n_scored_hours": d.n_scored,
        "mape_pct": {k: (None if v is None else round(v, 3)) for k, v in d.mape.items()},
        "first_forecast_target": iso(d.first_forecast),
        "model_version": d.model_version,
    }
