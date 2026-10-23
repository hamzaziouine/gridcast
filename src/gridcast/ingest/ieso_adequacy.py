"""IESO Adequacy3 -> ieso_forecast_snapshots, every `_vN` version kept (C4).

Source facts (docs/DATA_NOTES.md §3, plan §3.0 decision 3):
- `PUB_Adequacy3_YYYYMMDD_vN.xml`, one series of versions per delivery date; IESO keeps ~3 months.
- Default namespace `http://www.ieso.ca/schema`. The benchmark is
  `DocBody/ForecastDemand/OntarioDemand/ForecastOntDemand/Demand/{DeliveryHour,EnergyMW}`.
- `DocHeader/CreatedAt` is fixed EST (UTC-5) all year, stored as UTC.
- Every version is a snapshot; the benchmark for an issue time is the latest snapshot created at
  or before its data cutoff, whatever its age.

Raw XML is archived gzipped, keyed `raw/ieso/adequacy3/YYYY/MM/DD/<file>.xml.gz` (DD = delivery
date), in the private bucket when an object store is passed, else under a local, git-ignored root
(`<archive_root>/ieso/adequacy3/...`). `scripts/copy_archive_fallback.py` moves a local archive to
the bucket.

Every file read is recorded in `ieso_adequacy_files` (migration 0003); the listing is compared
with that ledger, so a version whose rows were pruned is not downloaded again.

Retention (`prune_snapshots`, DATA_NOTES §15): for delivery date D keep every version created at or
after (D - 2 days) 00:00 EST, plus the newest version created before that. Issue times whose 24
targets touch D start at D-1 00:00 EST, so the benchmark (latest snapshot <= data_cutoff) is
always among the kept versions.
"""

from __future__ import annotations

import gzip
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import defusedxml.ElementTree as ET  # noqa: N817 - the conventional alias
import pandas as pd
from sqlalchemy import Engine, text

from gridcast import contracts
from gridcast.ingest.http import EmptyFeedError, Fetched, list_directory
from gridcast.storage.objstore import ObjectStore
from gridcast.timeutil import EST, he_to_utc

BASE_URL = "https://reports-public.ieso.ca/public/Adequacy3/"
NS = {"i": "http://www.ieso.ca/schema"}
DEFAULT_ARCHIVE_ROOT = Path("data/raw")
MAX_FILES_PER_RUN = 20  # plan §6.1 step 0

_NAME_RE = re.compile(r"^PUB_Adequacy3_(\d{8})_v(\d+)\.xml$")


@dataclass(frozen=True)
class Snapshot:
    created_at: datetime  # UTC
    delivery_date: date
    rows: pd.DataFrame  # target_time, energy_mw


def parse_file_name(name: str) -> tuple[date, int] | None:
    """`PUB_Adequacy3_20261024_v68.xml` -> (2026-10-24, 68); unversioned names -> None."""
    m = _NAME_RE.match(name)
    if not m:
        return None
    return datetime.strptime(m.group(1), "%Y%m%d").date(), int(m.group(2))


def parse_created_at(value: str) -> datetime:
    """`CreatedAt` is IESO market time, fixed EST all year (no DST) -> UTC."""
    local = datetime.fromisoformat(value.strip())
    if local.tzinfo is not None:
        raise ValueError(f"CreatedAt carries an offset, expected EST wall time: {value!r}")
    return local.replace(tzinfo=EST).astimezone(UTC)


def parse_adequacy(xml: str | bytes) -> Snapshot:
    root = ET.fromstring(xml)
    created = root.findtext("i:DocHeader/i:CreatedAt", namespaces=NS)
    delivery = root.findtext("i:DocBody/i:DeliveryDate", namespaces=NS)
    if not created or not delivery:
        raise ValueError("Adequacy3 file without CreatedAt or DeliveryDate")
    d = date.fromisoformat(delivery.strip())
    path = "i:DocBody/i:ForecastDemand/i:OntarioDemand/i:ForecastOntDemand/i:Demand"
    records = []
    for el in root.findall(path, NS):
        he = int(el.findtext("i:DeliveryHour", namespaces=NS) or 0)
        mw = el.findtext("i:EnergyMW", namespaces=NS)
        if mw is None or not mw.strip():
            continue
        records.append((he_to_utc(d, he), float(mw)))
    if not records:
        raise EmptyFeedError("Adequacy3 file has no ForecastOntDemand rows")
    rows = pd.DataFrame(records, columns=["target_time", "energy_mw"])
    rows["target_time"] = pd.to_datetime(rows["target_time"], utc=True)
    return Snapshot(parse_created_at(created), d, rows)


ARCHIVE_PREFIX = "raw/ieso/adequacy3/"
RETENTION_DAYS = 2


def archive_path(root: Path, file_name: str, delivery_date: date) -> Path:
    return root / "ieso" / "adequacy3" / f"{delivery_date:%Y/%m/%d}" / f"{file_name}.gz"


def archive_key(file_name: str, delivery_date: date) -> str:
    return f"{ARCHIVE_PREFIX}{delivery_date:%Y/%m/%d}/{file_name}.gz"


def pending_files(
    listing: Iterable[str], delivery_dates: Iterable[date], stored: set[str]
) -> list[str]:
    """Versioned files for the given delivery dates that are not stored yet, oldest first."""
    wanted = set(delivery_dates)
    found = []
    for name in listing:
        parsed = parse_file_name(name)
        if parsed and parsed[0] in wanted and name not in stored:
            found.append((parsed, name))
    return [name for _, name in sorted(found)]


def store_snapshot(
    engine: Engine,
    snap: Snapshot,
    file_name: str,
    fetched_at: datetime,
    archived_as: str | None = None,
) -> int:
    parsed = parse_file_name(file_name)
    if parsed is None:
        raise ValueError(f"not a versioned Adequacy3 file: {file_name!r}")
    params = [
        {
            "created_at": snap.created_at,
            "delivery_date": snap.delivery_date,
            "target_time": t.to_pydatetime(),
            "energy_mw": float(mw),
            "version": parsed[1],
            "source_file": file_name,
            "fetched_at": fetched_at,
        }
        for t, mw in zip(
            pd.DatetimeIndex(snap.rows["target_time"]), snap.rows["energy_mw"], strict=True
        )
    ]
    with engine.begin() as conn:
        result = conn.execute(
            text(
                "INSERT INTO ieso_forecast_snapshots (created_at, delivery_date, target_time,"
                " energy_mw, version, source_file, fetched_at) VALUES (:created_at,"
                " :delivery_date, :target_time, :energy_mw, :version, :source_file, :fetched_at)"
                " ON CONFLICT (created_at, target_time) DO NOTHING"
            ),
            params,
        )
        conn.execute(
            text(
                "INSERT INTO ieso_adequacy_files (source_file, delivery_date, version, created_at,"
                " fetched_at, archive_key) VALUES (:f, :d, :v, :c, :t, :k)"
                " ON CONFLICT (source_file) DO NOTHING"
            ),
            {
                "f": file_name,
                "d": snap.delivery_date,
                "v": parsed[1],
                "c": snap.created_at,
                "t": fetched_at,
                "k": archived_as,
            },
        )
    return int(result.rowcount or 0)


def ingest_recent(
    engine: Engine,
    fetch: Callable[[str], Fetched],
    today_est: date,
    days_ahead: int = 2,
    max_files: int = MAX_FILES_PER_RUN,
    archive_root: Path | None = DEFAULT_ARCHIVE_ROOT,
    store: ObjectStore | None = None,
) -> dict[str, int]:
    """Store every not-yet-read version for delivery dates today .. today + days_ahead (EST).

    The raw XML goes to `store` when given, else under `archive_root`. Returns counts: listed,
    pending, files, rows, pruned.
    """
    dates = [today_est + timedelta(days=k) for k in range(days_ahead + 1)]
    listing = fetch(BASE_URL)
    if listing.status != 200:
        raise RuntimeError(f"Adequacy3 listing returned HTTP {listing.status}")
    names = list_directory(listing.text)
    with engine.connect() as conn:
        stored = {
            r[0]
            for r in conn.execute(
                text(
                    "SELECT source_file FROM ieso_adequacy_files WHERE delivery_date = ANY(:dates)"
                ),
                {"dates": dates},
            )
        }
    todo = pending_files(names, dates, stored)
    counts = {"listed": len(names), "pending": len(todo), "files": 0, "rows": 0}
    newest: datetime | None = None
    last_fetch: datetime | None = None
    for name in todo[:max_files]:
        fetched = fetch(BASE_URL + name)
        if fetched.status != 200:
            continue  # listed but gone or not yet readable; the next slot retries
        snap = parse_adequacy(fetched.text.encode("utf-8"))
        contracts.validate_ieso_snapshot(snap.created_at, snap.rows, fetched.fetched_at)
        body = gzip.compress(fetched.text.encode("utf-8"), mtime=0)
        archived_as: str | None = None
        if store is not None:
            archived_as = archive_key(name, snap.delivery_date)
            store.put_bytes(archived_as, body, "application/gzip")
        elif archive_root is not None:
            path = archive_path(archive_root, name, snap.delivery_date)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(body)
        counts["rows"] += store_snapshot(engine, snap, name, fetched.fetched_at, archived_as)
        counts["files"] += 1
        newest = max(newest, snap.created_at) if newest else snap.created_at
        last_fetch = fetched.fetched_at
    if newest is not None and last_fetch is not None and len(todo) <= max_files:
        # All pending versions were read, so the newest one IESO lists is among them.
        contracts.check_newest_snapshot(newest, last_fetch)
    counts["pruned"] = prune_snapshots(engine)["rows_deleted"]
    return counts


# Cutoff = (delivery_date - RETENTION_DAYS) 00:00 EST, as UTC.
_PRUNE_SQL = """
WITH cut AS (
  SELECT delivery_date,
         ((delivery_date - CAST(:days AS integer))::timestamp + interval '5 hours')
           AT TIME ZONE 'UTC' AS cutoff
  FROM (SELECT DISTINCT delivery_date FROM ieso_forecast_snapshots) d
), newest_old AS (
  SELECT s.delivery_date, max(s.created_at) AS created_at
  FROM ieso_forecast_snapshots s JOIN cut USING (delivery_date)
  WHERE s.created_at < cut.cutoff
  GROUP BY s.delivery_date
), gone AS (
  DELETE FROM ieso_forecast_snapshots s
  USING newest_old k
  WHERE s.delivery_date = k.delivery_date AND s.created_at < k.created_at
  RETURNING s.source_file
)
SELECT source_file, count(*) FROM gone GROUP BY source_file
"""


def prune_snapshots(engine: Engine) -> dict[str, int]:
    """Apply the retention rule (module docstring); mark pruned files in the ledger."""
    with engine.begin() as conn:
        gone = conn.execute(text(_PRUNE_SQL), {"days": RETENTION_DAYS}).all()
        files = [r[0] for r in gone]
        if files:
            conn.execute(
                text(
                    "UPDATE ieso_adequacy_files SET rows_kept = false"
                    " WHERE source_file = ANY(:files)"
                ),
                {"files": files},
            )
    return {"files_pruned": len(files), "rows_deleted": int(sum(r[1] for r in gone))}
