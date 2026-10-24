# ADR-0012: Three hourly cron slots, success judged per issue hour

- Status: **Accepted**
- Date: 2026-10-24
- Related: BUILD_PLAN §2 (M4), §4.6, §6.1, §10 R1; ADR-0004 (as-of rule); DATA_NOTES §17;
  `.github/workflows/hourly.yml`, `src/gridcast/jobs/hourly.py`

## Context

`hourly.yml` ran at `7,27 * * * *`. First day of scheduled runs (2026-10-24, UTC):

| slot | what GitHub did |
|---|---|
| 18:07, 18:27 | never started (no run in `gh run list`) |
| 19:07 | started 19:24:09 (17 min late), `ok` at 19:25:04, 120 rows |
| 19:27 | not started by 19:43 |

GitHub's documented behaviour matches: scheduled runs are delayed under load and can be
dropped. Two questions follow: can a late run still publish an honest forecast, and does a late
or duplicate run raise a false `/fail` on Healthchecks?

What a run for issue hour H may use is fixed by `data_cutoff = H + 30 min` (ADR-0004). A run that
starts after the cutoff fetches a weather snapshot whose `available_at` (= fetch time) is after
the cutoff, so it cannot use it. It does not fail, though:
`weather_snapshots.qualifying` takes the newest snapshot with `available_at ≤ cutoff` that covers
H − 23 h … H + 24 h, and a snapshot of slot S covers S − 24 h … S + 47 h. Any snapshot fetched
before the cutoff and at most ~23 h old qualifies, so the previous slot's snapshot (≈ 1 h older)
is used. The forecast is honest, only its weather is ~1 h staler. The job fails (and pings
`/fail`) only when no snapshot from the last ~23 h exists. That happened once, on the first
manual run at 16:57Z, when no snapshot had ever been stored (DATA_NOTES §17.5).

A second slot for an hour that is already `ok` is `skipped` by the dedupe rule and pings success.
So late or duplicate runs were never a source of false `/fail` pings. What lateness costs is
**M4** (finished within 20 min of the issue hour) and weather freshness. Dropped slots cost
whole hours, which only the next hour's catch-up can fill (those hours count as missed for M4).

## Decision

1. **Cron `2,12,22 * * * *`.** Three chances per hour instead of two. The first is as early as
   the inputs allow: a complete RealtimeTotals hour lands at :53–:57 of the hour it covers, so the
   L0 hour is in at :02. With a 15–25 min delay the first slot lands at ~:17–:27, before the
   cutoff, so it uses its own fresh snapshot. The :22 slot (landing ~:37–:47) still picks up the
   ~:25 Adequacy3 reissue, and the next hour's :02 slot picks up the ~:55 one.
2. **Success is judged per issue hour.** Status `ok` means "issue hour H has its 24 champion
   rows", counted from `predictions`, whichever run wrote them. A later slot for an `ok` hour is
   `skipped` and pings success. A run that re-runs an hour whose champion rows already exist
   (because an earlier run failed after writing them, e.g. in publish) becomes `ok` without
   rewriting them (`ON CONFLICT DO NOTHING`). `/fail` is sent only when this run leaves H without
   24 champion rows, or publishing fails. Both are real faults.
3. **The cutoff is never moved.** A late run is honest, never better (plan §4.6). No slot is
   allowed to look at data stamped after `H + 30 min`.

Integration tests: `test_late_run_after_the_cutoff_uses_the_previous_snapshot_and_pings_success`
(start 18 min after the cutoff → `ok` from the previous snapshot, success ping; a duplicate slot →
`skipped`, success ping) and `test_no_qualifying_weather_fails_the_hour_but_still_writes_baselines`
(no snapshot in the last 23 h → `failed`, `/fail`).

## Consequences

- Load on the sources stays polite. A `skipped` run makes one Adequacy3 directory listing plus
  any new versions (normally 0–2 files). It does no RealtimeTotals or Open-Meteo call. Open-Meteo
  is called at most once per hour slot, as before. Net: +1 IESO listing per hour.
- `pipeline_runs` gains ~1 `skipped` and ~1 `ieso_adequacy` row per hour (≈ 17.5 k rows/year,
  ≈ 3 MB on Neon). This is inside the §14.4 budget line for hourly bookkeeping.
- GitHub keeps one pending run per concurrency group. If two slots queue behind a running one,
  the older pending run is cancelled. It shows as `cancelled` in Actions and sends no ping.
- M4 is measured from `pipeline_runs` as the plan defines it. If it stays below 98% after a week
  (Day 12 check), the number is reported honestly. The cutoff is not moved.

## Alternatives rejected

- **Moving the cutoff later (e.g. issue + 45 min)** would turn GitHub's delay into extra data.
  It would also change the as-of rule that training and backtests share, which is cheating.
- **More slots after :30.** They cannot make the current hour fresher. The next hour's catch-up
  already covers a dropped hour.
- **An external scheduler pinging `workflow_dispatch`.** Adds a credentialled service outside the
  repo for a gain in M4 only. Revisit if M4 is far below target after a week.
