# ADR-0016: The shadow gate: promotion, waiting, rejection and rollback

- Status: **Accepted**
- Date: 2026-10-25
- Related: BUILD_PLAN §4.7 items 2-8, §6.4, §6.7 step 3, §7.1 (gate logic tests); ADR-0005
  (`champion.json` authoritative), ADR-0013 (retrain registers shadows); `src/gridcast/jobs/gate.py`,
  `src/gridcast/jobs/predict.py`, `.github/workflows/gate.yml`; DATA_NOTES §20.4

## Context

ADR-0013 registers candidates as `shadow.json` / `queued.json` and never promotes. The plan's
gate decides, after the shadow has been scored live next to the champion, whether it replaces
the champion, and can undo a promotion that turns out worse.

## Decision

`gridcast gate evaluate` (gate.yml, daily 06:30Z, `model-registry` concurrency group shared with
retrain) applies the plan's rules as written:

| step | rule (plan §4.7) |
|---|---|
| ready | shadow ≥ 14 days **and** ≥ 300 distinct target hours scored on pairs both models have |
| early | `drift_early`: a **refit** shadow with ≥ 7 days and ≥ 150 target hours while the champion's `rolling_mape_7d` breach has lasted ≥ 7 consecutive days |
| MAPE | shadow ≤ 1.00 × champion (refit) / 0.99 × (challenger); fail → `rejected` |
| peaks | ≥ 30 peak pairs (`peak_hours.is_peak`) else **wait**; shadow peak MAPE ≤ champion + 0.2 pt, else `rejected` |
| coverage | \|cov_shadow − 0.80\| ≤ \|cov_champion − 0.80\| + 0.02, else `rejected` |
| promote | `champion.prev.json` ← old pointer, `champion.json` ← shadow pointer (`promoted_at`, `previous_version`, `champion_status='ok'`), `shadow.json` deleted, event `promoted` with every number |
| queue | after a promote/reject (or whenever no shadow runs), `queued.json` → `shadow.json` (`started_at` = now), event `shadow_started` |
| rollback | while the champion is < 7 days old and `champion.prev.json` exists: over the same trailing 3 EST days, ≥ 48 paired target hours, `MAPE_new > 1.15 × MAPE_prev` and no Ontario holiday in the window → `champion.json` ← prev pointer, prev deleted, event `rolled_back` (`blocked_version`, `cooldown_until` = +7 days) |
| after a rollback | a passing shadow **waits** until `cooldown_until`; the blocked version is rejected whenever it reappears |

`gridcast gate rollback` does the same rollback by hand (`triggered_by='manual'`). The hourly
job writes `prev_champion` rows (from `champion.prev.json`, insert-only, isolated like the shadow)
for issue hours in the 7 days after `promoted_at`, so the rollback compares both models on the
same hours.

**Dry run by default.** The CLI and a manual dispatch compute the decision and every number
from live data and print them; they write nothing: no pointer, no event, no `pipeline_runs` row,
no heartbeat. Only the schedule (or a dispatch with `apply` ticked) writes.

**Pure rules, tested by table.** `shadow_decision` and `rollback_decision` take numbers and
return `promote | hold | reject | rollback` with the reason; the data access is separate.

## Deviations from the plan

- **Rollback runs in the gate, not in the daily job** (plan §6.2 step 5): both are daily, and
  keeping every pointer write in one job under the `model-registry` group means a retrain can
  never interleave with a promotion or rollback.
- **Holiday only, no ICI flag**: the plan excuses a rollback gap "explained by an ICI/holiday
  flag in the drift context". No ICI flag exists yet; Ontario holidays (civil dates of the
  window, `holidays` CA-ON) are the only excuse. Noted in DATA_NOTES §20.4.
- **Coverage of the shadow is measured on the intervals it served** (its backtest quantiles ×
  1.15), while the champion's are live-recalibrated after 14 days (§4.5). The plan says "both on
  live-recalibrated intervals"; recalibrating a shadow would mean running the daily
  recalibration on two models. The rule is unchanged; the asymmetry is stated in the event.
- **The cooldown reads `cooldown_until` from the `rolled_back` event**, not `event_time`, so the
  gate's own clock decides (and tests can run on a simulated clock).
- **DeleteObject is used** (pointer files only). The plan listed four S3 calls; the jobs key was
  checked to allow DeleteObject on R2 on 2026-10-25 (scratch key put, deleted, re-deleted).
- **Comparisons at a tolerance boundary pass** (`+1e-9`): "≤ champion + 0.2 pt" is read
  inclusively, as written.

## Consequences

- With today's `full` champion every weekly `fast` candidate is a challenger (ADR-0013) and is
  normally rejected at the backtest stage, so the gate will mostly print `hold: no shadow
  running` until a challenger passes. The first possible decision is 14 days after the first
  `shadow_started`.
- A promotion needs no deploy: the hourly job reads `champion.json` on its next run; the
  Worker's `/health` shows the new version from the next `meta.json`.
- `keepalive` gains `gate_stale` (no `ok` gate run in 48 h), armed once a gate run exists.
