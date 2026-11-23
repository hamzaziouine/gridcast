# ADR-0008: The interval ladder: backtest residuals at launch, live residuals after 14 days

- Status: **Accepted** (steps 1 and 2 in use; step 3 planned)
- Date: 2026-11-23 (records BUILD_PLAN §4.5 as built)
- Related: BUILD_PLAN §4.5, §6.2 step 3b, §10 R11; ADR-0003, ADR-0005, ADR-0010;
  `src/gridcast/models/intervals.py`, `src/gridcast/jobs/daily.py` (`recalibrate_intervals`);
  DATA_NOTES §21.7

## Context

GridCast publishes an 80 % interval (p10, p90) with every forecast. LightGBM is trained for the
median only (ADR-0003). The interval has to be honest at launch, before any live error exists,
and has to adapt once live errors show how 24-hour-ahead weather really behaves.

## Decision

The interval at horizon h is `p50 x (1 + q)`, with q the 10 % and 90 % quantiles of relative
residuals `actual / p50 - 1` at that horizon (pooled over horizons when a horizon has fewer than
30 rows). The quantiles come from a ladder:

1. **Launch (bootstrap).** Residuals of backtest folds 1 to 10 only; folds 11 and 12 are held out
   to report coverage. The quantiles are widened by a fixed **1.15 lead-time allowance**, because
   the backtest's archived weather is closer to a short-lead forecast than the live 24-hour-ahead
   one. Held-out coverage at launch: 0.823.
2. **Live (from 14 EST days of final-scored residuals).** The daily job refits the quantiles each
   day from the champion's live residuals over the trailing min(60, available) days, with no
   allowance, writes `intervals.vNNNN.json` next to the model, points `champion.json` at it and
   logs `interval_recalibrated`. Idempotent per day.
3. **Planned: split-conformal per horizon** (Strong phase, BUILD_PLAN S2), replacing step 2; the
   target is 30-day live coverage between 0.78 and 0.82.

`COVERAGE_BAND = (0.76, 0.84)` is one constant shared by the bootstrap check, the gate and the
daily drift signal.

## In use

Step 2 started on 2026-11-06 and has run every day since (`intervals.v0002.json` to
`v0018.json` by 11-22). Live coverage: 0.813 from 11-07, 0.822 over the last 14 days to 11-22.
The trailing window still holds residuals of forecasts made with stale catch-up inputs between
10-24 and 11-01 (DATA_NOTES §21.2) until about 12-23; coverage stays in the band, so nothing was
changed.

## Consequences

- Intervals reflect live error within days of a regime change, without retraining.
- The same residual method already serves `seasonal_naive_168` (its offsets are refitted on the
  trailing 60 days at every prediction), so the baselines' intervals are comparable.
- A shadow is judged on the intervals it served (its backtest quantiles x 1.15), while the
  champion's are live-recalibrated (ADR-0016 records this asymmetry).

## Alternatives rejected

- **Quantile objectives** (three models per horizon). 72 models and crossing quantiles to fix by
  hand, for little gain.
- **Using the backtest coverage as the live claim.** The backtest's weather is easier than live
  weather, so its coverage would be optimistic.
