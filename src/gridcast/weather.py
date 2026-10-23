"""The weather station set and variables GridCast uses (BUILD_PLAN §4.5), in one place.

Column names of the wide weather frame are `<variable>_<city key>` (migration 0002).
"""

from __future__ import annotations

from dataclasses import dataclass


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
