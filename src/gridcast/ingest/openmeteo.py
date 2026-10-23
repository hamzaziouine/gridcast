"""Open-Meteo weather: Historical Forecast API backfill and the live forecast snapshot (C3).

- Model pinned to `gfs_seamless` in both APIs (plan §3.0 decision 5; `best_match` resolves to it at
  Toronto today, pinning prevents a silent train/serve switch).
- 6 cities with fixed weights (BUILD_PLAN §4.5), 9 hourly variables, all times UTC.
- Storage layout (migration 0002, DATA_NOTES §9): one `weather_hourly` row per hour, one `real`
  column per (variable, city), named `<variable>_<city key>`.
- Historical rows carry the SYNTHETIC modelled stamp `valid_time - 24 h` (ADR-0004).
- Rate limits (free, non-commercial): 600 calls/min, 5,000/h, 10,000/day, where a request counts
  `locations x max(1, variables/10) x max(1, days/14)` calls. The backfill paces itself to half of
  each limit, so the hourly live job always has headroom.
"""

from __future__ import annotations

import json
import time
from collections import deque
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pandas as pd
from sqlalchemy import Connection, Engine, text

from gridcast.ingest.availability import historical_forecast_available_at
from gridcast.ingest.http import Fetched

HISTORICAL_URL = "https://historical-forecast-api.open-meteo.com/v1/forecast"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
MODEL = "gfs_seamless"
HISTORY_START = date(2018, 1, 1)  # first date with data for gfs_seamless at Toronto (DATA_NOTES §5)
LIVE_PAST_HOURS = 24
LIVE_FORECAST_HOURS = 48


@dataclass(frozen=True)
class City:
    location_id: int
    name: str
    key: str
    lat: float
    lon: float
    weight: float


# Must match the weather_locations seed in alembic 0002 (a test checks it).
CITIES: tuple[City, ...] = (
    City(1, "Toronto", "toronto", 43.65, -79.38, 0.45),
    City(2, "Ottawa", "ottawa", 45.42, -75.70, 0.15),
    City(3, "London", "london", 42.98, -81.25, 0.10),
    City(4, "Windsor", "windsor", 42.30, -83.02, 0.08),
    City(5, "Sudbury", "sudbury", 46.49, -80.99, 0.07),
    City(6, "Thunder Bay", "thunder_bay", 48.38, -89.25, 0.05),
)
VARIABLES: tuple[str, ...] = (
    "temperature_2m",
    "apparent_temperature",
    "relative_humidity_2m",
    "dew_point_2m",
    "wind_speed_10m",
    "cloud_cover",
    "shortwave_radiation",
    "precipitation",
    "snowfall",
)
WEATHER_COLUMNS: tuple[str, ...] = tuple(f"{v}_{c.key}" for c in CITIES for v in VARIABLES)
ROW_COLUMNS: tuple[str, ...] = (
    "valid_time",
    "source",
    "available_at",
    "available_at_kind",
    "fetched_at",
    *WEATHER_COLUMNS,
)

Fetch = Callable[[str, dict[str, str]], Fetched]


def _base_params() -> dict[str, str]:
    return {
        "latitude": ",".join(f"{c.lat}" for c in CITIES),
        "longitude": ",".join(f"{c.lon}" for c in CITIES),
        "hourly": ",".join(VARIABLES),
        "models": MODEL,
        "timezone": "GMT",
    }


def request_weight(n_locations: int, n_variables: int, n_days: float) -> float:
    """Open-Meteo's fractional call count for one request."""
    return n_locations * max(1.0, n_variables / 10) * max(1.0, n_days / 14)


def parse_response(payload: Any) -> pd.DataFrame:
    """Open-Meteo JSON (one object per city, in CITIES order) -> wide frame.

    Columns: valid_time (UTC) + WEATHER_COLUMNS. Missing values stay NaN.
    """
    items = payload if isinstance(payload, list) else [payload]
    if len(items) != len(CITIES):
        raise ValueError(f"expected {len(CITIES)} locations, got {len(items)}")
    frame: pd.DataFrame | None = None
    for city, item in zip(CITIES, items, strict=True):
        if abs(item["latitude"] - city.lat) > 0.2 or abs(item["longitude"] - city.lon) > 0.2:
            raise ValueError(f"response order mismatch at {city.name}: {item['latitude']}")
        hourly = item["hourly"]
        missing = [v for v in VARIABLES if v not in hourly]
        if missing:
            raise ValueError(f"{city.name}: variables missing from response: {missing}")
        part = pd.DataFrame({f"{v}_{city.key}": hourly[v] for v in VARIABLES}, dtype="float64")
        part.insert(0, "valid_time", pd.to_datetime(hourly["time"], utc=True))
        frame = part if frame is None else frame.merge(part, on="valid_time", how="outer")
    if frame is None:  # pragma: no cover - CITIES is never empty
        raise ValueError("empty response")
    return frame.sort_values("valid_time").reset_index(drop=True)


def to_rows(frame: pd.DataFrame, fetched_at: datetime) -> pd.DataFrame:
    """Historical-forecast rows with the synthetic modelled availability (valid_time - 24 h)."""
    df = frame.copy()
    df["source"] = "historical_forecast"
    df["available_at"] = pd.to_datetime(
        [historical_forecast_available_at(t.to_pydatetime()) for t in df["valid_time"]], utc=True
    )
    df["available_at_kind"] = "modelled"
    df["fetched_at"] = pd.Timestamp(fetched_at)
    return df[list(ROW_COLUMNS)]


def upsert_weather(conn: Connection, rows: pd.DataFrame) -> int:
    """Insert new hours; rewrite a stored historical hour only if a value changed. Returns count."""
    if rows.empty:
        return 0
    cols = ", ".join(ROW_COLUMNS)
    conn.execute(
        text(
            "CREATE TEMP TABLE stage_weather (LIKE weather_hourly INCLUDING DEFAULTS)"
            " ON COMMIT DROP"
        )
    )
    raw = conn.connection.driver_connection
    if raw is None:  # pragma: no cover
        raise RuntimeError("no DBAPI connection")
    with raw.cursor() as cur, cur.copy(f"COPY stage_weather ({cols}) FROM STDIN") as cp:
        for rec in rows.itertuples(index=False, name=None):
            cp.write_row(
                [
                    None
                    if (isinstance(v, float) and v != v)
                    else (v.to_pydatetime() if isinstance(v, pd.Timestamp) else v)
                    for v in rec
                ]
            )
    sets = ", ".join(f"{c} = EXCLUDED.{c}" for c in ROW_COLUMNS[1:])
    changed = " OR ".join(f"w.{c} IS DISTINCT FROM EXCLUDED.{c}" for c in WEATHER_COLUMNS)
    result = conn.execute(
        text(
            # Column names are the ROW_COLUMNS constant; values travel as COPY data.
            f"INSERT INTO weather_hourly AS w ({cols}) SELECT {cols} FROM stage_weather"  # noqa: S608
            f" ON CONFLICT (valid_time) DO UPDATE SET {sets}"
            f" WHERE w.source = 'historical_forecast' AND ({changed})"
        )
    )
    return int(result.rowcount or 0)


def month_ranges(start: date, end: date) -> Iterator[tuple[date, date]]:
    """Calendar-month chunks [first, last] covering start..end inclusive."""
    cur = start
    while cur <= end:
        nxt = (cur.replace(day=1) + timedelta(days=32)).replace(day=1)
        yield cur, min(nxt - timedelta(days=1), end)
        cur = nxt


def stored_hours(conn: Connection, first: date, last: date) -> int:
    lo = datetime.combine(first, datetime.min.time(), tzinfo=UTC)
    hi = datetime.combine(last + timedelta(days=1), datetime.min.time(), tzinfo=UTC)
    return int(
        conn.execute(
            text(
                "SELECT count(*) FROM weather_hourly WHERE valid_time >= :lo AND valid_time < :hi"
            ),
            {"lo": lo, "hi": hi},
        ).scalar_one()
    )


class RateLimiter:
    """Keeps weighted calls under fixed shares of Open-Meteo's minute/hour/day limits."""

    def __init__(
        self,
        budgets: Sequence[tuple[float, float]] = ((60, 300), (3600, 2500), (86400, 5000)),
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self.budgets = budgets
        self.clock = clock
        self.sleep = sleep
        self.calls: deque[tuple[float, float]] = deque()

    def acquire(self, weight: float) -> None:
        while True:
            now = self.clock()
            horizon = max(w for w, _ in self.budgets)
            while self.calls and self.calls[0][0] <= now - horizon:
                self.calls.popleft()
            wait = 0.0
            for window, budget in self.budgets:
                in_window = [(t, w) for t, w in self.calls if t > now - window]
                if sum(w for _, w in in_window) + weight > budget and in_window:
                    wait = max(wait, in_window[0][0] + window - now)
            if wait <= 0:
                self.calls.append((now, weight))
                return
            self.sleep(wait)


def backfill(
    engine: Engine,
    fetch: Fetch,
    start: date = HISTORY_START,
    end: date | None = None,
    limiter: RateLimiter | None = None,
    log: Callable[[str], None] = print,
) -> int:
    """Fetch month by month; skip months already complete in the database (resumable)."""
    end = end or (datetime.now(UTC).date() - timedelta(days=1))
    limiter = limiter or RateLimiter()
    total = 0
    for first, last in month_ranges(start, end):
        expected = ((last - first).days + 1) * 24
        with engine.connect() as conn:
            have = stored_hours(conn, first, last)
        if have >= expected:
            continue
        n_days = (last - first).days + 1
        limiter.acquire(request_weight(len(CITIES), len(VARIABLES), n_days))
        params = _base_params() | {"start_date": first.isoformat(), "end_date": last.isoformat()}
        fetched = fetch(HISTORICAL_URL, params)
        if fetched.status != 200:
            raise RuntimeError(f"historical forecast {first:%Y-%m}: HTTP {fetched.status}")
        frame = parse_response(json.loads(fetched.text))
        rows = to_rows(frame, fetched.fetched_at)
        with engine.begin() as conn:
            n = upsert_weather(conn, rows)
        total += n
        log(f"{first:%Y-%m}: hours={len(frame)} written={n}")
    return total


def fetch_live(fetch: Fetch) -> tuple[pd.DataFrame, datetime]:
    """The live Forecast API snapshot (-24 h / +48 h, 6 cities, gfs_seamless) and its fetch time."""
    params = _base_params() | {
        "past_hours": str(LIVE_PAST_HOURS),
        "forecast_hours": str(LIVE_FORECAST_HOURS),
    }
    fetched = fetch(FORECAST_URL, params)
    if fetched.status != 200:
        raise RuntimeError(f"live forecast: HTTP {fetched.status}")
    return parse_response(json.loads(fetched.text)), fetched.fetched_at
