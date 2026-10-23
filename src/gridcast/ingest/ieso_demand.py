"""IESO PUB_Demand: parse, append versioned rows to raw_demand_history, rebuild actuals.

Source facts (docs/DATA_NOTES.md §2, BUILD_PLAN §3.0/§3.1):
- `PUB_Demand.csv` (current year) and `PUB_Demand_YYYY.csv` (archive); each revision is also posted
  as `PUB_Demand_YYYY_vNNN.csv`, and the unsuffixed names are the latest version.
- 3 junk lines starting with `\\`, then `Date,Hour,Market Demand,Ontario Demand`.
- The target is Ontario Demand. Market Demand is stored too (it is the Market Demand feature series
  that RealtimeTotals supplies live, plan §3.0 decision 1).
- The daily 07:30 EST file carries yesterday HE24 plus today's provisional HE1, which the next
  file revises.

Versioning (BUILD_PLAN §4.3): raw_demand_history is append-only. A row is written only when the
(target_time, source) value differs from the latest stored version. Availability stamps:
- `observed` mode (live daily job): available_at = our fetch time.
- `modelled` mode (backfill): the FIRST version of an hour gets the ADR-0004 rule
  (`pub_demand_available_at`, D+1 13:00Z). Hours whose modelled stamp lies after our fetch time
  (today's provisional HE1) are not stored: the model says they are not published yet, and the
  next run picks up their final value.
- In both modes a REVISION of an already-stored hour is stamped `observed` at our fetch time: we do
  not know when IESO changed it, only that it was changed by then. An earlier version's stamp is
  never rewritten.

Note for the feature builder (C6): a row's `available_at` describes its Ontario Demand. The Market
Demand column of a PUB row is available earlier, under `availability.market_demand_available_at`
(plan §3.0 decision 1); apply that rule to the column instead of the row stamp.
"""

from __future__ import annotations

import csv
import io
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Literal

import pandas as pd
from sqlalchemy import Connection, Engine, text

from gridcast import contracts
from gridcast.ingest.availability import pub_demand_available_at
from gridcast.ingest.http import EmptyFeedError, Fetched, list_directory
from gridcast.timeutil import EST, he_to_utc

BASE_URL = "https://reports-public.ieso.ca/public/Demand/"
SOURCE = "pub_demand"
HEADER = ["Date", "Hour", "Market Demand", "Ontario Demand"]

Mode = Literal["observed", "modelled"]

_NAME_RE = re.compile(r"^PUB_Demand(?:_(\d{4}))?(?:_v(\d+))?\.csv$")
_CREATED_RE = re.compile(r"Created at (\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")

# Column order of the staging COPY; shared with ieso_realtime.
RAW_COLUMNS = (
    "target_time",
    "source",
    "file_name",
    "file_version",
    "local_date",
    "hour_ending",
    "market_demand_mw",
    "ontario_demand_mw",
    "n_intervals",
    "available_at",
    "available_at_kind",
    "fetched_at",
    "http_last_modified",
)


@dataclass(frozen=True)
class IngestResult:
    file_name: str
    rows_parsed: int
    rows_inserted: int  # new raw_demand_history rows (first versions + revisions)
    actuals_changed: int  # actuals rows inserted or revised


def parse_file_name(name: str) -> tuple[int | None, str | None]:
    """`PUB_Demand_2026_v295.csv` -> (2026, 'v295'); `PUB_Demand.csv` -> (None, None)."""
    m = _NAME_RE.match(name)
    if not m:
        raise ValueError(f"not a PUB_Demand file name: {name!r}")
    year = int(m.group(1)) if m.group(1) else None
    version = f"v{int(m.group(2)):03d}" if m.group(2) else None
    return year, version


def latest_files_by_year(listing: Iterable[str]) -> dict[int, str]:
    """Newest file per year from a directory listing: the highest `_vNNN`, else the plain name."""
    best: dict[int, tuple[int, str]] = {}
    for name in listing:
        try:
            year, version = parse_file_name(name)
        except ValueError:
            continue
        if year is None:
            continue
        rank = int(version[1:]) if version else -1
        if year not in best or rank > best[year][0]:
            best[year] = (rank, name)
    return {y: n for y, (_, n) in sorted(best.items())}


def parse_created_at(body: str) -> datetime | None:
    """The `\\Created at` header line (EST wall time) as a UTC instant, if present."""
    m = _CREATED_RE.search(body[:500])
    if not m:
        return None
    local = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=EST)
    return local.astimezone(UTC)


def parse_pub_demand(body: str) -> pd.DataFrame:
    """Parse a PUB_Demand CSV body.

    Returns one row per (Date, Hour) with columns local_date, hour_ending, market_demand_mw,
    ontario_demand_mw, target_time (UTC interval start). Junk `\\` lines before the header are
    skipped whatever their number; gaps (e.g. 2025-05-01 HE1) are kept as gaps.
    """
    lines = body.splitlines()
    try:
        start = next(i for i, ln in enumerate(lines) if ln.startswith("Date,Hour"))
    except StopIteration as exc:
        raise EmptyFeedError("PUB_Demand header line not found") from exc
    reader = csv.reader(io.StringIO("\n".join(lines[start:])))
    header = next(reader)
    if [h.strip() for h in header[:4]] != HEADER:
        raise ValueError(f"unexpected PUB_Demand header: {header!r}")
    records = []
    for row in reader:
        if not row or not row[0].strip():
            continue
        d = date.fromisoformat(row[0].strip())
        he = int(row[1])
        records.append(
            {
                "local_date": d,
                "hour_ending": he,
                "market_demand_mw": float(row[2]),
                "ontario_demand_mw": float(row[3]),
                "target_time": he_to_utc(d, he),
            }
        )
    if not records:
        raise EmptyFeedError("PUB_Demand file has 0 data rows")
    df = pd.DataFrame.from_records(records)
    if df.duplicated(["local_date", "hour_ending"]).any():
        raise ValueError("duplicate (Date, Hour) rows in PUB_Demand file")
    df["target_time"] = pd.to_datetime(df["target_time"], utc=True)
    return df


def to_raw_rows(
    parsed: pd.DataFrame,
    *,
    file_name: str,
    fetched_at: datetime,
    mode: Mode,
    http_last_modified: datetime | None = None,
) -> pd.DataFrame:
    """Add the raw_demand_history columns and availability stamps to a parsed file."""
    _, version = parse_file_name(file_name)
    df = parsed.copy()
    if mode == "modelled":
        df["available_at"] = pd.to_datetime(
            [pub_demand_available_at(t.to_pydatetime()) for t in df["target_time"]], utc=True
        )
        # Not yet published under the model (today's provisional HE1): do not store.
        df = df[df["available_at"] <= pd.Timestamp(fetched_at)].copy()
    else:
        df["available_at"] = pd.Timestamp(fetched_at)
    df["available_at_kind"] = mode
    df["source"] = SOURCE
    df["file_name"] = file_name
    df["file_version"] = version
    df["n_intervals"] = None
    df["fetched_at"] = pd.Timestamp(fetched_at)
    df["http_last_modified"] = (
        pd.Timestamp(http_last_modified) if http_last_modified is not None else pd.NaT
    )
    return df[list(RAW_COLUMNS)].reset_index(drop=True)


def _none_if_na(v: object) -> object:
    if v is None or v is pd.NaT:
        return None
    if isinstance(v, float) and v != v:  # NaN
        return None
    if isinstance(v, pd.Timestamp):
        return v.to_pydatetime()
    return v


def append_raw_demand(conn: Connection, rows: pd.DataFrame) -> list[datetime]:
    """Append rows to raw_demand_history where the value changed; return their target_times.

    Staged through a temp table and COPY (fast over Neon). A row is inserted when its
    (target_time, source) has no stored version or the latest stored version (by available_at)
    differs in Market or Ontario Demand. A revision is stamped `observed` at its fetched_at.
    """
    if rows.empty:
        return []
    conn.execute(
        text(
            "CREATE TEMP TABLE stage_raw_demand (LIKE raw_demand_history INCLUDING DEFAULTS)"
            " ON COMMIT DROP"
        )
    )
    raw = conn.connection.driver_connection
    if raw is None:  # pragma: no cover - SQLAlchemy always has one inside a transaction
        raise RuntimeError("no DBAPI connection")
    cols = ", ".join(RAW_COLUMNS)
    with raw.cursor() as cur, cur.copy(f"COPY stage_raw_demand ({cols}) FROM STDIN") as cp:
        for rec in rows.itertuples(index=False, name=None):
            cp.write_row([_none_if_na(v) for v in rec])
    # Column names are the RAW_COLUMNS constant; values travel as COPY data.
    inserted = conn.execute(
        text(
            f"""
            INSERT INTO raw_demand_history ({cols})
            SELECT s.target_time, s.source, s.file_name, s.file_version, s.local_date,
                   s.hour_ending, s.market_demand_mw, s.ontario_demand_mw, s.n_intervals,
                   CASE WHEN cur.target_time IS NULL THEN s.available_at ELSE s.fetched_at END,
                   CASE WHEN cur.target_time IS NULL THEN s.available_at_kind
                        ELSE 'observed' END,
                   s.fetched_at, s.http_last_modified
            FROM stage_raw_demand s
            LEFT JOIN LATERAL (
                SELECT r.target_time, r.market_demand_mw, r.ontario_demand_mw
                FROM raw_demand_history r
                WHERE r.target_time = s.target_time AND r.source = s.source
                ORDER BY r.available_at DESC, r.ingested_at DESC
                LIMIT 1
            ) cur ON true
            WHERE cur.target_time IS NULL
               OR cur.market_demand_mw IS DISTINCT FROM s.market_demand_mw
               OR cur.ontario_demand_mw IS DISTINCT FROM s.ontario_demand_mw
            ON CONFLICT (target_time, source, file_name) DO NOTHING
            RETURNING target_time
            """  # noqa: S608
        )
    )
    return [r[0] for r in inserted]


def rebuild_actuals(conn: Connection, target_times: Sequence[datetime]) -> int:
    """Upsert actuals from the latest pub_demand version of each hour; bump revision on change."""
    if not target_times:
        return 0
    result = conn.execute(
        text(
            """
            INSERT INTO actuals (target_time, ontario_demand_mw, source, revision,
                                 available_at, available_at_kind)
            SELECT DISTINCT ON (target_time) target_time, ontario_demand_mw, 'pub_demand', 1,
                   available_at, available_at_kind
            FROM raw_demand_history
            WHERE source = 'pub_demand' AND target_time = ANY(:tts)
            ORDER BY target_time, available_at DESC, ingested_at DESC
            ON CONFLICT (target_time) DO UPDATE
              SET ontario_demand_mw = EXCLUDED.ontario_demand_mw,
                  revision = actuals.revision + 1,
                  available_at = EXCLUDED.available_at,
                  available_at_kind = EXCLUDED.available_at_kind,
                  updated_at = now()
              WHERE actuals.ontario_demand_mw IS DISTINCT FROM EXCLUDED.ontario_demand_mw
            """
        ),
        {"tts": list(target_times)},
    )
    return int(result.rowcount or 0)


def ingest_pub_demand(engine: Engine, fetched: Fetched, mode: Mode) -> IngestResult:
    """Parse one fetched PUB_Demand file and apply it in one transaction."""
    parsed = parse_pub_demand(fetched.text)
    rows = to_raw_rows(
        parsed,
        file_name=fetched.name,
        fetched_at=fetched.fetched_at,
        mode=mode,
        http_last_modified=fetched.last_modified,
    )
    contracts.RawDemand.validate(rows)
    with engine.begin() as conn:
        changed = append_raw_demand(conn, rows)
        n_actuals = rebuild_actuals(conn, changed)
    return IngestResult(fetched.name, len(parsed), len(changed), n_actuals)


def backfill(
    engine: Engine,
    years: Iterable[int],
    fetch: Callable[[str], Fetched],
    log: Callable[[str], None] = print,
) -> list[IngestResult]:
    """Load the newest version of each year's file with modelled stamps (idempotent)."""
    listing = fetch(BASE_URL)
    if listing.status != 200:
        raise RuntimeError(f"PUB_Demand listing returned HTTP {listing.status}")
    files = latest_files_by_year(list_directory(listing.text))
    results = []
    for year in years:
        if year not in files:
            raise RuntimeError(f"no PUB_Demand file listed for {year}")
        fetched = fetch(BASE_URL + files[year])
        if fetched.status != 200:
            raise RuntimeError(f"{files[year]} returned HTTP {fetched.status}")
        res = ingest_pub_demand(engine, fetched, "modelled")
        log(
            f"{res.file_name}: parsed={res.rows_parsed} inserted={res.rows_inserted}"
            f" actuals_changed={res.actuals_changed}"
        )
        results.append(res)
    return results


def week_ratio_outliers(actuals: pd.Series) -> pd.Series:
    """The §3.1 damaged-hour check: value < 50 % or > 150 % of the same hour one week earlier
    AND one week later (one-sided where the later week does not exist yet).

    `actuals` is indexed by target_time (UTC, hourly). Returns the flagged subset. Per plan §3.0
    decision 4 this is a diagnostic on PUB_Demand (it flags 0 hours there); the filter proper is
    applied to zonal data only.
    """
    s = actuals.sort_index().astype("float64")
    idx = pd.DatetimeIndex(s.index)
    week = pd.Timedelta(days=7)
    before = pd.Series(s.reindex(idx - week).to_numpy(), index=idx)
    after = pd.Series(s.reindex(idx + week).to_numpy(), index=idx)
    v = pd.Series(s.to_numpy(), index=idx)
    bad_before = (v < 0.5 * before) | (v > 1.5 * before)
    bad_after = (v < 0.5 * after) | (v > 1.5 * after)
    flagged = bad_before & (bad_after | after.isna())
    return s[flagged.to_numpy()]
