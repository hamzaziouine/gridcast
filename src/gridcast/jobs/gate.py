"""`gridcast gate evaluate | rollback` (C19, BUILD_PLAN §4.7 steps 2-8, §6.4; ADR-0016).

`evaluate` (gate.yml, daily), in this order:

1. **Rollback check** (§4.7 item 8). While `models/champion.prev.json` exists and the champion
   was promoted < 7 days ago, compare the champion with `prev_champion` over the **same trailing
   3 EST days** of final-scored target hours (both scored on every pair, >= 48 distinct target
   hours). Roll back when `MAPE_new > 1.15 x MAPE_prev` and the window holds no Ontario holiday
   (the plan's "not explained by an ICI/holiday flag"; ICI days are not flagged anywhere yet, so
   only holidays excuse a gap). A rollback ends the run.
2. **Shadow evaluation** (§4.7 items 2-4, 7). Pairs = (issue_time, target_time) both the shadow
   and the champion scored, from the shadow's `started_at`. Ready at >= 14 days AND >= 300
   distinct target hours, or early (`drift_early`): a refit shadow with >= 7 days and >= 150
   target hours while the champion's 7-day MAPE breach has lasted >= 7 consecutive days. Rules:
   MAPE_shadow <= 1.00 x (refit) / 0.99 x (challenger) MAPE_champion; peak MAPE (pairs whose target
   is a `peak_hours.is_peak` hour, >= 30 of them, else **wait**) <= champion + 0.2 pt; |coverage -
   0.80| <= champion's + 0.02. All pass -> `promoted` (unless a rollback cooldown runs -> wait);
   MAPE, peak or coverage fail -> `rejected`. A version blocked by an earlier rollback is rejected.
3. **Promotion**: `champion.prev.json` <- the old pointer, `champion.json` <- the shadow pointer
   (`promoted_at`, `champion_status='ok'`, `previous_version`), `shadow.json` deleted, event
   `promoted` with every number. The hourly job scores the old champion as `prev_champion` for
   7 days from `promoted_at`.
4. **Queue hand-over** (§4.7 item 6): once no shadow runs (resolved now or earlier), a waiting
   `queued.json` becomes `shadow.json` (`started_at` = now), event `shadow_started`.

`rollback` (manual, §4.7 item 8 "by hand"): `champion.json` <- `champion.prev.json` (+
`restored_at`, `rolled_back_from`), `champion.prev.json` deleted, event `rolled_back` naming the
demoted version as `blocked_version` and a 7-day promotion cooldown.

**Dry run by default** (CLI and manual dispatch): the decision and every number are computed from
live data and printed; nothing is written (no pointer, no event, no `pipeline_runs` row).
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

import holidays
import numpy as np
import pandas as pd
from sqlalchemy import Engine, text

from gridcast.config import get_settings
from gridcast.jobs import runs
from gridcast.jobs.predict import CHAMPION_KEY, PREV_KEY, PREV_SCORING, SHADOW_KEY
from gridcast.jobs.retrain import QUEUED_KEY
from gridcast.jobs.train import _event
from gridcast.storage.objstore import ObjectStore
from gridcast.timeutil import civil, est_date, floor_hour

JOB = "gate"
SHADOW_MIN_DAYS, SHADOW_MIN_TARGETS = 14, 300
EARLY_MIN_DAYS, EARLY_MIN_TARGETS, EARLY_BREACH_DAYS = 7, 150, 7
MAPE_RATIO = {"refit": 1.00, "challenger": 0.99}
PEAK_MIN_PAIRS, PEAK_TOLERANCE_PT = 30, 0.2
COVERAGE_NOMINAL, COVERAGE_TOLERANCE = 0.80, 0.02
ROLLBACK_DAYS, ROLLBACK_MIN_TARGETS, ROLLBACK_RATIO = 3, 48, 1.15
COOLDOWN = timedelta(days=7)
EPS = 1e-9  # a value exactly on a tolerance passes, as the plan reads


# ------------------------------------------------------------------------------ pure rules


@dataclass(frozen=True)
class PairStats:
    """Champion (c) vs candidate (s) on identical scored pairs."""

    n_pairs: int
    n_targets: int
    mape_c: float
    mape_s: float
    n_peak: int = 0
    peak_c: float = math.nan
    peak_s: float = math.nan
    cov_c: float = math.nan
    cov_s: float = math.nan


@dataclass(frozen=True)
class Decision:
    action: str  # 'promote' | 'reject' | 'hold' | 'rollback'
    reason: str
    triggered_by: str = "gate"
    checks: dict[str, Any] = field(default_factory=dict)


def shadow_decision(
    kind: str,
    age: timedelta,
    st: PairStats,
    early: bool = False,
    cooldown_until: datetime | None = None,
    blocked: bool = False,
) -> Decision:
    """§4.7 items 2-4 and 7 on one shadow."""
    if blocked:
        return Decision("reject", "version blocked by an earlier rollback")
    days = age.total_seconds() / 86400
    ready = days >= SHADOW_MIN_DAYS and st.n_targets >= SHADOW_MIN_TARGETS
    early_ok = (
        early and kind == "refit" and days >= EARLY_MIN_DAYS and st.n_targets >= EARLY_MIN_TARGETS
    )
    trig = "gate" if ready else "drift_early"
    if not (ready or early_ok):
        return Decision(
            "hold",
            f"shadow too young: {days:.1f} days, {st.n_targets} scored target hours (needs"
            f" {SHADOW_MIN_DAYS} days and {SHADOW_MIN_TARGETS})",
        )
    ratio = MAPE_RATIO[kind]
    checks: dict[str, Any] = {
        "kind": kind,
        "ratio": ratio,
        "mape_ok": bool(st.mape_s <= ratio * st.mape_c),
    }
    if not checks["mape_ok"]:
        return Decision(
            "reject",
            f"MAPE {st.mape_s:.3f} > {ratio:.2f} x champion {st.mape_c:.3f}",
            trig,
            checks,
        )
    if st.n_peak < PEAK_MIN_PAIRS:
        return Decision(
            "hold",
            f"insufficient peak sample: {st.n_peak} peak pairs (needs {PEAK_MIN_PAIRS})",
            trig,
            checks,
        )
    checks["peak_ok"] = bool(st.peak_s <= st.peak_c + PEAK_TOLERANCE_PT + EPS)
    gap_s, gap_c = abs(st.cov_s - COVERAGE_NOMINAL), abs(st.cov_c - COVERAGE_NOMINAL)
    checks["coverage_ok"] = bool(gap_s <= gap_c + COVERAGE_TOLERANCE + EPS)  # NaN -> False
    failed = [k[:-3] for k in ("peak_ok", "coverage_ok") if not checks[k]]
    if failed:
        return Decision(
            "reject",
            f"MAPE {st.mape_s:.3f} vs {st.mape_c:.3f} passed; failed: {', '.join(failed)}"
            f" (peak {st.peak_s:.3f} vs {st.peak_c:.3f} + {PEAK_TOLERANCE_PT}; coverage"
            f" {st.cov_s:.3f} vs {st.cov_c:.3f})",
            trig,
            checks,
        )
    why = (
        f"MAPE {st.mape_s:.3f} <= {ratio:.2f} x {st.mape_c:.3f} on {st.n_targets} target hours;"
        f" peak {st.peak_s:.3f} vs {st.peak_c:.3f}; coverage {st.cov_s:.3f} vs {st.cov_c:.3f}"
    )
    if cooldown_until is not None:
        return Decision("hold", f"passed, but promotion cooldown until {cooldown_until:%Y-%m-%d}"
                        f" after a rollback ({why})", trig, checks)  # fmt: skip
    return Decision("promote", why, trig, checks)


def rollback_decision(n_targets: int, mape_new: float, mape_prev: float, holiday: bool) -> Decision:
    """§4.7 item 8 on the same trailing window."""
    if n_targets < ROLLBACK_MIN_TARGETS:
        return Decision("hold", f"rollback check: {n_targets} paired target hours (needs"
                        f" {ROLLBACK_MIN_TARGETS})")  # fmt: skip
    worse = mape_new > ROLLBACK_RATIO * mape_prev
    why = f"MAPE new {mape_new:.3f} vs {ROLLBACK_RATIO} x prev {mape_prev:.3f} over {n_targets} h"
    if worse and holiday:
        return Decision("hold", f"{why}: worse, but the window holds a holiday (not rolled back)")
    if worse:
        return Decision("rollback", why, "gate")
    return Decision("hold", f"{why}: no rollback")


# ------------------------------------------------------------------------------ data


def _scored(engine: Engine, names: dict[str, str], since: datetime) -> pd.DataFrame:
    """v_scores rows of the given model_name -> model_version, issue_time >= since."""
    with engine.connect() as conn:
        df = pd.read_sql(
            text(
                "SELECT s.issue_time, s.target_time, s.model_name, s.p10, s.p50, s.p90,"
                " s.actual_mw AS actual, ph.is_peak FROM v_scores s"
                " LEFT JOIN peak_hours ph ON ph.target_time = s.target_time"
                " WHERE s.issue_time >= :since AND s.model_name = ANY(:n)"
                " AND s.model_version = ANY(:v)"
            ),
            conn,
            params={"since": since, "n": list(names), "v": list(names.values())},
        )
    df["target_time"] = pd.to_datetime(df["target_time"], utc=True)
    return df


def _metrics(g: pd.DataFrame) -> tuple[float, float]:
    ape = (g["p50"] - g["actual"]).abs() / g["actual"]
    has = g["p10"].notna() & g["p90"].notna()
    cov = (
        float(((g["actual"] >= g["p10"]) & (g["actual"] <= g["p90"]))[has].mean())
        if has.any()
        else math.nan
    )
    return float(ape.mean() * 100), cov


def pair_stats(df: pd.DataFrame, c: str, s: str) -> PairStats:
    key = ["issue_time", "target_time"]
    a = df[df["model_name"] == c].set_index(key)
    b = df[df["model_name"] == s].set_index(key)
    both = a.index.intersection(b.index)
    if not len(both):
        return PairStats(0, 0, math.nan, math.nan)
    a, b = a.loc[both], b.loc[both]
    mc, cc = _metrics(a)
    ms, cs = _metrics(b)
    pk = a["is_peak"].fillna(False).astype(bool).to_numpy()
    pc = _metrics(a[pk])[0] if pk.any() else math.nan
    ps = _metrics(b[pk])[0] if pk.any() else math.nan
    targets = both.get_level_values("target_time").nunique()
    return PairStats(len(both), int(targets), mc, ms, int(pk.sum()), pc, ps, cc, cs)


def _ts(x: Any) -> datetime:
    t = datetime.fromisoformat(str(x))
    return t if t.tzinfo else t.replace(tzinfo=UTC)


def _registry_state(engine: Engine, now: datetime) -> tuple[datetime | None, set[str]]:
    """(cooldown end if a rollback happened < 7 days ago, versions blocked by rollbacks)."""
    with engine.connect() as conn:
        rows = conn.execute(
            text(
                "SELECT metrics->>'cooldown_until', metrics->>'blocked_version'"
                " FROM model_registry_events WHERE event_type = 'rolled_back'"
            )
        ).all()
    blocked = {str(r[1]) for r in rows if r[1]}
    ends = [_ts(r[0]) for r in rows if r[0]]  # written by do_rollback: rollback time + 7 days
    until = max(ends, default=None)
    return (until if until is not None and now < until else None), blocked


def _breach_days(engine: Engine) -> int:
    with engine.connect() as conn:
        v = conn.execute(
            text(
                "SELECT CASE WHEN breached THEN (context->>'consecutive_days')::int ELSE 0 END"
                " FROM drift_metrics WHERE metric = 'rolling_mape_7d' AND feature = ''"
                " ORDER BY run_time DESC LIMIT 1"
            )
        ).scalar()
    return int(v or 0)


def _holiday_in(targets: pd.Series) -> bool:
    days: set[date] = {civil(t.to_pydatetime())[0] for t in pd.DatetimeIndex(targets)}
    on = holidays.country_holidays("CA", subdiv="ON", years=sorted({d.year for d in days}))
    return any(d in on for d in days)


def rollback_check(
    engine: Engine, champion: dict[str, Any], prev: dict[str, Any], now: datetime
) -> tuple[Decision, dict[str, Any]]:
    since = _ts(champion["promoted_at"]) - timedelta(hours=24)
    df = _scored(engine, {"champion": champion["model_version"],
                          "prev_champion": prev["model_version"]}, since)  # fmt: skip
    if df.empty:
        return rollback_decision(0, math.nan, math.nan, False), {"n_targets": 0}
    end = df["target_time"].max()
    first_day = est_date(end.to_pydatetime()) - timedelta(days=ROLLBACK_DAYS - 1)
    w = df[[est_date(t) >= first_day for t in df["target_time"].dt.to_pydatetime()]]
    st = pair_stats(w, "champion", "prev_champion")
    hol = _holiday_in(w["target_time"].drop_duplicates()) if len(w) else False
    d = rollback_decision(st.n_targets, st.mape_c, st.mape_s, hol)
    return d, {"window_first_day": str(first_day), "n_targets": st.n_targets,
               "mape_new": st.mape_c, "mape_prev": st.mape_s, "holiday": hol}  # fmt: skip


# ------------------------------------------------------------------------------ actions


def _get(store: ObjectStore, key: str) -> dict[str, Any] | None:
    return store.get_json(key) if store.exists(key) else None


def _common(p: dict[str, Any], triggered_by: str) -> dict[str, Any]:
    return {
        "model_name": str(p.get("model_name", "lgbm")),
        "model_version": str(p["model_version"]),
        "mlflow_run_id": p.get("mlflow_run_id"),
        "artifact_key": p.get("artifact_key"),
        "triggered_by": triggered_by,
    }


def do_rollback(
    engine: Engine, store: ObjectStore, now: datetime, reason: str, triggered_by: str,
    numbers: dict[str, Any] | None = None,
) -> dict[str, Any]:  # fmt: skip
    champion, prev = store.get_json(CHAMPION_KEY), _get(store, PREV_KEY)
    if prev is None:
        raise ValueError("no models/champion.prev.json: nothing to roll back to")
    restored = {k: v for k, v in prev.items() if k not in ("restored_at", "rolled_back_from")}
    restored.update(restored_at=now.isoformat(), rolled_back_from=champion["model_version"])
    store.put_json(CHAMPION_KEY, restored)
    store.delete(PREV_KEY)
    metrics = {**(numbers or {}), "restored_version": prev["model_version"],
               "blocked_version": champion["model_version"],
               "cooldown_until": (now + COOLDOWN).isoformat()}  # fmt: skip
    _event(engine, event_type="rolled_back", reason=reason, metrics=metrics,
           **_common(champion, triggered_by))  # fmt: skip
    return {"rolled_back": champion["model_version"], "restored": prev["model_version"]}


def do_promote(
    engine: Engine, store: ObjectStore, shadow: dict[str, Any], d: Decision, st: PairStats,
    now: datetime,
) -> dict[str, Any]:  # fmt: skip
    old = store.get_json(CHAMPION_KEY)
    store.put_json(PREV_KEY, old)
    keep = ("model_name", "model_version", "artifact_key", "sha256", "feature_schema_version",
            "intervals_key", "mlflow_run_id", "profile", "kind")  # fmt: skip
    new = {k: shadow.get(k) for k in keep}
    new.update(promoted_at=now.isoformat(), champion_status="ok", bootstrap_reason=None,
               previous_version=old["model_version"])  # fmt: skip
    store.put_json(CHAMPION_KEY, new)
    store.delete(SHADOW_KEY)
    _event(engine, event_type="promoted", reason=d.reason,
           metrics={**st.__dict__, **d.checks, "previous_version": old["model_version"]},
           **_common(shadow, d.triggered_by))  # fmt: skip
    return {"promoted": shadow["model_version"], "previous": old["model_version"]}


def hand_over(engine: Engine, store: ObjectStore, now: datetime) -> str | None:
    queued = _get(store, QUEUED_KEY)
    if queued is None or store.exists(SHADOW_KEY):
        return None
    store.put_json(SHADOW_KEY, {**queued, "started_at": now.isoformat()})
    store.delete(QUEUED_KEY)
    _event(engine, event_type="shadow_started", reason="queue hand-over after the gate",
           metrics={"kind": queued.get("kind")}, **_common(queued, "gate"))  # fmt: skip
    return str(queued["model_version"])


def evaluate(engine: Engine, store: ObjectStore, now: datetime, apply: bool) -> dict[str, Any]:
    out: dict[str, Any] = {"mode": "apply" if apply else "dry-run", "now": now.isoformat()}
    champion = store.get_json(CHAMPION_KEY)
    shadow, prev, queued = _get(store, SHADOW_KEY), _get(store, PREV_KEY), _get(store, QUEUED_KEY)
    out["pointers"] = {
        "champion": champion.get("model_version"),
        "shadow": (shadow or {}).get("model_version"),
        "prev_champion": (prev or {}).get("model_version"),
        "queued": (queued or {}).get("model_version"),
    }
    cooldown_until, blocked = _registry_state(engine, now)
    out["cooldown_until"] = cooldown_until.isoformat() if cooldown_until else None

    # 1. rollback check while prev_champion is still scored
    if (
        prev is not None
        and champion.get("promoted_at")
        and now - _ts(champion["promoted_at"]) < PREV_SCORING
    ):
        rb, nums = rollback_check(engine, champion, prev, now)
        out["rollback_check"] = {"action": rb.action, "reason": rb.reason, **nums}
        if rb.action == "rollback":
            out["decision"] = "rollback"
            if apply:
                out["result"] = do_rollback(
                    engine, store, now, f"automatic: {rb.reason}", "gate", nums
                )
            return out

    # 2-3. the shadow
    if shadow is None:
        out["decision"], out["reason"] = "hold", "no shadow running"
    else:
        started = _ts(shadow["started_at"])
        df = _scored(engine, {"champion": champion["model_version"],
                              "shadow": shadow["model_version"]}, floor_hour(started))  # fmt: skip
        st = pair_stats(df, "champion", "shadow")
        early = _breach_days(engine) >= EARLY_BREACH_DAYS
        d = shadow_decision(str(shadow.get("kind") or "challenger"), now - started, st, early,
                            cooldown_until, str(shadow["model_version"]) in blocked)  # fmt: skip
        out.update(decision=d.action, reason=d.reason, triggered_by=d.triggered_by,
                   stats=st.__dict__, checks=d.checks,
                   shadow_age_days=round((now - started).total_seconds() / 86400, 2))  # fmt: skip
        if apply and d.action == "promote":
            out["result"] = do_promote(engine, store, shadow, d, st, now)
        elif apply and d.action == "reject":
            store.delete(SHADOW_KEY)
            _event(
                engine,
                event_type="rejected",
                reason=f"shadow stage: {d.reason}",
                metrics={**st.__dict__, **d.checks},
                **_common(shadow, d.triggered_by),
            )
            out["result"] = {"rejected": shadow["model_version"]}

    # 4. queue hand-over
    if queued is not None and (shadow is None or out["decision"] in ("promote", "reject")):
        out["hand_over"] = hand_over(engine, store, now) if apply else queued["model_version"]
    return out


def _json_safe(x: Any) -> Any:
    if isinstance(x, dict):
        return {k: _json_safe(v) for k, v in x.items()}
    if isinstance(x, float | np.floating):
        return None if not math.isfinite(float(x)) else round(float(x), 4)
    return x


def run(
    engine: Engine,
    store: ObjectStore,
    action: str = "evaluate",
    apply: bool = False,
    reason: str = "manual rollback",
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    ping: runs.Pinger = runs.http_get,
    run_kind: str = "manual",
) -> dict[str, Any]:
    """Dry run (apply=False): read-only, no pipeline_runs row, no heartbeat."""
    t = now()
    run_id = runs.start(engine, JOB, floor_hour(t), run_kind) if apply else None
    notes = runs.Notes()
    status = "ok"
    try:
        if action == "rollback":
            out: dict[str, Any] = {"mode": "apply" if apply else "dry-run", "decision": "rollback"}
            prev = _get(store, PREV_KEY)
            out["pointers"] = {"champion": store.get_json(CHAMPION_KEY)["model_version"],
                               "prev_champion": (prev or {}).get("model_version")}  # fmt: skip
            if prev is None:
                raise ValueError("no models/champion.prev.json: nothing to roll back to")
            if apply:
                out["result"] = do_rollback(engine, store, t, reason, "manual")
        elif action == "evaluate":
            out = evaluate(engine, store, t, apply)
        else:
            raise ValueError(f"unknown action {action!r}")
        notes.add(json.dumps({k: out.get(k) for k in ("decision", "reason")}, default=str))
    except Exception as exc:
        out = {"mode": "apply" if apply else "dry-run", "action": action}
        if isinstance(exc, ValueError):  # the gate's own guard messages (no driver text)
            out["error"] = str(exc)
        notes.error("gate", exc)
        status = "failed"
    out = _json_safe(out)
    if run_id is not None:
        out["status"] = runs.finish(engine, run_id, status, 0, notes.private)
        out["heartbeat"] = runs.heartbeat(get_settings().healthchecks_gate_url,
                                          out["status"] != "failed", ping)  # fmt: skip
    else:
        out["status"] = status
    out["notes"] = notes.public
    return out
