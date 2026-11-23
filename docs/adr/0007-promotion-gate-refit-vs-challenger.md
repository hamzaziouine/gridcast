# ADR-0007: Promotion gate: refit vs challenger, backtest then live shadow

- Status: **Accepted**
- Date: 2026-11-23 (records BUILD_PLAN §4.7 as built in ADR-0013 and ADR-0016)
- Related: BUILD_PLAN §4.7; ADR-0005, ADR-0010, ADR-0013 (backtest stage, registration),
  ADR-0016 (shadow stage, rollback); `src/gridcast/jobs/retrain.py`, `src/gridcast/jobs/gate.py`

## Context

The system retrains every week and on drift. A new model must never reach the public forecast
because it looked good once. Two kinds of candidate need different tests: a **refit** (same
family, same hyperparameters, same feature schema, newer data) cannot be "compared" with the
champion on a backtest, because both run the same procedure; a **challenger** (anything else)
can and must be.

## Decision

1. **Kind is derived, not chosen.** Same training profile and feature schema as the champion:
   refit. Anything else: challenger (ADR-0013).
2. **Backtest stage**, on the champion's own folds:
   - refit: pooled MAPE within plus or minus 0.5 % (relative) of the champion's recorded backtest
     (a determinism check), contracts and the leakage assertion green, held-out 80 % coverage in
     `COVERAGE_BAND` (0.76, 0.84);
   - challenger: MAPE at most 0.98 times the champion's (at least 2 % better) and coverage in
     the band.
   A pass registers a shadow (`shadow.json`), or queues the candidate if a shadow is running.
3. **Shadow stage**, live: at least 14 days and 300 scored target hours on pairs both models
   have; MAPE at most 1.00 times the champion's (refit) or 0.99 times (challenger); peak-hour MAPE
   at most the champion's plus 0.2 point (at least 30 peak pairs, else wait); coverage no further
   from 0.80 than the champion's plus 0.02.
4. **Promotion** writes `champion.json` (ADR-0005) and keeps the old pointer as
   `champion.prev.json`; the old champion is scored for 7 more days. **Rollback** if, over the
   same 3 days (at least 48 paired hours), the new MAPE exceeds 1.15 times the old one with no
   holiday to explain it; the rolled-back version is blocked and a 7-day cooldown starts.
5. Only the gate promotes or rolls back, once a day, in the `model-registry` concurrency group
   shared with the retrain. Every decision is a `model_registry_events` row with all numbers.

## Consequences

- Six `fast` candidates (10-24 to 11-22) were challengers against the `full` champion and
  were all rejected at the backtest stage (2.630 % vs 2.560 % needed); no shadow has run yet, so
  the gate has held every day (`no shadow running`; 21 of 21 runs since 11-01 successful).
- The earliest possible promotion is 14 days after the first shadow starts.
- The thresholds (2 %, 1 %, 0.2 point, 0.02, 1.15) are constants in code and are never changed to
  let a candidate through.

## Alternatives rejected

- **Promote on backtest alone.** A backtest uses modelled availability and archived weather
  (ADR-0004); only live scores show how a model does with real 24-hour-ahead inputs.
- **Comparing a refit against the frozen champion artifact on old folds.** That is in-sample for
  the champion, so the comparison would be meaningless.
- **Automatic promotion from the retrain job.** Couples training and serving; one job deciding
  both is harder to audit.
