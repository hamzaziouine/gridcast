# ADR-0003: LightGBM direct multi-horizon as the champion family, TCN as the planned challenger

- Status: **Accepted** (LightGBM). The TCN challenger is planned and **not built** (2026-11-23).
- Date: 2026-11-23 (records decisions taken 2026-10-23 and 10-24)
- Related: BUILD_PLAN §4.5, §10 R10; ADR-0004 (as-of features), ADR-0007 (gate), ADR-0008
  (intervals), ADR-0010 (bootstrap), ADR-0013 (fast profile); `src/gridcast/models/lgbm.py`,
  `src/gridcast/features/`; DATA_NOTES §15, §16, §21

## Context

GridCast forecasts Ontario Demand for the next 24 hours, every hour, on free CPU-only
infrastructure: a GitHub runner with 4 vCPUs retrains it weekly. The inputs are tabular: calendar
facts, recent demand known at the cutoff, and a 24-hour-ahead weather forecast for six cities.
About 77,000 hours of training data exist (2018 onwards). The project is built from scratch, so the
model has to be something one person can train, test and explain.

## Decision

1. **Champion family: LightGBM, direct multi-horizon.** Twenty-four regressors, one per horizon
   1 to 24, objective `l1` (MAPE-friendly), learning rate 0.03, 63 leaves, up to 400 rounds. Feature
   set v1 (`FEATURE_SCHEMA_VERSION` 1): calendar at the target, demand levels with the same lag
   in training and live (ADR-0004), weather at the target for six cities plus weighted
   aggregates. The artifact is gzipped JSON with a sha256 in `champion.json` (ADR-0005), not a
   pickle. Two training profiles exist (ADR-0013): `full` for the bootstrap champion and `fast`
   for the weekly candidate.
2. **Intervals come from residuals, not from quantile objectives** (ADR-0008).
3. **TCN is the planned challenger** (BUILD_PLAN §4.5: PyTorch on CPU, trailing 168 h plus 24
   future covariate rows, three quantile heads, about 100k parameters). It is not built yet. When
   it is, it enters like any other candidate: a challenger that must be at least 2 % better on
   the champion's folds, then 14 days in shadow (ADR-0007). If it loses, the README says so.

## Evidence

- Backtest (12 monthly folds, 2025-10 to 2026-09, 35,040 forecasts per model): LightGBM MAPE
  2.61 %, 60.7 % better than the same hour last week (target 30 %), held-out 80 % coverage 0.82.
- Live (champion-paired rows of issue hours 2026-10-24 to 11-22, DATA_NOTES §21.8): champion
  2.49 %, IESO 1.69 %, same hour last week 3.87 %, persistence 9.00 %.
- Weekly `fast` candidates on the runner: 13 min each; all six so far (10-24 to 11-22) scored
  2.630 % and were rejected as challengers (2.560 % needed). This is the gate working, not a
  failure: the `fast` profile trades a little accuracy for runtime.

## Consequences

- Retraining fits a GitHub runner and costs no money.
- The gap to IESO (about 0.8 point over the first month) is expected: IESO has embedded
  generation, ICI programmes and its own weather vendor. The stated goal is to be within about a
  point of IESO, not to beat it.
- A family change (TCN, new features, new hyperparameters) is always a challenger and always
  goes through the full gate.

## Alternatives rejected

- **LSTM.** Slower to train on CPU for no expected gain on this data; the plan picked a TCN as
  the deep challenger instead.
- **Three quantile objectives per horizon** (72 models). Marginal gain, and quantile crossing has
  to be fixed by hand anyway.
- **Training on observed weather (ERA5).** Leakage; see ADR-0004.
