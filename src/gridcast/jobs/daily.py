"""`gridcast run daily` (C11, BUILD_PLAN §6.2 minimum + drift signals; DATA_NOTES §18).

For the EST day D the run is for (default: today, EST), logical_time = D 00:00 EST:

1. The newest PUB_Demand_<year>_vNNN.csv (and last year's in January), observed stamps ->
   raw_demand_history (new hours and revisions only) -> actuals (revision bumps). A 404 /
   outage is a note, not a crash.
2. Score every prediction whose target has an actual, for targets in the 45 EST days before D
   (D itself is excluded: its HE1 is provisional in the 07:30 file). New rows are inserted in
   target order (right-most leaf, migration 0004); rows whose actual moved are re-scored.
3. peak_hours for those targets: P90 of the 720 actuals of the 30 EST days before the target's
   day, only when all 720 exist (plan §4.3 3.6b).
4. score_daily for the same 45 days (delete + insert in one transaction), final_only = true,
   bands all / 1-6 / 7-12 / 13-24, peak MAPE from peak_hours.
5. Drift signals as plan §6.2 step 4 defines the error-based ones (PSI is not built yet):
   rolling_mape_7d (champion) vs 1.5 x ref_mape_30d (champion's trailing 30 d excluding the
   last 7, or its backtest MAPE until 30 live days exist), naive_mape_7d (seasonal_naive_168,
   context), coverage_30d vs [0.72, 0.88], consecutive breach days in `context`; plus the §6.2
   step 6c storage guard (neon_storage_mb, warn 400, fail 450). Nothing triggers a retrain:
   C11 records the signals only (DATA_NOTES §18.3).
6. Publish latest/scoreboard.json (schema below). Heartbeat HEALTHCHECKS_DAILY_URL (optional).

Status: `skipped` (retried by the 18:20 slot) when yesterday HE24 is still missing after the
ingest; `failed` when the newest actual is > 48 h old, the database passes 450 MB, or a step
errors; else `ok`.

scoreboard.json (schema_version 1): {schema_version, generated_at, scored_through (last scored
EST date), champion {model_version, champion_status, backtest_mape}, pairing, windows {"7d",
"30d": {start, end, models {name: {n, mape, mae, rmse, coverage_80, peak_mape, n_peak}}},
"since_launch": {start, end, models {name: {n, mape, coverage_80}}}}, by_horizon {"30d":
{name: [{horizon, n, mape, mae, coverage_80}]}}, daily [{date, <model>: mape}], attribution}.
Every window compares models on the same (target_time, horizon) pairs: those the champion has.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd
from sqlalchemy import Connection, Engine, text

from gridcast.backtest.harness import peak_thresholds, score
from gridcast.config import get_settings
from gridcast.ingest import ieso_demand
from gridcast.ingest.http import Fetched, list_directory
from gridcast.jobs import predict, publish, runs
from gridcast.storage.objstore import ObjectStore
from gridcast.timeutil import est_date, he_to_utc

JOB = "daily"
SCORE_DAYS = 45
BANDS = (("all", 1, 24), ("1-6", 1, 6), ("7-12", 7, 12), ("13-24", 13, 24))
MAPE_RATIO = 1.5
COVERAGE_DRIFT_BAND = (0.72, 0.88)
STORAGE_WARN_MB, STORAGE_FAIL_MB = 400.0, 450.0
ACTUALS_MAX_AGE = timedelta(hours=48)
MODELS = ("champion", "ieso_adequacy", "seasonal_naive_168", "seasonal_naive_24", "persistence")


@dataclass
class Deps:
    fetch: Callable[[str], Fetched]
    now: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    ping: runs.Pinger = runs.http_get


def day_start(d: date) -> datetime:
    """00:00 EST of d, as UTC (= HE1 interval start)."""
    return he_to_utc(d, 1)


# ------------------------------------------------------------------------------ 1. ingest


def ingest(engine: Engine, deps: Deps, day: date, notes: runs.Notes) -> int:
    """Newest versioned PUB_Demand_<year>_vNNN.csv for this year (and last year in January).

    The versioned name, not `PUB_Demand.csv`: raw_demand_history is keyed by file name, so a
    revision read from the same unsuffixed name would collide with the version it revises.
    """
    try:
        listing = deps.fetch(ieso_demand.BASE_URL)
        if listing.status != 200:
            notes.add(f"pub_demand: listing HTTP {listing.status}")
            return 0
        files = ieso_demand.latest_files_by_year(list_directory(listing.text))
    except Exception as exc:
        notes.error("pub_demand listing", exc)
        return 0
    years = [day.year - 1, day.year] if day.month == 1 else [day.year]
    changed = 0
    for year in years:
        name = files.get(year)
        if name is None:
            notes.add(f"pub_demand: no file listed for {year}")
            continue
        try:
            fetched = deps.fetch(ieso_demand.BASE_URL + name)
            if fetched.status != 200:
                notes.add(f"pub_demand: HTTP {fetched.status} for {name}")
                continue
            changed += ieso_demand.ingest_pub_demand(engine, fetched, "observed").rows_inserted
        except Exception as exc:
            notes.error("pub_demand", exc)
    return changed


# ------------------------------------------------------------------------------ 2. score

_SCORE_SQL = """
INSERT INTO scores (target_time, model_id, horizon, actual_revision, actual_mw)
SELECT a.target_time, p.model_id, p.horizon, a.revision::smallint, a.ontario_demand_mw::real
FROM predictions p
JOIN actuals a ON a.target_time = p.issue_time + p.horizon * interval '1 hour'
WHERE p.issue_time >= CAST(:lo AS timestamptz) - interval '24 hours'
  AND p.issue_time < CAST(:hi AS timestamptz)
  AND a.target_time >= :lo AND a.target_time < :hi
ORDER BY a.target_time, p.model_id, p.horizon
ON CONFLICT (target_time, model_id, horizon) DO UPDATE
  SET actual_revision = EXCLUDED.actual_revision, actual_mw = EXCLUDED.actual_mw
  WHERE scores.actual_revision IS DISTINCT FROM EXCLUDED.actual_revision
     OR scores.actual_mw IS DISTINCT FROM EXCLUDED.actual_mw
RETURNING (xmax = 0) AS inserted
"""


def score_window(engine: Engine, lo: datetime, hi: datetime) -> dict[str, int]:
    with engine.begin() as conn:
        flags = [bool(r[0]) for r in conn.execute(text(_SCORE_SQL), {"lo": lo, "hi": hi})]
    return {"inserted": sum(flags), "rescored": len(flags) - sum(flags)}


# ------------------------------------------------------------------------------ 3. peak hours


def upsert_peak_hours(engine: Engine, lo: datetime, hi: datetime) -> int:
    with engine.connect() as conn:
        df = pd.read_sql(
            text(
                "SELECT target_time, ontario_demand_mw::float8 AS v FROM actuals"
                " WHERE target_time >= :a AND target_time < :b ORDER BY 1"
            ),
            conn,
            params={"a": lo - timedelta(days=31), "b": hi},
        )
    if df.empty:
        return 0
    actuals = pd.Series(
        df["v"].to_numpy(), index=pd.DatetimeIndex(pd.to_datetime(df["target_time"], utc=True))
    )
    thr = peak_thresholds(actuals)
    rows = []
    recent = actuals[actuals.index >= pd.Timestamp(lo)]
    for t, v in zip(pd.DatetimeIndex(recent.index).to_pydatetime(), recent.to_numpy(), strict=True):
        d = est_date(t)
        th = thr.get(d, float("nan"))
        if np.isnan(th):
            continue
        rows.append(
            {
                "t": t,
                "thr": round(th, 2),
                "peak": bool(v >= th),
                "rs": d - timedelta(days=30),
                "re": d - timedelta(days=1),
            }
        )
    if rows:
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO peak_hours (target_time, threshold_mw, is_peak, ref_start,"
                    " ref_end) VALUES (:t, :thr, :peak, :rs, :re) ON CONFLICT (target_time) DO"
                    " UPDATE SET threshold_mw = EXCLUDED.threshold_mw, is_peak = EXCLUDED.is_peak,"
                    " ref_start = EXCLUDED.ref_start, ref_end = EXCLUDED.ref_end,"
                    " computed_at = now() WHERE peak_hours.threshold_mw <> EXCLUDED.threshold_mw"
                    " OR peak_hours.is_peak <> EXCLUDED.is_peak"
                ),
                rows,
            )
    return len(rows)


# ------------------------------------------------------------------------------ 4. score_daily


def load_scores(conn: Connection, lo: datetime, hi: datetime) -> pd.DataFrame:
    df = pd.read_sql(
        text(
            "SELECT s.target_time, s.horizon, s.model_name, s.p10, s.p50, s.p90,"
            " s.actual_mw AS actual, ph.is_peak FROM v_scores s"
            " LEFT JOIN peak_hours ph ON ph.target_time = s.target_time"
            " WHERE s.target_time >= :lo AND s.target_time < :hi"
        ),
        conn,
        params={"lo": lo, "hi": hi},
    )
    df["target_time"] = pd.to_datetime(df["target_time"], utc=True)
    df["score_date"] = [est_date(t) for t in df["target_time"].dt.to_pydatetime()]
    df["is_peak"] = df["is_peak"].astype("boolean")
    return df


def matched(df: pd.DataFrame) -> pd.DataFrame:
    """Rows whose (target_time, horizon) the champion also forecast."""
    keys = df.loc[df["model_name"] == "champion", ["target_time", "horizon"]]
    return df.merge(keys.drop_duplicates(), on=["target_time", "horizon"], how="inner")


def daily_rows(df: pd.DataFrame) -> list[dict[str, Any]]:
    rows = []
    for (d, name), g in df.groupby(["score_date", "model_name"]):
        for band, lo, hi in BANDS:
            m = score(g[g["horizon"].between(lo, hi)])
            if not m["n"]:
                continue
            rows.append(
                {
                    "d": d,
                    "m": name,
                    "b": band,
                    "n": int(m["n"]),
                    "mape": _num(m.get("mape"), 5),
                    "rmse": _num(m.get("rmse"), 3),
                    "cov": _num(m.get("coverage_80"), 4),
                    "wink": _num(m.get("winkler_80"), 3),
                    "pk": _num(m.get("peak_mape"), 5),
                    "npk": int(m.get("n_peak", 0)),
                }
            )
    return rows


def _num(x: Any, digits: int) -> float | None:
    if x is None or (isinstance(x, float) and np.isnan(x)):
        return None
    return round(float(x), digits)


def rebuild_score_daily(engine: Engine, df: pd.DataFrame, first_day: date) -> int:
    rows = daily_rows(df)
    with engine.begin() as conn:
        conn.execute(text("DELETE FROM score_daily WHERE score_date >= :d"), {"d": first_day})
        if rows:
            conn.execute(
                text(
                    "INSERT INTO score_daily (score_date, model_name, horizon_band, final_only, n,"
                    " mape, rmse, coverage_80, winkler_80, peak_mape, n_peak) VALUES (:d, :m, :b,"
                    " true, :n, :mape, :rmse, :cov, :wink, :pk, :npk)"
                ),
                rows,
            )
    return len(rows)


# ------------------------------------------------------------------------------ 5. drift


def _mape(df: pd.DataFrame, name: str) -> float:
    g = df[df["model_name"] == name]
    return float(score(g)["mape"]) if len(g) else float("nan")


def backtest_mape(engine: Engine, version: str | None) -> float:
    if not version:
        return float("nan")
    with engine.connect() as conn:
        v = conn.execute(
            text(
                "SELECT (metrics->>'lgbm_mape')::float8 FROM model_registry_events WHERE"
                " event_type = 'backtested' AND model_version = :v ORDER BY event_time DESC LIMIT 1"
            ),
            {"v": version},
        ).scalar()
    return float("nan") if v is None else float(v)


def drift_signals(
    engine: Engine, df: pd.DataFrame, hi: datetime, run_time: datetime, bt_mape: float
) -> list[dict[str, Any]]:
    w7 = df[df["target_time"] >= hi - timedelta(days=7)]
    w30 = df[df["target_time"] >= hi - timedelta(days=30)]
    ref_part = w30[w30["target_time"] < hi - timedelta(days=7)]
    live_days = df.loc[df["model_name"] == "champion", "score_date"].nunique()
    if live_days >= 30:
        ref, ref_basis = _mape(ref_part, "champion"), "live trailing 30 d excl. last 7"
    else:
        ref, ref_basis = bt_mape, f"backtest MAPE (only {live_days} live days)"
    roll7 = _mape(w7, "champion")
    cov = score(w30[w30["model_name"] == "champion"]).get("coverage_80", float("nan"))
    with engine.connect() as conn:
        size_mb = (
            float(conn.execute(text("SELECT pg_database_size(current_database())")).scalar_one())
            / 1e6
        )
        prev = conn.execute(
            text(
                "SELECT metric, (context->>'consecutive_days')::int FROM drift_metrics"
                " WHERE run_time = (SELECT max(run_time) FROM drift_metrics WHERE run_time < :t)"
            ),
            {"t": run_time},
        ).all()
    streak = {str(r[0]): int(r[1] or 0) for r in prev}
    win = (hi - timedelta(days=7), hi)
    w30_ = (hi - timedelta(days=30), hi)

    def row(
        metric: str,
        value: float,
        threshold: float,
        breached: bool,
        window: tuple[datetime, datetime],
        **ctx: Any,
    ) -> dict[str, Any] | None:
        if np.isnan(value):
            return None
        days = streak.get(metric, 0) + 1 if breached else 0
        return {
            "rt": run_time, "ws": window[0], "we": window[1], "metric": metric,
            "value": round(value, 6), "br": breached,
            "thr": 0.0 if np.isnan(threshold) else round(threshold, 6),
            "ctx": json.dumps({"consecutive_days": days, **ctx}),
        }  # fmt: skip

    thr = MAPE_RATIO * ref
    out = [
        row("rolling_mape_7d", roll7, thr, bool(roll7 > thr), win,
            ref_basis=ref_basis, trigger_rule="breached 3 consecutive days -> retrain (not wired)"),
        row("ref_mape_30d", ref, float("nan"), False, w30_, basis=ref_basis),
        row("naive_mape_7d", _mape(w7, "seasonal_naive_168"), float("nan"), False, win),
        row("coverage_30d", float(cov), COVERAGE_DRIFT_BAND[0],
            bool(not COVERAGE_DRIFT_BAND[0] <= cov <= COVERAGE_DRIFT_BAND[1]), w30_,
            band=list(COVERAGE_DRIFT_BAND),
            trigger_rule="7 days outside -> recalibrate (not wired)"),
        row("neon_storage_mb", size_mb, STORAGE_FAIL_MB, size_mb > STORAGE_FAIL_MB,
            (run_time, run_time), warn_mb=STORAGE_WARN_MB, warn=size_mb > STORAGE_WARN_MB),
    ]  # fmt: skip
    return [r for r in out if r is not None]


def write_drift(engine: Engine, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO drift_metrics (run_time, window_start, window_end, metric, feature,"
                " value, threshold, breached, context) VALUES (:rt, :ws, :we, :metric, '', :value,"
                " :thr, :br, CAST(:ctx AS jsonb)) ON CONFLICT (run_time, metric, feature) DO UPDATE"
                " SET value = EXCLUDED.value, threshold = EXCLUDED.threshold, breached ="
                " EXCLUDED.breached, context = EXCLUDED.context, window_start ="
                " EXCLUDED.window_start, window_end = EXCLUDED.window_end"
            ),
            rows,
        )


# ------------------------------------------------------------------------------ 6. scoreboard


def _metrics(g: pd.DataFrame) -> dict[str, Any]:
    m = score(g)
    d = g.dropna(subset=["p50", "actual"])
    mae = float(np.mean(np.abs(d["p50"] - d["actual"]))) if len(d) else float("nan")
    return {
        "n": int(m["n"]),
        "mape": publish.num(m.get("mape"), 3),
        "mae": publish.num(mae, 1),
        "rmse": publish.num(m.get("rmse"), 1),
        "coverage_80": publish.num(m.get("coverage_80"), 3),
        "peak_mape": publish.num(m.get("peak_mape"), 3),
        "n_peak": int(m.get("n_peak", 0)),
    }


def scoreboard_doc(
    engine: Engine,
    df: pd.DataFrame,
    hi: datetime,
    champion: dict[str, Any] | None,
    bt_mape: float,
    now: datetime,
) -> dict[str, Any]:
    mdf = matched(df)
    windows: dict[str, Any] = {}
    for label, days in (("7d", 7), ("30d", 30)):
        lo = hi - timedelta(days=days)
        w = mdf[mdf["target_time"] >= lo]
        windows[label] = {
            "start": publish.iso(lo),
            "end": publish.iso(hi),
            "models": {str(n): _metrics(g) for n, g in w.groupby("model_name")},
        }
    with engine.connect() as conn:
        sd = pd.read_sql(
            text(
                "SELECT score_date, model_name, n, mape::float8 AS mape,"
                " coverage_80::float8 AS cov FROM score_daily WHERE horizon_band = 'all'"
                " ORDER BY score_date"
            ),
            conn,
        )
    since: dict[str, Any] = {}
    for n, g in sd.groupby("model_name"):
        tot = int(g["n"].sum())
        cov = g.dropna(subset=["cov"])
        since[str(n)] = {
            "n": tot,
            "mape": publish.num((g["mape"] * g["n"]).sum() / tot, 3) if tot else None,
            "coverage_80": publish.num((cov["cov"] * cov["n"]).sum() / cov["n"].sum(), 3)
            if len(cov)
            else None,
        }
    windows["since_launch"] = {
        "start": str(sd["score_date"].min()) if len(sd) else None,
        "end": str(sd["score_date"].max()) if len(sd) else None,
        "models": since,
        "note": "n-weighted from score_daily (each model on its own scored rows)",
    }
    w30 = mdf[mdf["target_time"] >= hi - timedelta(days=30)]
    by_h = {
        str(n): [{"horizon": int(str(h)), **{k: v for k, v in _metrics(gh).items()
                                              if k in ("n", "mape", "mae", "coverage_80")}}
                 for h, gh in g.groupby("horizon")]
        for n, g in w30.groupby("model_name")
    }  # fmt: skip
    recent = sd[sd["score_date"] >= (hi - timedelta(days=30)).date()]
    daily = [
        {"date": str(d), **{str(r.model_name): publish.num(r.mape, 3) for r in g.itertuples()}}
        for d, g in recent.groupby("score_date")
    ]
    scored = df["score_date"].max() if len(df) else None
    return {
        "schema_version": publish.SCHEMA_VERSION,
        "generated_at": publish.iso(now),
        "scored_through": str(scored) if scored is not None else None,
        "champion": {
            "model_version": (champion or {}).get("model_version"),
            "champion_status": (champion or {}).get("champion_status"),
            "backtest_mape": publish.num(bt_mape, 3),
        },
        "pairing": "7d/30d windows and by_horizon use only (target_time, horizon) pairs the"
        " champion forecast; actuals are final IESO PUB_Demand Ontario Demand, scored the day"
        " after the target hour.",
        "windows": windows,
        "by_horizon": {"30d": by_h},
        "daily": daily,
        "attribution": list(publish.ATTRIBUTION),
    }


# ------------------------------------------------------------------------------ the job


def run(
    engine: Engine,
    store: ObjectStore,
    public: ObjectStore | None,
    deps: Deps,
    day: date | None = None,
    run_kind: str = "scheduled",
) -> dict[str, Any]:
    now = deps.now()
    d = day or est_date(now)
    hi = day_start(d)
    lo = hi - timedelta(days=SCORE_DAYS)
    hc_url = get_settings().healthchecks_daily_url
    out: dict[str, Any] = {"day": str(d), "run_kind": run_kind}
    run_id = runs.start(engine, JOB, hi, run_kind)
    if runs.already_ok(engine, JOB, hi):
        out["status"] = runs.finish(engine, run_id, "skipped", 0, ["already ok"])
        out["heartbeat"] = runs.heartbeat(hc_url, True, deps.ping)
        return out

    notes = runs.Notes()
    status = "ok"
    out["pub_demand_rows"] = ingest(engine, deps, d, notes)
    written = out["pub_demand_rows"]
    try:
        out["scores"] = score_window(engine, lo, hi)
        written += sum(out["scores"].values())
        out["peak_hours"] = upsert_peak_hours(engine, lo, hi)
        with engine.connect() as conn:
            df = load_scores(conn, lo, hi)
        out["score_daily_rows"] = rebuild_score_daily(engine, df, lo.astimezone(UTC).date())
        champion: dict[str, Any] | None = None
        try:
            champion = store.get_json(predict.CHAMPION_KEY)
        except Exception as exc:
            notes.error("champion.json", exc)
        bt = backtest_mape(engine, (champion or {}).get("model_version"))
        drift = drift_signals(engine, df, hi, hi, bt)
        write_drift(engine, drift)
        out["drift"] = {r["metric"]: [r["value"], r["br"]] for r in drift}
        if any(r["metric"] == "neon_storage_mb" and r["br"] for r in drift):
            notes.add(f"neon storage above {STORAGE_FAIL_MB} MB")
            status = "failed"
        if public is not None:
            doc = scoreboard_doc(engine, df, hi, champion, bt, now)
            out["published"] = {
                publish.SCOREBOARD_KEY: publish.put(public, publish.SCOREBOARD_KEY, doc)
            }
    except Exception as exc:
        notes.error("score/publish", exc)
        status = "failed"

    with engine.connect() as conn:
        newest = conn.execute(text("SELECT max(target_time) FROM actuals")).scalar()
    out["newest_actual"] = publish.iso(newest)
    if newest is None or now - newest > ACTUALS_MAX_AGE:
        notes.add("actuals_stale: newest final actual older than 48 h")
        status = "failed"
    elif newest < hi - timedelta(hours=1) and status == "ok":
        notes.add("pub_demand_incomplete: yesterday HE24 not posted yet (retry next slot)")
        status = "skipped"

    out["status"] = runs.finish(engine, run_id, status, written, notes.private)
    out["notes"] = notes.public
    out["heartbeat"] = runs.heartbeat(hc_url, out["status"] != "failed", deps.ping)
    return out
