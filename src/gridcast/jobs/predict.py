"""`gridcast predict --issue-time T` (C9): champion, baselines and IESO-as-of rows for one issue.

- Champion: `models/champion.json` -> artifact (sha256 checked) + interval offsets.
- Shadow (C18/C19): when `models/shadow.json` exists, the same features are scored by the shadow
  candidate and written as `model_name='shadow'` (insert-only, like the champion). A shadow
  failure never blocks the champion rows: it is reported in `shadow_error`.
- live_features: the horizon-1 PSI features and the L0 lag of the champion's feature frame, one
  row per issue hour, insert-only (drift diagnostics, DATA_NOTES §19.3); a failure is reported in
  `live_features_error` and never blocks the forecast.
- Baselines: seasonal_naive_168 / _24 / persistence on the same as-of history; p10/p90 from each
  baseline's own relative residuals over the 60 days before the cutoff, using only actuals
  visible at the cutoff.
- ieso_adequacy: per target, the latest Adequacy3 snapshot with created_at <= data_cutoff (plan
  §3.0 decision 3: whatever its age); p10/p90 NULL; the snapshot created_at is the version.
- Weather: `store` = weather_hourly (historical-forecast archive, synthetic stamps); `live` = one
  Open-Meteo snapshot fetched now, usable only if fetched_at <= data_cutoff (in memory here; the
  hourly job of C10 stores snapshots in the bucket).
- Writes: champion rows insert-only (ON CONFLICT DO NOTHING, a published row is never rewritten);
  baseline and IESO rows upsert (deterministic). run_kind defaults to 'manual'.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import Connection, Engine, text

from gridcast import contracts
from gridcast.features.build import (
    CUTOFF_NS,
    LOOKBACK,
    DemandHistory,
    FeatureBuilder,
    load_history,
    load_weather,
    to_ns,
)
from gridcast.ingest.availability import data_cutoff
from gridcast.models.baselines import BASELINES, baseline_frame
from gridcast.models.intervals import IntervalOffsets, fit_offsets
from gridcast.models.lgbm import LgbmForecaster
from gridcast.storage.objstore import ObjectStore
from gridcast.timeutil import floor_hour, to_utc

CHAMPION_KEY = "models/champion.json"
SHADOW_KEY = "models/shadow.json"
BASELINE_WINDOW = timedelta(days=60)
INSERT_ONLY = {"champion", "shadow", "prev_champion"}
_MODEL_CACHE: dict[tuple[str, str], tuple[LgbmForecaster, IntervalOffsets]] = {}

RUN_INSERT = (
    "INSERT INTO prediction_runs (issue_time, model_id, run_id, run_kind)"
    " VALUES (:i, :m, :r, :k) ON CONFLICT (issue_time, model_id) DO "
)
RUN_INSERT_ONLY = RUN_INSERT + "NOTHING"
RUN_UPSERT = (
    RUN_INSERT + "UPDATE SET run_id = EXCLUDED.run_id, run_kind = EXCLUDED.run_kind,"
    " created_at = now()"
)
PRED_INSERT = (
    "INSERT INTO predictions (issue_time, model_id, horizon, version_id, p50, p10, p90)"
    " VALUES (:i, :m, :h, :v, :p50, :p10, :p90) ON CONFLICT (issue_time, model_id, horizon) DO "
)
PRED_INSERT_ONLY = PRED_INSERT + "NOTHING"  # a published champion row is never rewritten
PRED_UPSERT = (
    PRED_INSERT + "UPDATE SET version_id = EXCLUDED.version_id, p50 = EXCLUDED.p50,"
    " p10 = EXCLUDED.p10, p90 = EXCLUDED.p90"
)


def _weather_live(
    fetch_snapshot: Callable[[], tuple[pd.DataFrame, datetime]], cutoff: datetime
) -> pd.DataFrame:
    frame, fetched_at = fetch_snapshot()
    if fetched_at > cutoff:
        raise ValueError(
            f"live snapshot fetched at {fetched_at:%H:%M:%SZ}, after data_cutoff {cutoff:%H:%MZ}:"
            " it cannot be used for this issue time"
        )
    frame = frame.copy()
    frame["available_at"] = pd.Timestamp(fetched_at)
    return frame


def baseline_rows(history: DemandHistory, issue: datetime) -> pd.DataFrame:
    issue_ns = to_ns([issue])
    rows = baseline_frame(history, issue_ns)
    cut = issue_ns[0] + CUTOFF_NS
    past = pd.date_range(issue - BASELINE_WINDOW, issue - timedelta(hours=1), freq="h")
    hist = baseline_frame(history, to_ns(past))
    tgt = to_ns(hist["target_time"])
    actual, _ = history.ontario.lookup(tgt, np.full(len(tgt), cut))  # only actuals visible now
    hist["actual"] = actual
    for name in BASELINES:
        off = fit_offsets(hist[hist["model_name"] == name], 1.0, "trailing 60 d, visible actuals")
        m = rows["model_name"] == name
        p10, p90 = off.apply(rows.loc[m, "p50"].to_numpy(), rows.loc[m, "horizon"].to_numpy())
        rows.loc[m, "p10"], rows.loc[m, "p90"] = p10, p90
    rows["model_version"] = rows["model_name"]
    return rows.drop(columns=["input_available_at"])


def ieso_rows(engine: Engine, issue: datetime) -> pd.DataFrame:
    cutoff = data_cutoff(issue)
    targets = [issue + timedelta(hours=h) for h in range(1, 25)]
    with engine.connect() as conn:
        df = pd.read_sql(
            text(
                "SELECT DISTINCT ON (target_time) target_time, energy_mw::float8 AS p50, created_at"
                " FROM ieso_forecast_snapshots WHERE target_time = ANY(:t) AND created_at <= :c"
                " ORDER BY target_time, created_at DESC"
            ),
            conn,
            params={"t": targets, "c": cutoff},
        )
    df["target_time"] = pd.to_datetime(df["target_time"], utc=True)
    df["issue_time"] = pd.Timestamp(issue)
    df["horizon"] = ((df["target_time"] - df["issue_time"]).dt.total_seconds() // 3600).astype(int)
    df["model_name"] = "ieso_adequacy"
    df["model_version"] = [pd.Timestamp(c).tz_convert("UTC").isoformat() for c in df["created_at"]]
    df["p10"] = np.nan
    df["p90"] = np.nan
    return df.drop(columns=["created_at"])


def champion_rows(
    store: ObjectStore,
    frame: pd.DataFrame,
    history: DemandHistory,
    issue: datetime,
    key: str = CHAMPION_KEY,
    model_name: str = "champion",
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """Rows of the model a pointer (`champion.json`, or `shadow.json` as model_name 'shadow')
    names."""
    champ = store.get_json(key)
    out = frame[["issue_time", "target_time"]].copy()
    out["horizon"] = frame["horizon"].astype(int)
    out["model_name"] = model_name
    out["model_version"] = champ["model_version"]
    if champ["model_name"] == "lgbm":
        cache_key = (champ["sha256"], champ["intervals_key"])
        if cache_key not in _MODEL_CACHE:  # one download per process (catch-up runs several hours)
            blob = store.get_bytes(champ["artifact_key"])
            if hashlib.sha256(blob).hexdigest() != champ["sha256"]:
                raise ValueError("champion artifact sha256 mismatch")
            _MODEL_CACHE[cache_key] = (
                LgbmForecaster.from_bytes(blob),
                IntervalOffsets.from_json(store.get_bytes(champ["intervals_key"])),
            )
        model, offsets = _MODEL_CACHE[cache_key]
        out["p50"] = model.predict(frame)
        out["p10"], out["p90"] = offsets.apply(out["p50"].to_numpy(), out["horizon"].to_numpy())
    elif champ["model_name"] == "seasonal_naive_168":  # bootstrap-fallback champion
        b = baseline_rows(history, issue)
        b = b[b["model_name"] == "seasonal_naive_168"].set_index("horizon")
        for c in ("p10", "p50", "p90"):
            out[c] = b.loc[out["horizon"], c].to_numpy()
    else:
        raise ValueError(f"unknown champion model {champ['model_name']!r}")
    return out, champ


def write_live_features(engine: Engine, frame: pd.DataFrame) -> None:
    from gridcast.drift.psi import LIVE_COLUMNS

    row = frame[frame["horizon"] == 1].iloc[0]
    params: dict[str, Any] = {"i": row["issue_time"].to_pydatetime()}
    for c in LIVE_COLUMNS:
        v = float(row[c])
        params[c] = None if np.isnan(v) else v
    cols = ", ".join(LIVE_COLUMNS)
    vals = ", ".join(f":{c}" for c in LIVE_COLUMNS)
    with engine.begin() as conn:
        conn.execute(
            text(
                f"INSERT INTO live_features (issue_time, {cols}) VALUES (:i, {vals})"  # noqa: S608 - constant columns
                " ON CONFLICT (issue_time) DO NOTHING"
            ),
            params,
        )


def _version_ids(conn: Connection, versions: list[str]) -> dict[str, int]:
    conn.execute(
        text(
            "INSERT INTO model_versions (model_version) SELECT unnest(CAST(:v AS text[]))"
            " ON CONFLICT (model_version) DO NOTHING"
        ),
        {"v": versions},
    )
    rows = conn.execute(
        text("SELECT model_version, version_id FROM model_versions WHERE model_version = ANY(:v)"),
        {"v": versions},
    ).all()
    return {str(r[0]): int(r[1]) for r in rows}


def write_predictions(
    engine: Engine, rows: pd.DataFrame, run_id: uuid.UUID, run_kind: str
) -> dict[str, int]:
    """Write rows (contract-checked) into predictions / prediction_runs. Returns rows per model."""
    contracts.Predictions.validate(rows)
    written: dict[str, int] = {}
    with engine.begin() as conn:
        ids: dict[str, int] = {
            str(n): int(i)
            for n, i in conn.execute(text("SELECT model_name, model_id FROM prediction_models"))
        }
        vids = _version_ids(conn, sorted(set(rows["model_version"])))
        issue = rows["issue_time"].iloc[0].to_pydatetime()
        for name, g in rows.groupby("model_name"):
            insert_only = name in INSERT_ONLY
            run_sql = RUN_INSERT_ONLY if insert_only else RUN_UPSERT
            conn.execute(
                text(run_sql), {"i": issue, "m": ids[str(name)], "r": run_id, "k": run_kind}
            )

            def opt(x: float) -> float | None:
                return None if np.isnan(x) else float(x)

            params = [
                {"i": issue, "m": ids[str(name)], "h": int(h), "v": vids[str(v)],
                 "p50": float(p50), "p10": opt(p10), "p90": opt(p90)}
                for h, v, p50, p10, p90 in zip(
                    g["horizon"].to_numpy(int), g["model_version"].to_numpy(str),
                    g["p50"].to_numpy(np.float64), g["p10"].to_numpy(np.float64),
                    g["p90"].to_numpy(np.float64), strict=True,
                )
            ]  # fmt: skip
            res = conn.execute(text(PRED_INSERT_ONLY if insert_only else PRED_UPSERT), params)
            written[str(name)] = int(res.rowcount or 0)
    return written


def run(
    engine: Engine,
    store: ObjectStore,
    issue_time: datetime,
    weather: str = "store",
    fetch_snapshot: Callable[[], tuple[pd.DataFrame, datetime]] | None = None,
    run_kind: str = "manual",
    weather_frame: pd.DataFrame | None = None,
) -> dict[str, Any]:
    """`weather`: 'store' | 'live' | 'frame' (use `weather_frame`, e.g. an archived snapshot) |
    'none' (no weather may be used: champion skipped, baselines and IESO still written)."""
    issue = to_utc(issue_time)
    cutoff = data_cutoff(issue)
    raw = load_history(engine, issue - BASELINE_WINDOW - LOOKBACK, issue + timedelta(hours=1))
    wx: pd.DataFrame | None
    if weather == "live":
        if fetch_snapshot is None:
            raise ValueError("live weather needs a snapshot fetcher")
        wx = _weather_live(fetch_snapshot, cutoff)
    elif weather == "frame":
        if weather_frame is None:
            raise ValueError("weather='frame' needs weather_frame")
        wx = weather_frame
    elif weather == "none":
        wx = None
    else:
        wx = load_weather(engine, issue - timedelta(hours=30), issue + timedelta(hours=24))
    history = DemandHistory.from_raw(raw)
    parts = [baseline_rows(history, issue), ieso_rows(engine, issue)]
    champ: dict[str, Any] | None = None
    max_avail = None
    shadow_error: str | None = None
    live_features_error: str | None = None
    if wx is not None:
        frame = FeatureBuilder(history, wx).frame([issue])  # assert_asof inside
        champ_rows, champ = champion_rows(store, frame, history, issue)
        parts.insert(0, champ_rows)
        max_avail = str(frame["input_available_at"].max())
        try:
            shadow = store.get_json(SHADOW_KEY) if store.exists(SHADOW_KEY) else None
            # only issue hours from the shadow's start: its training data ends before then
            if shadow is not None and issue >= floor_hour(
                to_utc(datetime.fromisoformat(shadow["started_at"]))
            ):
                parts.append(champion_rows(store, frame, history, issue, SHADOW_KEY, "shadow")[0])
        except Exception as exc:  # the shadow must never cost the champion its rows
            shadow_error = type(exc).__name__
        try:
            write_live_features(engine, frame)
        except Exception as exc:  # a drift diagnostic must never cost the forecast
            live_features_error = type(exc).__name__
    rows = pd.concat([p for p in parts if not p.empty], ignore_index=True)[
        ["issue_time", "target_time", "horizon", "model_name", "model_version", "p10", "p50", "p90"]
    ]
    rows["issue_time"] = pd.to_datetime(rows["issue_time"], utc=True)
    rows["target_time"] = pd.to_datetime(rows["target_time"], utc=True)
    written = write_predictions(engine, rows, uuid.uuid4(), run_kind)
    counts = rows.groupby("model_name").size().to_dict()
    return {
        "issue_time": issue.isoformat(),
        "data_cutoff": cutoff.isoformat(),
        "champion": (
            f"{champ['model_name']} {champ['model_version']} ({champ['champion_status']})"
            if champ
            else None
        ),
        "rows_built": {str(k): int(v) for k, v in counts.items()},
        "rows_written": written,
        "max_input_available_at": max_avail,
        "shadow_error": shadow_error,
        "live_features_error": live_features_error,
    }
