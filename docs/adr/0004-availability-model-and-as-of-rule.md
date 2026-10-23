# ADR-0004: Availability model and as-of rule (DRAFT)

- Status: **Draft**. It is finalised in C20 once the full-day RealtimeTotals observation and the
  RealtimeTotals derivation decision are in.
- Date: 2026-10-23
- Related: BUILD_PLAN §3.1, §3.2, §3.4, §4.6, §6.1 step 2; ADR-0002; `docs/DATA_NOTES.md` §0, §6

## Context

A forecast issued at hour T may only use data that really existed at T. Backtests and training
must obey the same rule, or the offline metrics are fiction.

Two difficulties:

1. **Backfilled history has no record of when each value was published.** Only live ingests
   observe that.
2. **The sources lag differently.** P0.5 measured:
   - `PUB_Demand`: posted once a day at ~07:30 EST (12:30–12:33Z). It carries yesterday HE1–24 plus
     today's provisional HE1.
   - RealtimeTotals: re-posted every 5 min. A complete hour is posted 53–57 min after its start.
     It carries Market Demand, **not Ontario Demand** (DATA_NOTES Contradiction 1).
   - Adequacy3: reissued with `CreatedAt` in fixed EST.
   - Open-Meteo Historical Forecast: an archive that stitches together the *shortest available
     lead* per hour.

GitHub cron also starts late (ADR-0001), and the live weather snapshot is fetched *after* the top
of the hour.

## Decision

**One cutoff for every source.**
- `issue_time = floor(now, 1 h)`.
- `data_cutoff = issue_time + 30 min`.
- Every source is filtered with `available_at ≤ data_cutoff`. Training, backtests, live inference
  and catch-up all use this rule.
- A run finishing after the cutoff is allowed, but it just ignores late data. Delays make a run
  *honest*, never *better*.

**Every raw row carries `available_at` and an `available_at_kind`.**
- `observed` = our fetch time (live ingest).
- `modelled` = a documented rule (backfill). The rules live in `ingest/availability.py`, one
  function per source, and each is unit-tested:

| Source | Modelled `available_at` |
|---|---|
| `pub_demand` | `D+1 13:00Z` for every hour of EST date D. This is 27–30 min after the observed posting. |
| `realtime_totals` | `target_time + REALTIME_LAG_H`. **Provisional `REALTIME_LAG_H = 1`**, because the complete file was observed at target + 53–57 min. This is confirmed or raised to 2 after the full-day observation. It only applies if a provisional source is kept (see Open items). |
| weather, historical forecast | `valid_time − 24 h`. This is **synthetic and optimistic**, because the archive uses the shortest lead. R11 tracks the risk, and S4 measures the gap. |
| weather, live snapshot | `fetched_at` (observed). Every live snapshot is archived, so that a true as-of weather set accumulates for a later retrain. |
| `ieso_adequacy` | `CreatedAt` parsed as EST and converted to UTC (observed; ADR-0002). |

**Demand features read `raw_demand_history`.**
- For each `target_time`, take the value with the latest qualifying `available_at`.
  `pub_demand` wins when both sources qualify.
- `L0` and `L0_age_hours` therefore have the same lag distribution in training and live.
  A unit test asserts this parity, and a daily `l0_lag_parity` drift metric watches it once the
  system is live.

**Benchmark fairness.**
- The IESO comparison for issue T uses the latest Adequacy3 version created at or before
  `data_cutoff`, never a later reissue.
- `ieso_staleness = data_cutoff − created_at` is stored per row.

**Catch-up policy.**
- Each hourly run re-runs up to the last 6 issue hours that have no `champion` row, oldest first,
  with `run_kind='catchup'`. Each re-run uses its own `data_cutoff`.
- A missed hour H gets weather **only** from the newest archived snapshot with
  `available_at ≤ H + 30 min` that still covers H+1…H+24. **It never uses the current forecast.**
- If such a snapshot exists, the row is written and scored normally, but it counts as *missed* for
  M4. If none exists, no `champion` row is written, which leaves a visible gap.
- Baselines and the IESO row for H use the same cutoff.

**Monitoring.**
- Live runs log `fetched_at − modelled available_at` per source, which must be ≥ 0. A negative
  value means a modelled rule is optimistic: it alerts and is recorded in `DATA_NOTES.md`.

## Consequences

- Backtests, training and live inference see the same information set, except for the
  documented optimism of the synthetic weather label.
- If the realtime source is kept, `L0` is the hour that just ended. Without it, `L0` is 8–32 h old.
- The catch-up policy means some hours are permanently missing on the public scoreboard. That is
  intended, because a back-dated forecast with today's weather would be leakage.
- Changing any rule changes the training lag, so it requires a retrain and an ADR update.

## Open items (to close before "Accepted")

1. **RealtimeTotals carries Market Demand, not Ontario Demand.** Choose one:
   - (a) derive Ontario ≈ Market − scheduled exports + c;
   - (b) use Market Demand as a feature only;
   - (c) drop the source.

   Under (b) or (c), provisional scoring disappears and scores arrive daily (the plan's
   "daily-only" branch).
2. **Final `REALTIME_LAG_H`**, from ≥ 24 h of `scripts/observe_realtime_cadence.py` output.
   Include whether partial or 404 hours (seen 2026-10-23 HE11/HE12) are back-filled later.
3. **Adequacy3 staleness rule.** Day-ahead hours are reissued only ~3×/day before ~20:28 EST
   on D−1, so a 60-min staleness exclusion would drop most D+1 targets of daytime issues
   (DATA_NOTES Contradiction 3).
4. **Pin `models=gfs_seamless`** in both Open-Meteo APIs. `best_match` resolves to it at Toronto
   today, and pinning it prevents silent train/serve model drift.

## Alternatives rejected

- **Train on ERA5 reanalysis (observed weather).** That is leakage: at issue time the model would
  know the weather that actually happened. Published MAPEs that use observed weather are not
  comparable to ours.
- **No availability stamps; assume everything up to `issue_time` is known.** Demand would then
  look 0–1 h old in training but be 8–32 h old live, and the offline metrics would be fiction.
- **Separate cutoffs per source.** This is harder to reason about and test, and no source needs one.
- **`data_cutoff = issue_time` (strict).** The live weather snapshot is fetched after the top of
  the hour, so it would be excluded every hour.
- **Catch-up with the current forecast.** That gives the "missed" hour information it could not
  have had.
