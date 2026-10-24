"""Shared pieces for the job-level integration tests (hourly, daily, end-to-end).

Nothing here touches the network: IESO and Open-Meteo are fake callables, S3 is moto.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pandas as pd
from sqlalchemy import Engine, text

from gridcast.features.build import DemandHistory, FeatureBuilder, load_history, load_weather
from gridcast.ingest.http import Fetched
from gridcast.jobs import predict
from gridcast.models import lgbm as lgbm_mod
from gridcast.models.intervals import IntervalOffsets
from gridcast.storage.objstore import ObjectStore, ObjstoreSettings, make_client
from gridcast.timeutil import floor_hour
from gridcast.weather import WEATHER_COLUMNS
from tests.synthetic import weather_frame

MOTO_SETTINGS = ObjstoreSettings(
    _env_file=None,  # type: ignore[call-arg]
    region="us-east-1",
    access_key_id="test",
    secret_access_key="test",  # noqa: S106 - moto dummy
)


def make_buckets() -> tuple[ObjectStore, ObjectStore]:
    """(private, public) stores on moto; call inside `mock_aws()`."""
    client = make_client(MOTO_SETTINGS)
    for b in (MOTO_SETTINGS.bucket_private, MOTO_SETTINGS.bucket_public):
        client.create_bucket(Bucket=b)
    return (
        ObjectStore(client, MOTO_SETTINGS.bucket_private),
        ObjectStore(client, MOTO_SETTINGS.bucket_public),
    )


def install_champion(
    engine: Engine, store: ObjectStore, start: datetime, end: datetime, version: str = "lgbm-test"
) -> None:
    """A tiny LightGBM champion (lgbm_mod.MAX_ROUNDS should be patched small) + champion.json."""
    fb = FeatureBuilder(DemandHistory.from_raw(load_history(engine)), load_weather(engine))
    issues = pd.date_range(start + timedelta(days=2), end - timedelta(days=2), freq="h", tz=UTC)
    model = lgbm_mod.LgbmForecaster()
    for h in range(1, 25):
        fr = fb.frame(issues, [h])
        model.fit_horizon(h, fr, 15000.0 + fr["temperature_2m_w"].to_numpy() * 10)
    blob = model.to_bytes()
    offsets = IntervalOffsets({h: -0.05 for h in range(1, 25)}, {h: 0.05 for h in range(1, 25)})
    store.put_bytes(f"models/{version}/model.json.gz", blob)
    store.put_bytes(f"models/{version}/intervals.v0001.json", offsets.to_json().encode())
    store.put_json(
        predict.CHAMPION_KEY,
        {
            "model_name": "lgbm",
            "model_version": version,
            "artifact_key": f"models/{version}/model.json.gz",
            "sha256": hashlib.sha256(blob).hexdigest(),
            "intervals_key": f"models/{version}/intervals.v0001.json",
            "champion_status": "ok",
            "bootstrap_reason": "bootstrap",
            "feature_schema_version": 1,
        },
    )


def insert_ieso_snapshot(engine: Engine, created_at: datetime, first: datetime, hours: int) -> None:
    """One Adequacy3-like snapshot covering `hours` targets from `first`."""
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO ieso_forecast_snapshots (created_at, delivery_date, target_time,"
                " energy_mw, version, source_file) VALUES (:c, :d, :t, :mw, 1, 'synthetic.xml')"
                " ON CONFLICT DO NOTHING"
            ),
            [
                {
                    "c": created_at,
                    "d": (first + timedelta(hours=k) - timedelta(hours=5)).date(),
                    "t": first + timedelta(hours=k),
                    "mw": 15500.0 + k,
                }
                for k in range(hours)
            ],
        )


class Clock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


def ieso_fetch(pub_demand_body: Callable[[], str] | None = None) -> Callable[[str], Fetched]:
    """Fake reports-public: empty Adequacy3 listing, every RealtimeTotals hour 404 (an outage),
    PUB_Demand.csv from `pub_demand_body` when given (else 404)."""

    def fetch(url: str) -> Fetched:
        now = datetime.now(UTC)
        if url.endswith("/Adequacy3/"):
            return Fetched(url, 200, "<html></html>", now, None)
        if url.endswith("PUB_Demand.csv") and pub_demand_body is not None:
            return Fetched(url, 200, pub_demand_body(), now, None)
        return Fetched(url, 404, "", now, None)

    return fetch


def snapshot_fetcher(clock: Clock) -> Callable[[], tuple[pd.DataFrame, datetime]]:
    """Fake Open-Meteo live call: the synthetic weather for slot - 24 h .. slot + 47 h."""

    def fetch() -> tuple[pd.DataFrame, datetime]:
        slot = floor_hour(clock())
        frame = weather_frame(slot - timedelta(hours=24), slot + timedelta(hours=48))
        return frame[["valid_time", *WEATHER_COLUMNS]].copy(), clock()

    return fetch


def pub_demand_csv(values: pd.Series, created: str = "2024-01-26 07:30:09") -> str:
    """A PUB_Demand.csv body (3 junk lines + header) for Ontario values indexed by target_time."""
    from gridcast.timeutil import utc_to_he

    lines = ["\\\\Hourly Demand Report,,,", f"\\\\Created at {created},,,", "\\\\For 2024,,,"]
    lines.append("Date,Hour,Market Demand,Ontario Demand")
    for t, v in values.items():
        d, he = utc_to_he(pd.Timestamp(t).to_pydatetime())
        lines.append(f"{d:%Y-%m-%d},{he},{int(v) + 2000},{int(v)}")
    return "\n".join(lines) + "\n"


def champion_count(engine: Engine, issue: datetime) -> int:
    with engine.connect() as conn:
        return int(
            conn.execute(
                text("SELECT count(*) FROM predictions WHERE issue_time = :i AND model_id = 1"),
                {"i": issue},
            ).scalar_one()
        )
