# ADR-0002: Fixed UTC−5 time model for IESO data

- Status: Accepted
- Date: 2026-10-23
- Related: BUILD_PLAN §3.3, §3.5; `docs/DATA_NOTES.md` §2–§3

## Context

IESO publishes everything in **EST (UTC−5) all year, with no daylight-saving shift**, using
**hour-ending** labels 1–24 (HE1 = 00:00–01:00 EST). P0.5 confirmed this first-hand:

- `PUB_Demand` `Created at 07:30:09` against `Last-Modified 12:32:17Z`;
- Adequacy3 `CreatedAt` against HTTP `Last-Modified`, both in October (+3 min 44 s) and in **July**
  (+41 s). The July file was created during Toronto daylight time, and still no DST shift appears;
- every DST date has exactly 24 rows.

Parsing these timestamps with `America/Toronto` would produce two classes of silent bug:
- a 23-hour or 25-hour day around each DST change;
- a one-hour shift of every summer timestamp.

The second one would also corrupt the as-of rule (ADR-0004).

Calendar facts are different. Holidays, weekday and long weekends follow the *civil* clock that
people live by. During EDT, civil 00:00–01:00 of a holiday is HE24 of the *previous* EST date.

## Decision

- All storage and computation use **UTC timestamps**. Intervals are labelled by their start
  (`target_time`).
- IESO `(date D, hour-ending h)` maps to `target_time = D 00:00 + (h − 1) h + 5 h` UTC. So HE1 maps
  to `D 05:00Z`, and HE24 maps to `D+1 04:00Z`.
- IESO wall-clock fields (`Created at`, `CreatedAt`, `CREATED AT`) are parsed as fixed UTC−5, using
  `timezone(timedelta(hours=-5))` or `Etc/GMT+5` (POSIX sign inverted), then converted to UTC.
- **`America/Toronto` is banned at the data layer.** A tripwire test greps for it. The single
  permitted use is `timeutil.civil(ts_utc) -> (civil_date, civil_hour)`. That function feeds
  `civil_hour` and every calendar flag: holidays (`holidays.country_holidays("CA", subdiv="ON")`),
  `dow`, `is_weekend`, long-weekend, bridge and Christmas-period flags.
- Tests pin the DST dates 2025-03-09, 2025-11-02, 2026-03-08 and 2026-11-01 to exactly 24 rows,
  plus a winter and a July `CreatedAt` conversion.

## Consequences

- There is one unambiguous key per hour, with no duplicate or missing hours at DST changes.
  Joins with Open-Meteo (requested in GMT) are exact.
- Displaying local time is a presentation concern only (API/site), done at the edge.
- Contributors must learn the hour-ending convention and the `Etc/GMT+5` sign inversion. The
  `timeutil` module and its tests are the single source of truth.
- Calendar features can disagree with the EST date for the 00:00–01:00 civil hour in summer. That
  disagreement is intended, because it is what makes them correct.

## Alternatives rejected

- **`America/Toronto` everywhere.** Wrong for IESO data (DST shifts, 23/25-hour days); see Context.
- **Store naive EST timestamps.** It works until anything (weather, cron, the API) is in UTC, and
  then every join needs a hand-written offset.
- **Hour-beginning labels taken directly from IESO's hour number.** This is off by one hour
  against IESO's hour-ending convention, an easy and invisible error.
