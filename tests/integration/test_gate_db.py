"""C19 against real Postgres + moto: the gate's whole life cycle on scored rows.

hold (young shadow) -> promote (+ queue hand-over) -> automatic rollback (new champion > 1.15 x
prev_champion over 3 days) -> cooldown hold -> blocked version rejected; plus a plain reject and a
manual rollback, and a dry run that writes nothing.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import numpy as np
import pandas as pd
import pytest
from moto import mock_aws
from sqlalchemy import Engine, text

from gridcast import db
from gridcast.jobs import gate, predict, retrain
from tests.integration.helpers import make_buckets

pytestmark = pytest.mark.integration

S = datetime(2027, 1, 1, tzinfo=UTC)  # shadow start
ACTUAL = 15000.0


def pointer(version: str, **kw: Any) -> dict[str, Any]:
    return {"model_name": "lgbm", "model_version": version,
            "artifact_key": f"models/{version}/model.json.gz", "sha256": "0" * 64,
            "intervals_key": f"models/{version}/intervals.v0001.json",
            "feature_schema_version": 1, "kind": "challenger", "profile": "fast", **kw}  # fmt: skip


def seed(eng: Engine, start: datetime, hours: int, models: dict[str, tuple[str, float]],
         peak_first: int = 0) -> None:  # fmt: skip
    """Hourly issues from `start`, horizon 1 only; model_name -> (version, relative error)."""
    issues = pd.date_range(start, periods=hours, freq="h")
    for issue in issues:
        rows = []
        for name, (version, err) in models.items():
            p50 = ACTUAL * (1 + err)
            rows.append({"issue_time": issue, "target_time": issue + pd.Timedelta(hours=1),
                         "horizon": 1, "model_name": name, "model_version": version,
                         "p10": p50 * 0.97, "p50": p50, "p90": p50 * 1.03})  # fmt: skip
        predict.write_predictions(eng, pd.DataFrame(rows), uuid.uuid4(), "scheduled")
    targets = [(i + pd.Timedelta(hours=1)).to_pydatetime() for i in issues]
    with eng.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO scores (target_time, model_id, horizon, actual_revision, actual_mw)"
                " SELECT p.issue_time + interval '1 hour', p.model_id, 1, 1, :a FROM predictions p"
                " WHERE p.issue_time >= :s AND p.issue_time < :e ON CONFLICT DO NOTHING"
            ),
            {"a": ACTUAL, "s": start, "e": start + timedelta(hours=hours)},
        )
        conn.execute(
            text(
                "INSERT INTO peak_hours (target_time, threshold_mw, is_peak, ref_start, ref_end)"
                " VALUES (:t, 1, :p, '2026-12-01', '2026-12-30') ON CONFLICT DO NOTHING"
            ),
            [{"t": t, "p": k < peak_first} for k, t in enumerate(targets)],
        )


def events(eng: Engine) -> list[tuple[str, str]]:
    with eng.connect() as conn:
        sql = "SELECT event_type, model_version FROM model_registry_events ORDER BY event_id"
        return [(str(r[0]), str(r[1])) for r in conn.execute(text(sql))]


@pytest.fixture(scope="module")
def env(fresh_db_url: str) -> Iterator[Any]:
    db.upgrade(fresh_db_url)
    eng = db.make_engine(fresh_db_url)
    with mock_aws(), pytest.MonkeyPatch.context() as mp:
        mp.delenv("HEALTHCHECKS_GATE_URL", raising=False)
        private, _ = make_buckets()
        yield eng, private
    eng.dispose()


def run(env: Any, now: datetime, action: str = "evaluate", apply: bool = True) -> dict[str, Any]:
    eng, store = env
    return gate.run(eng, store, action, apply, now=lambda: now, ping=lambda u: None)


def test_dry_run_and_hold_then_promote_with_queue_hand_over(env: Any) -> None:
    eng, store = env
    store.put_json(predict.CHAMPION_KEY, pointer("lgbm-champ", promoted_at="2026-10-24T01:18:02Z"))
    store.put_json(predict.SHADOW_KEY, pointer("lgbm-shadow", started_at=S.isoformat()))
    store.put_json(retrain.QUEUED_KEY, pointer("lgbm-queued"))
    seed(eng, S, 15 * 24 + 2, {"champion": ("lgbm-champ", 0.02), "shadow": ("lgbm-shadow", 0.01)},
         peak_first=40)  # fmt: skip

    young = run(env, S + timedelta(days=10))
    assert young["decision"] == "hold" and "too young" in young["reason"], young
    assert events(eng) == [] and store.exists(predict.SHADOW_KEY)

    now = S + timedelta(days=15, hours=3)
    dry = run(env, now, apply=False)
    assert dry["decision"] == "promote" and dry["mode"] == "dry-run" and "result" not in dry
    assert dry["stats"]["n_targets"] == 362 and dry["stats"]["n_peak"] == 40
    assert (
        events(eng) == [] and store.get_json(predict.CHAMPION_KEY)["model_version"] == "lgbm-champ"
    )
    with eng.connect() as conn:  # a dry run leaves no pipeline_runs row either
        assert conn.execute(text("SELECT count(*) FROM pipeline_runs")).scalar_one() == 1

    out = run(env, now)
    assert out["decision"] == "promote" and out["status"] == "ok", out
    champ = store.get_json(predict.CHAMPION_KEY)
    assert champ["model_version"] == "lgbm-shadow" and champ["previous_version"] == "lgbm-champ"
    assert champ["promoted_at"] == now.isoformat() and champ["champion_status"] == "ok"
    assert store.get_json(gate.PREV_KEY)["model_version"] == "lgbm-champ"
    # the queued candidate became the new shadow the same run
    assert store.get_json(predict.SHADOW_KEY)["model_version"] == "lgbm-queued"
    assert not store.exists(retrain.QUEUED_KEY)
    assert events(eng) == [("promoted", "lgbm-shadow"), ("shadow_started", "lgbm-queued")]


def test_automatic_rollback_then_cooldown_and_blocked_version(env: Any) -> None:
    eng, store = env
    promoted = datetime.fromisoformat(store.get_json(predict.CHAMPION_KEY)["promoted_at"])
    # 4 days after the promotion: the new champion 5 % off, prev_champion 2 % off
    seed(eng, promoted.replace(minute=0) + timedelta(hours=1), 4 * 24,
         {"champion": ("lgbm-shadow", 0.05), "prev_champion": ("lgbm-champ", 0.02)})  # fmt: skip
    now = promoted + timedelta(days=4, hours=2)
    out = run(env, now)
    assert out["decision"] == "rollback", out
    assert out["rollback_check"]["n_targets"] >= gate.ROLLBACK_MIN_TARGETS
    assert out["rollback_check"]["holiday"] is False
    champ = store.get_json(predict.CHAMPION_KEY)
    assert champ["model_version"] == "lgbm-champ" and champ["rolled_back_from"] == "lgbm-shadow"
    assert not store.exists(gate.PREV_KEY)
    assert events(eng)[-1] == ("rolled_back", "lgbm-shadow")

    # a shadow that passes is held during the 7-day cooldown ...
    store.put_json(predict.SHADOW_KEY, pointer("lgbm-good", started_at=S.isoformat()))
    with eng.begin() as conn:  # give lgbm-good the shadow rows lgbm-shadow had
        conn.execute(text("INSERT INTO model_versions (model_version) VALUES ('lgbm-good')"))
        conn.execute(text(
            "UPDATE predictions SET version_id = (SELECT version_id FROM model_versions WHERE"
            " model_version = 'lgbm-good') WHERE model_id = 2"))  # fmt: skip
    held = run(env, now + timedelta(days=1))
    assert held["decision"] == "hold" and "cooldown" in held["reason"], held
    # ... and the rolled-back version can never come back
    store.put_json(predict.SHADOW_KEY, pointer("lgbm-shadow", started_at=S.isoformat()))
    blocked = run(env, now + timedelta(days=1, hours=1))
    assert blocked["decision"] == "reject" and "blocked" in blocked["reason"]
    assert events(eng)[-1] == ("rejected", "lgbm-shadow") and not store.exists(predict.SHADOW_KEY)


def test_reject_on_mape_and_manual_rollback(env: Any) -> None:
    eng, store = env
    start = S + timedelta(days=40)
    store.put_json(predict.CHAMPION_KEY, pointer("lgbm-c2", promoted_at="2026-12-01T00:00:00Z",
                                                 previous_version="lgbm-c1"))  # fmt: skip
    store.put_json(gate.PREV_KEY, pointer("lgbm-c1"))
    store.put_json(predict.SHADOW_KEY, pointer("lgbm-bad", started_at=start.isoformat()))
    seed(eng, start, 15 * 24 + 2, {"champion": ("lgbm-c2", 0.02), "shadow": ("lgbm-bad", 0.03)},
         peak_first=40)  # fmt: skip
    out = run(env, start + timedelta(days=15, hours=3))
    assert out["decision"] == "reject" and "MAPE" in out["reason"]
    assert events(eng)[-1] == ("rejected", "lgbm-bad") and not store.exists(predict.SHADOW_KEY)
    assert store.get_json(predict.CHAMPION_KEY)["model_version"] == "lgbm-c2"  # untouched

    dry = run(env, start + timedelta(days=16), action="rollback", apply=False)
    assert dry["decision"] == "rollback" and "result" not in dry
    assert store.get_json(predict.CHAMPION_KEY)["model_version"] == "lgbm-c2"
    manual = run(env, start + timedelta(days=16), action="rollback")
    assert manual["result"] == {"rolled_back": "lgbm-c2", "restored": "lgbm-c1"}
    assert store.get_json(predict.CHAMPION_KEY)["model_version"] == "lgbm-c1"
    assert events(eng)[-1] == ("rolled_back", "lgbm-c2")
    with eng.connect() as conn:
        sql = (
            "SELECT triggered_by, metrics->>'blocked_version' FROM model_registry_events"
            " ORDER BY event_id DESC LIMIT 1"
        )
        trig = conn.execute(text(sql)).one()
    assert tuple(trig) == ("manual", "lgbm-c2")
    nothing = run(env, start + timedelta(days=16), action="rollback")
    assert nothing["status"] == "failed"  # no prev pointer left: nothing to roll back to
    assert np.isfinite(out["stats"]["mape_s"])
