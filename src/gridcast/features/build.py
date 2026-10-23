"""Feature builder with as-of enforcement at data_cutoff (C6, BUILD_PLAN §4.5, §4.6, ADR-0004).

One code path serves training, backtests and live inference:

- Demand is read from `raw_demand_history` as VERSIONS (target_time, value, available_at). For an
  issue time the value of an hour is the newest version with `available_at <= data_cutoff`
  (`data_cutoff = issue_time + 30 min`); a version stamped later is invisible.
  * Ontario Demand: the PUB row stamp (modelled D+1 13:00Z for backfill, observed for live and for
    revisions), so the newest Ontario hour is 8-32 h old.
  * Market Demand: plan §3.0 decision 1. The FIRST PUB version of an hour is available at
    `availability.market_demand_available_at` (target + REALTIME_LAG_H), standing in for the
    RealtimeTotals value of the same hour; a PUB revision keeps its own (observed) stamp.
    RealtimeTotals rows keep their observed stamp (modelled rows get the same lag rule).
- Weather is NOT filtered: the caller hands in the frame the forecast may use (training: the
  historical-forecast store, stamped valid_time - 24 h; live: one snapshot with
  available_at = fetched_at <= data_cutoff). Every weather row used is checked instead.
- Every feature row carries `input_available_at`, the max availability over all input rows it
  used. `assert_asof` raises `LeakageError` if any row has `input_available_at > data_cutoff`. It is
  independent of the selection code, so a selection bug (or a weather row from the future) trips
  it instead of silently leaking.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np
import pandas as pd
from sqlalchemy import Engine, text

from gridcast.config import DATA_CUTOFF_OFFSET, REALTIME_LAG_H
from gridcast.features.calendar import CALENDAR_COLUMNS, calendar_features
from gridcast.features.schema import (
    FEATURE_COLUMNS,
    FEATURE_SCHEMA_VERSION,
    ISSUE_COLUMNS,
    META_COLUMNS,
    WEATHER_AGG_COLUMNS,
)
from gridcast.ingest.availability import data_cutoff
from gridcast.timeutil import to_utc
from gridcast.weather import CITIES, VARIABLES, WEATHER_COLUMNS

__all__ = [
    "FEATURE_COLUMNS",
    "FEATURE_SCHEMA_VERSION",
    "DemandHistory",
    "FeatureBuilder",
    "LeakageError",
    "MissingInputError",
    "VersionedSeries",
    "assert_asof",
    "build",
]

H_NS = 3_600_000_000_000
NEG = np.iinfo(np.int64).min  # "no input used"
POS = np.iinfo(np.int64).max  # "no version stored"
CUTOFF_NS = int(DATA_CUTOFF_OFFSET / timedelta(microseconds=1)) * 1000
HORIZONS = tuple(range(1, 25))
L0_SEARCH_H = 6  # live fallback: newest Market hour within issue - k .. issue - k - 5
ONT_SEARCH_H = 72  # newest Ontario hour within issue - 1 .. issue - 72
# History an issue time can reach back to: Ontario lag 336 h at h = 1 and the 168 h Market window.
LOOKBACK = timedelta(hours=400)


class LeakageError(AssertionError):
    """A feature row used an input that was not available at its data cutoff."""


class MissingInputError(ValueError):
    """A required input (the weather at a target hour) is absent."""


def to_ns(values: Iterable[datetime] | pd.Series | pd.DatetimeIndex) -> np.ndarray:
    """Aware timestamps -> int64 ns since the epoch (UTC)."""
    idx = pd.DatetimeIndex(
        values if isinstance(values, pd.Series | pd.DatetimeIndex) else list(values)
    )
    if idx.tz is None:
        raise ValueError("naive timestamps are not allowed")
    naive = idx.tz_convert("UTC").tz_localize(None).as_unit("ns")
    return np.asarray(naive.to_numpy(dtype="datetime64[ns]")).view(np.int64)


def from_ns(values: np.ndarray) -> pd.DatetimeIndex:
    return pd.DatetimeIndex(pd.to_datetime(values, unit="ns", utc=True))


class VersionedSeries:
    """All stored versions of one hourly quantity, with an as-of lookup.

    The first version of each hour sits in dense arrays (fast path); later versions (revisions)
    are applied with a backward as-of merge.
    """

    def __init__(self, target_ns: np.ndarray, value: np.ndarray, avail_ns: np.ndarray) -> None:
        order = np.lexsort((avail_ns, target_ns))
        t = np.asarray(target_ns, dtype=np.int64)[order]
        v = np.asarray(value, dtype=np.float64)[order]
        a = np.asarray(avail_ns, dtype=np.int64)[order]
        first = np.ones(len(t), dtype=bool)
        first[1:] = t[1:] != t[:-1]
        self.t0 = int(t[0]) if len(t) else 0
        n = int((t[-1] - self.t0) // H_NS) + 1 if len(t) else 0
        self.values = np.full(n, np.nan)
        self.avail = np.full(n, POS, dtype=np.int64)
        idx = (t[first] - self.t0) // H_NS
        self.values[idx] = v[first]
        self.avail[idx] = a[first]
        self.revisions = pd.DataFrame(
            {"target": t[~first], "avail": a[~first], "value": v[~first]}
        ).sort_values("avail", kind="stable")
        self.revised_targets = np.unique(t[~first])

    @staticmethod
    def _asof_mask(avail: np.ndarray, cutoffs: np.ndarray) -> np.ndarray:
        """The as-of filter: a version is visible iff available_at <= data_cutoff."""
        return avail <= cutoffs

    def lookup(self, targets: np.ndarray, cutoffs: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Value and availability of each target hour as of each cutoff (same shape, any shape).

        Invisible or absent hours return (NaN, NEG).
        """
        shape = targets.shape
        tg = targets.reshape(-1).astype(np.int64)
        cu = np.broadcast_to(cutoffs, shape).reshape(-1).astype(np.int64)
        vals = np.full(tg.shape, np.nan)
        avs = np.full(tg.shape, POS, dtype=np.int64)
        if len(self.values):
            idx = (tg - self.t0) // H_NS
            ok = (tg >= self.t0) & (idx < len(self.values)) & ((tg - self.t0) % H_NS == 0)
            vals[ok] = self.values[idx[ok]]
            avs[ok] = self.avail[idx[ok]]
        if len(self.revised_targets):
            hit = np.isin(tg, self.revised_targets)
            if hit.any():
                pos = np.flatnonzero(hit)
                left = pd.DataFrame({"target": tg[pos], "cutoff": cu[pos], "pos": pos})
                left = left.sort_values("cutoff", kind="stable")
                merged = pd.merge_asof(
                    left,
                    self.revisions,
                    left_on="cutoff",
                    right_on="avail",
                    by="target",
                    direction="backward",
                )
                m = merged["avail"].notna().to_numpy()
                p = merged["pos"].to_numpy()[m]
                vals[p] = merged["value"].to_numpy()[m]
                avs[p] = merged["avail"].to_numpy()[m].astype(np.int64)
        visible = self._asof_mask(avs, cu)
        vals = np.where(visible, vals, np.nan)
        avs = np.where(visible, avs, NEG)
        return vals.reshape(shape), avs.reshape(shape)


@dataclass(frozen=True)
class DemandHistory:
    ontario: VersionedSeries
    market: VersionedSeries

    @classmethod
    def from_raw(cls, raw: pd.DataFrame, lag_h: int = REALTIME_LAG_H) -> DemandHistory:
        """raw_demand_history rows -> as-of series (availability rules in the module docstring).

        Columns: target_time, source, market_demand_mw, ontario_demand_mw, available_at,
        available_at_kind.
        """
        lag = lag_h * H_NS
        pub = raw[raw["source"] == "pub_demand"].sort_values(["target_time", "available_at"])
        ont = pub[pub["ontario_demand_mw"].notna()]
        ontario = VersionedSeries(
            to_ns(ont["target_time"]),
            ont["ontario_demand_mw"].to_numpy(dtype=np.float64),
            to_ns(ont["available_at"]),
        )
        mp = pub[pub["market_demand_mw"].notna()]
        mp_t = to_ns(mp["target_time"])
        mp_a = to_ns(mp["available_at"])
        first = ~mp["target_time"].duplicated(keep="first").to_numpy()
        mp_a = np.where(first, mp_t + lag, mp_a)  # first version: target + REALTIME_LAG_H
        rt = raw[(raw["source"] == "realtime_totals") & raw["market_demand_mw"].notna()]
        rt_t = to_ns(rt["target_time"])
        rt_a = np.where(
            rt["available_at_kind"].to_numpy() == "observed", to_ns(rt["available_at"]), rt_t + lag
        )
        market = VersionedSeries(
            np.concatenate([mp_t, rt_t]),
            np.concatenate(
                [
                    mp["market_demand_mw"].to_numpy(np.float64),
                    rt["market_demand_mw"].to_numpy(np.float64),
                ]
            ),
            np.concatenate([mp_a, rt_a]),
        )
        return cls(ontario=ontario, market=market)


def _weights() -> np.ndarray:
    w = np.array([c.weight for c in CITIES], dtype=np.float64)
    return np.asarray(w / w.sum(), dtype=np.float64)


def prepare_weather(weather: pd.DataFrame) -> pd.DataFrame:
    """Hourly grid of per-city columns + aggregates + rolling features, with provenance.

    Input columns: valid_time (UTC), available_at (UTC), WEATHER_COLUMNS. Output is indexed by
    valid_time (ns int) on a gap-free hourly grid; `prov` = max available_at over the rows each
    hour's features read (the hour itself and the 23 before it).
    """
    if weather.empty:
        raise MissingInputError("empty weather frame")
    w = weather.sort_values("valid_time").drop_duplicates("valid_time", keep="last")
    t = to_ns(w["valid_time"])
    grid = np.arange(t[0], t[-1] + H_NS, H_NS, dtype=np.int64)
    frame = pd.DataFrame(index=grid)
    vals = w[list(WEATHER_COLUMNS)].astype("float64")
    vals.index = t
    frame = frame.join(vals)
    # Seconds as float64 (exact for whole seconds; ns would lose precision in float64).
    avail = pd.Series((to_ns(w["available_at"]) // 1_000_000_000).astype("float64"), index=t)
    frame["_avail"] = avail.reindex(grid)
    weights = _weights()
    for var in VARIABLES:
        cols = [f"{var}_{c.key}" for c in CITIES]
        frame[f"{var}_w"] = frame[cols].to_numpy() @ weights
    temp_cols = [f"temperature_2m_{c.key}" for c in CITIES]
    frame["temperature_2m_mean"] = frame[temp_cols].mean(axis=1)
    tw = frame["temperature_2m_w"]
    frame["hdd_w"] = np.maximum(0.0, 18.0 - tw)
    frame["cdd_w"] = np.maximum(0.0, tw - 18.0)
    frame["temp_w_mean24"] = tw.rolling(24, min_periods=24).mean()
    frame["temp_w_delta3"] = tw - tw.shift(3)
    frame["prov"] = frame["_avail"].rolling(24, min_periods=1).max()
    return frame


def assert_asof(frame: pd.DataFrame) -> None:
    """Leakage tripwire: every row's inputs were available at its data cutoff."""
    cut = to_ns(frame["issue_time"]) + CUTOFF_NS
    used = to_ns(frame["input_available_at"])
    bad = used > cut
    if bad.any():
        i = int(np.flatnonzero(bad)[0])
        raise LeakageError(
            f"{int(bad.sum())} feature rows used inputs available after data_cutoff; first: "
            f"issue_time={frame['issue_time'].iloc[i]}, input_available_at="
            f"{frame['input_available_at'].iloc[i]}"
        )


def _first_visible(avs: np.ndarray) -> np.ndarray:
    """Column index of the first visible entry per row, -1 if none."""
    vis = avs != NEG
    j = np.argmax(vis, axis=1)
    return np.asarray(np.where(vis.any(axis=1), j, -1), dtype=np.int64)


def _take(m: np.ndarray, j: np.ndarray) -> np.ndarray:
    rows = np.arange(len(j))
    out = m[rows, np.maximum(j, 0)]
    if out.dtype.kind == "f":
        return np.where(j >= 0, out, np.nan)
    return np.where(j >= 0, out, NEG)


def _nanagg(v: np.ndarray, fn: str) -> np.ndarray:
    has = ~np.isnan(v).all(axis=1)
    out = np.full(len(v), np.nan)
    if has.any():
        out[has] = getattr(np, fn)(v[has], axis=1)
    return out


class FeatureBuilder:
    """Builds feature rows for many issue times over one demand history and one weather frame."""

    def __init__(
        self, history: DemandHistory, weather: pd.DataFrame, lag_h: int = REALTIME_LAG_H
    ) -> None:
        self.history = history
        self.weather = prepare_weather(weather)
        self.lag_h = lag_h
        self._calendar = calendar_features(from_ns(self.weather.index.to_numpy()))
        self._calendar.index = self.weather.index

    def issue_frame(self, issue_ns: np.ndarray) -> pd.DataFrame:
        """Issue-level demand features (ISSUE_COLUMNS) + provenance + L0 hour."""
        n = len(issue_ns)
        cut = (issue_ns + CUTOFF_NS)[:, None]
        mk, on = self.history.market, self.history.ontario

        offs = self.lag_h + np.arange(L0_SEARCH_H)
        v, a = mk.lookup(issue_ns[:, None] - offs[None, :] * H_NS, cut)
        j = _first_visible(a)
        l0 = _take(v, j)
        l0_age = np.where(j >= 0, offs[np.maximum(j, 0)], np.nan).astype(np.float64)
        l0_t = np.where(j >= 0, issue_ns - offs[np.maximum(j, 0)] * H_NS, NEG)
        prov = _take(a, j)

        def window(
            series: VersionedSeries, end: np.ndarray, hours: int
        ) -> tuple[np.ndarray, np.ndarray]:
            tg = end[:, None] - np.arange(hours)[None, :] * H_NS
            wv, wa = series.lookup(tg, cut)
            wv[end == NEG] = np.nan
            wa[end == NEG] = NEG
            return wv, wa.max(axis=1)

        w24, p24 = window(mk, l0_t, 24)
        w168, p168 = window(mk, l0_t, 168)
        mean168 = _nanagg(w168, "nanmean")

        oo = np.arange(1, ONT_SEARCH_H + 1)
        ov, oa = on.lookup(issue_ns[:, None] - oo[None, :] * H_NS, cut)
        oj = _first_visible(oa)
        ont_last = _take(ov, oj)
        ont_t = np.where(oj >= 0, issue_ns - oo[np.maximum(oj, 0)] * H_NS, NEG)
        o24, po24 = window(on, ont_t, 24)
        mk_at_ont, pmo = mk.lookup(ont_t, cut[:, 0])
        mk_at_ont[ont_t == NEG] = np.nan
        pmo[ont_t == NEG] = NEG

        out = pd.DataFrame(
            {
                "mkt_l0": l0,
                "mkt_l0_age_h": l0_age,
                "mkt_mean24": _nanagg(w24, "nanmean"),
                "mkt_max24": _nanagg(w24, "nanmax"),
                "mkt_min24": _nanagg(w24, "nanmin"),
                "mkt_mean168": mean168,
                "mkt_l0_ratio168": l0 / mean168,
                "ont_last": ont_last,
                "ont_last_age_h": np.where(oj >= 0, oo[np.maximum(oj, 0)], np.nan).astype(float),
                "ont_mean24": _nanagg(o24, "nanmean"),
                "ont_mkt_ratio": ont_last / mk_at_ont,
            },
            index=pd.RangeIndex(n),
        )
        out["_prov"] = np.max(np.vstack([prov, p24, p168, _take(oa, oj), po24, pmo]), axis=0)
        out["_l0_t"] = l0_t
        return out[[*ISSUE_COLUMNS, "_prov", "_l0_t"]]

    def frame(
        self,
        issue_times: Iterable[datetime] | pd.DatetimeIndex,
        horizons: Iterable[int] = HORIZONS,
        issue: pd.DataFrame | None = None,
    ) -> pd.DataFrame:
        """Long frame: one row per (issue_time, horizon), META_COLUMNS + FEATURE_COLUMNS."""
        issue_ns = to_ns(issue_times)
        if issue is None:
            issue = self.issue_frame(issue_ns)
        cut = issue_ns + CUTOFF_NS
        hist = self.history
        wx = self.weather
        parts = []
        for h in horizons:
            tgt = issue_ns + h * H_NS
            l168, a168 = hist.ontario.lookup(tgt - 168 * H_NS, cut)
            l336, a336 = hist.ontario.lookup(tgt - 336 * H_NS, cut)
            m24, am24 = hist.market.lookup(tgt - 24 * H_NS, cut)
            pos = wx.index.get_indexer(pd.Index(tgt))
            got = np.full(len(tgt), np.nan)
            got[pos >= 0] = wx["_avail"].to_numpy()[pos[pos >= 0]]
            absent = np.isnan(got)  # outside the frame, or a gap inside it
            if absent.any():
                bad = from_ns(tgt[absent])
                raise MissingInputError(f"no weather row for {len(bad)} targets, e.g. {bad[:3]}")
            wrow = wx.iloc[pos]
            wprov = wrow["prov"].to_numpy().astype(np.int64) * 1_000_000_000
            part = pd.DataFrame(index=pd.RangeIndex(len(issue_ns)))
            part["horizon"] = float(h)
            cal = self._calendar.iloc[pos]
            for c in CALENDAR_COLUMNS:
                part[c] = cal[c].to_numpy()
            for c in ISSUE_COLUMNS:
                part[c] = issue[c].to_numpy()
            part["ont_lag168"] = l168
            part["ont_lag336"] = l336
            part["mkt_lag24"] = m24
            for c in (*WEATHER_COLUMNS, *WEATHER_AGG_COLUMNS):
                part[c] = wrow[c].to_numpy()
            prov = np.max(np.vstack([issue["_prov"].to_numpy(), a168, a336, am24, wprov]), axis=0)
            part["issue_time"] = from_ns(issue_ns)
            part["target_time"] = from_ns(tgt)
            part["input_available_at"] = from_ns(prov)
            l0_t = issue["_l0_t"].to_numpy()
            part["l0_target_time"] = pd.Series(from_ns(np.where(l0_t == NEG, 0, l0_t))).where(
                l0_t != NEG
            )
            part["weather_available_at"] = from_ns(wprov)
            parts.append(part)
        out = pd.concat(parts, ignore_index=True)
        out = out[[*META_COLUMNS, *FEATURE_COLUMNS]]
        out[list(FEATURE_COLUMNS)] = out[list(FEATURE_COLUMNS)].astype("float64")
        assert_asof(out)
        return out


# ------------------------------------------------------------------------------- DB loaders


def load_history(
    engine: Engine, start: datetime | None = None, end: datetime | None = None
) -> pd.DataFrame:
    """raw_demand_history rows with target_time in [start, end] (no availability filter: the
    builder applies the per-column as-of rule)."""
    sql = (
        "SELECT target_time, source, market_demand_mw::float8 AS market_demand_mw,"
        " ontario_demand_mw::float8 AS ontario_demand_mw, available_at, available_at_kind"
        " FROM raw_demand_history"
        " WHERE (CAST(:start AS timestamptz) IS NULL OR target_time >= :start)"
        " AND (CAST(:end AS timestamptz) IS NULL OR target_time <= :end)"
    )
    with engine.connect() as conn:
        df = pd.read_sql(text(sql), conn, params={"start": start, "end": end})
    for c in ("target_time", "available_at"):
        df[c] = pd.to_datetime(df[c], utc=True)
    return df


def load_weather(
    engine: Engine, start: datetime | None = None, end: datetime | None = None
) -> pd.DataFrame:
    cols = ", ".join(WEATHER_COLUMNS)
    sql = (
        f"SELECT valid_time, available_at, {cols} FROM weather_hourly"  # noqa: S608 - constant columns
        " WHERE (CAST(:start AS timestamptz) IS NULL OR valid_time >= :start)"
        " AND (CAST(:end AS timestamptz) IS NULL OR valid_time <= :end) ORDER BY valid_time"
    )
    with engine.connect() as conn:
        df = pd.read_sql(text(sql), conn, params={"start": start, "end": end})
    for c in ("valid_time", "available_at"):
        df[c] = pd.to_datetime(df[c], utc=True)
    return df


def build(
    issue_time: datetime,
    engine: Engine,
    weather: pd.DataFrame | None = None,
    lag_h: int = REALTIME_LAG_H,
) -> pd.DataFrame:
    """The 24 feature rows (h = 1..24) for one issue time, as of its data cutoff.

    `weather` is the frame the forecast may use (a live snapshot); default: `weather_hourly`.
    """
    issue = to_utc(issue_time)
    data_cutoff(issue)  # validates "on the hour"
    raw = load_history(engine, issue - LOOKBACK, issue + timedelta(hours=1))
    if weather is None:
        weather = load_weather(engine, issue - timedelta(hours=30), issue + timedelta(hours=24))
    builder = FeatureBuilder(DemandHistory.from_raw(raw, lag_h), weather, lag_h)
    return builder.frame([issue])
