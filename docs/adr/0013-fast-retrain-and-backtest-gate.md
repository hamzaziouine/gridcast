# ADR-0013: Fast retrain profile, backtest on the champion's folds, shadow registration

- Status: **Accepted**
- Date: 2026-10-24
- Related: BUILD_PLAN §4.5, §4.7 (steps 1, 6, 7), §6.2 step 4, §6.3; ADR-0005, ADR-0010;
  DATA_NOTES §15, §19.2; `src/gridcast/models/lgbm.py`, `src/gridcast/jobs/retrain.py`,
  `.github/workflows/retrain.yml`

## Context

The bootstrap champion's `gridcast train` backtest took **104 min on a 16-thread laptop**
(DATA_NOTES §15). It does 576 booster fits: for each of 12 folds × 24 horizons, an early-stopping
probe and then a refit. A GitHub runner has 4 vCPUs and a 6 h job limit, and the plan wants a
weekly unattended retrain. The candidate must also be compared with the champion "on identical
folds" (§4.7 step 1). Refitting the champion's procedure on the runner as well would double the
cost.

## Decision

1. **Two training profiles** in `models/lgbm.py`:

   | | `full` (bootstrap champion) | `fast` (retrain.yml) |
   |---|---|---|
   | early-stopping probe | yes (last 90 days), then refit with the best round count | no: a fixed 400 rounds on the whole window |
   | `max_bin` | 255 (LightGBM default) | 63 |
   | training issue hours | every hour from 2018-01-02 | half: kept when `hour + day number` is even, so the parity alternates by day and every horizon still sees all 24 target hours |
   | other parameters | `l1`, lr 0.03, 63 leaves, … | identical |

   Dropping the probe costs almost nothing. The champion's own probes stopped at 341–400 of 400
   rounds, and 15 of 24 stopped at ≥ 395. The profile is stored in the artifact. Artifacts from
   before this ADR carry none and load as `full`.
2. **The candidate is backtested on the champion's own folds.** The fold months come from the
   champion's `backtest.json`. Its recorded pooled MAPE is the reference. That backtest is
   deterministic: C9 reproduced C8's numbers exactly. So this comparison is the plan's
   "identical folds, both procedures" without re-running the champion procedure. A fresh
   `gridcast train` of the champion procedure on those folds would give the same numbers.
3. **Kind is derived, not chosen.** If the candidate's profile equals the champion's, it is a
   `refit` and must reproduce the champion's backtest MAPE within ±0.5 % relative (the plan's
   determinism check). Otherwise it is a `challenger` and needs MAPE ≤ 0.98 × the champion's
   (≥ 2 % relative). Both need held-out `coverage_80` in `COVERAGE_BAND`. Today the champion is
   `full` and the weekly candidate is `fast`, so every weekly candidate is a **challenger**.
4. **Registration only, never promotion.** Pass → `shadow.json` (`shadow_started`). If a shadow is
   already running, the candidate goes to `queued.json` (`candidate_queued`; one slot, newest
   wins). A challenger may pre-empt a refit shadow. Fail → `rejected` with every number in the
   event. `champion.json` is written only by the gate, after ≥ 14 days and ≥ 300 scored target
   hours of shadow (§4.7 step 2). The gate (`gate.yml`, C19) is not built yet.
5. **The shadow is scored live.** When `shadow.json` exists, the hourly job writes `shadow` rows
   from the same as-of features. It writes them only for issue hours from the shadow's
   `started_at` on, because the shadow's training data ends just before that. Rows are
   insert-only, like the champion's. A shadow failure is a note and never costs the champion
   its rows.
6. **Triggers.** Weekly cron (Sunday 03:00 UTC), `workflow_dispatch(reason, profile)`, and the
   daily job's error-based drift rule. That rule fires when the 7-day champion MAPE is above
   1.5 × its reference for 3 consecutive days, with ≥ 7 live days and no retrain started in the
   last 72 h. The daily job then runs `gh workflow run retrain.yml -f reason=drift:mape` (its
   only `actions: write` use).
7. **MLflow** (`gridcast-retrain` experiment on DagsHub) gets params, metrics and the stage
   verdict. An MLflow outage is a note, not a failure (ADR-0005).

## Measured (DATA_NOTES §19.2)

Backtest on the champion's 12 folds (2025-10 … 2026-09), 35,040 forecasts, local
`OMP_NUM_THREADS=4`:

| profile | pooled MAPE | peak MAPE | held-out coverage_80 | wall time |
|---|---:|---:|---:|---:|
| `full` (champion, C8) | 2.612 | 3.147 | 0.823 | 104 min on 16 threads |
| `fast` | **2.627** (+0.015 pt, +0.57 % rel.) | 3.191 | 0.828 | **12 min 48 s on 4 threads** |

The loss is small. It is still larger than the ±0.5 % refit tolerance, which is why `fast` is a
different procedure (a challenger) and not a refit.

## Consequences

- The weekly retrain fits a 4-vCPU runner well inside the 90-minute timeout. The runner time is
  recorded in DATA_NOTES §19.2.
- With a `full` champion, a `fast` challenger must beat 2.612 by 2 % (MAPE ≤ 2.560) to reach
  shadow. Same-fold candidates only differ by procedure, so the weekly fast candidate will
  normally be **rejected**. That is the gate working, not a bug. Newer data alone cannot win on
  identical folds; it wins live, which is the refit path. Once the champion is itself `fast` (one
  challenger promoted through the gate), weekly candidates become refits: they reproduce the
  champion's backtest and their decision is made live in shadow at 1.00×.
- An owner who wants weekly refits to reach shadow sooner can set a `fast` champion through the
  normal gate. That needs a fast challenger that wins by 2 %. A one-off manual `train --profile
  fast` bootstrap is not offered, because the champion's status is `ok` and the plan permits
  `--replace-bootstrap` only below target.
- Training reads Neon once per retrain (~40 MB egress, weekly), not the plan's bucket Parquet
  snapshot. This is a deviation: the egress is ~3 % of the monthly allowance, so the snapshot
  is not worth building yet.

## Alternatives rejected

- **Run the `full` profile on the runner**: ~4× fewer threads than the 104-min laptop run. Hours
  of runtime and a risk of hitting the 6 h limit.
- **Refit the champion procedure in every retrain to compare**: doubles the cost and gives
  numbers we already have (the run is deterministic).
- **A fixed every-2-hours training grid**: each horizon would then see only one parity of target
  hour. The day-alternating grid keeps every target hour.
- **Calling a `fast` candidate a refit because it is "close"**: that moves the plan's ±0.5 %
  tolerance to fit the result. Rejected under §12 ("never change a target to make a check pass").
