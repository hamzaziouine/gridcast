# ADR-0010: Bootstrap champion, with a hard floor and a time-boxed fallback

- Status: **Accepted**
- Date: 2026-10-23
- Related: BUILD_PLAN §4.3 (`champion.json`), §4.7, §9 C8/C9, §12; ADR-0008 (intervals);
  `src/gridcast/jobs/train.py`

## Context

The hourly job (C10) needs a champion before the promotion gate exists, and the gate needs a
champion to compare against. The first model therefore cannot pass through the gate. Two goals
pull against each other: never promote a broken model, and never let "Core live" wait on an ML
metric.

## Decision

`gridcast train` writes `models/champion.json` only if none exists, from the 12-fold rolling-origin
backtest (§7.3) of LightGBM and `seasonal_naive_168` on the same folds:

| Outcome | Condition | `bootstrap_reason` / `champion_status` |
|---|---|---|
| Target met | pooled MAPE ≤ 4.0 %, ≥ 30 % better than `seasonal_naive_168`, held-out `coverage_80` in `COVERAGE_BAND` (0.76, 0.84), leakage assertion green | `bootstrap` / `ok` |
| Target missed, floor met | MAPE strictly below `seasonal_naive_168`, `coverage_80` in [0.70, 0.90], leakage green | `bootstrap-below-target` / `below_target` |
| Floor failed | anything else | nothing promoted: a bug under the slip rule |
| Floor failed after Fri 2026-10-30 12:00 UTC | `gridcast train --allow-fallback` | `seasonal_naive_168` promoted, `bootstrap-fallback` / `fallback` |

Every outcome writes `model_registry_events` rows (`trained`, `backtested`, and `promoted` when
something is promoted) with all the numbers in `metrics`, including the target that was missed.
The 4.0 % / 30 % / band numbers are never changed to make a check pass (§12).

While `champion_status ≠ ok`, a candidate that meets the target on backtest may replace the
bootstrap once with `gridcast promote --replace-bootstrap` (`reason='bootstrap-replace'`, built
with the gate); after that every promotion goes through the gate.

## Consequences

- Core goes live on schedule with an honest label: `/health`, the README footnote and
  `scoreboard.json` show `champion_status`.
- A LightGBM that is not even better than last week's demand is never served.
- The bootstrap intervals are backtest residual quantiles (folds 1–10) × 1.15 (ADR-0008 step i);
  live recalibration (step ii) replaces them after 14 days of scored residuals.
