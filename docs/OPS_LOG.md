# GridCast operations log

One entry per operational event, newest first. Tags:

- **INTERVENTION**: an unplanned manual action to restore service or data (a data repair, a
  re-run, a rollback or a hotfix). These are what M6 counts (BUILD_PLAN §2: days with zero manual
  intervention). Planned feature merges that pass CI and deploy on their own do not count.
- **OWNER**: something only the repository owner can do (accounts, tokens, dashboards).

The build days (2026-10-23 to 10-25) are recorded in [`DATA_NOTES.md`](DATA_NOTES.md) §1 to §20.
This log starts with Core live. Details and every number below: DATA_NOTES §21.

## 2026-11-23 · INTERVENTION · hotfix, catch-up widened, IESO ingest fixed, IESO rows repaired

Health check (issue hours 2026-11-01 18Z to 11-23 01Z, 512 hours):

- `/health` 01:38Z: `status ok`, `dispatcher.status dispatched` (01:22:40Z), last issue 01:00Z,
  `stale false`, champion `lgbm-2026.10.23-49ca` (`ok`), unchanged since 2026-10-24. Four weekly
  candidates (11-01, 11-08, 11-15, 11-22) were trained and rejected by the backtest gate (2.630 vs
  2.560 needed).
- Hourly: 1,102 runs (1,022 Cloudflare dispatches, 80 GitHub fallback), 71 failed: 68 during the
  11-03 incident, 3 one-off (one setup-uv HTTP 504, two GitHub runs that never started a job).
  481 of 512 issue hours have their 24 champion rows (93.9 %).
- Daily: 87 runs, 5 failed (4 correct `actuals_stale` alarms on 11-03, one GitHub run that never
  started a job on 11-09 18:20Z; the 13:20Z run that day was fine). Retrain 3/3, keepalive 3/3,
  gate 21/21 successful.
- Neon `pg_database_size` 96.6 MB (budget 0.5 GB).

Shipped (all CI green on the branch before `main` was fast-forwarded):

- `dbb09ad` **hotfix**: a baseline horizon with no visible input no longer costs the champion its
  hour (cause of the 11-03 incident below).
- `88ea0c6`, `e2fc654`: catch-up and RealtimeTotals lookback 6 h to 24 h.
- `a38a9c4`: Adequacy3 read newest first, versions the prune would delete are no longer fetched,
  60 files a run (was 20).
- `c09835e`, `735bb0f`: `gridcast repair ieso-rows` and the manual `repair.yml` workflow.
- **Data repair**: `repair.yml` run 45100324658 (02:27Z, after dry runs 45099307708 and
  45100248067) rewrote the `ieso_adequacy` rows of 125 issue hours of the backlog days (1,578
  rows changed, 144 added) with the snapshot IESO had published when each live row was written. IESO
  MAPE on the repaired window went from 1.818 % to 1.691 %. The 2026-11-01 chart claim that
  GridCast beat IESO day-ahead (2.38 % vs 2.43 %) was wrong: for issue days 10-28 to 11-01
  IESO's day-ahead MAPE is 1.37 % (97 of 120 hours scored so far) against GridCast's 2.86 %.
  Champion rows were not touched.
- Docs: the GitHub cron description corrected (ADR-0012 amendment, `hourly.yml`, DATA_NOTES
  §17.1), DATA_NOTES §21, this log.

M6: this entry restarts the zero-intervention clock at 2026-11-23. With no further intervention,
60 days are reached on 2027-01-22.

Verify next: the 03:02Z hourly run on the new code; the 13:20Z daily run scores the 144 new IESO
rows and rebuilds `score_daily`.

**OWNER**, open:

- Healthchecks: add the optional checks (`HEALTHCHECKS_KEEPALIVE_URL`, `_RETRAIN_URL`,
  `_PRUNE_URL`, `_GATE_URL`, `_API_URL`) and a dispatcher check pinged by the Worker. Only the
  hourly and daily checks exist.
- Neon API key (`NEON_API_KEY`) for the compute-hours guard.
- The fine-grained PAT behind the Worker secret `GH_DISPATCH_TOKEN` expires about 2027-11-01:
  rotate before then. Confirm that the old classic all-scope PAT `gridcast-dispatch` is deleted.

## 2026-11-03 to 11-04 · incident · 31 champion hours lost (IESO actuals delayed)

- IESO's final actuals stopped at 2026-11-01 05Z until the 11-04 13:20Z daily run. From issue hour
  11-03 06Z the same-hour-yesterday baseline had no input for its last horizons, its NaN failed
  the predictions contract for the whole issue hour, and the champion's rows were lost with it.
  Every hourly run failed until 11-04 12Z (Healthchecks `/fail` each hour; daily
  `actuals_stale`). Recovery was automatic at 11-04 13:22Z.
- Issue hours 11-03 06Z to 11-04 12Z have no rows for any model and stay that way (champion rows
  are never back-dated).
- No manual action was taken at the time. The fix shipped on 2026-11-23 (`dbb09ad`).

## 2026-11-02 · Core live

- Met at issue hour **2026-11-02 20:00Z**: 24 consecutive hours (from 11-01 21Z) whose `ok` run
  was started by the Cloudflare cron with no `failed` run. Counting every on-time `ok` hour,
  whichever scheduler started it, met at 11-02 17:00Z.
- `/v1/forecast/latest` and `/health` answer 200 with `stale: false`; the README first screen has
  the live link and the 7-day chart; `champion.json` is `lgbm-2026.10.23-49ca`, bootstrap reason
  `bootstrap`, `champion_status ok` (backtest MAPE 2.61 %, 60.7 % better than the same hour last
  week; targets 4.0 % and 30 %).
- Live scoring started on 2026-10-24.
- This entry was written on 2026-11-23 from the run records (no OPS_LOG existed before).

## 2026-11-01 · INTERVENTION · Cloudflare dispatch restored

- From 10-25 06:22Z to 11-01 the Cloudflare cron dispatched nothing: the `GH_DISPATCH_TOKEN`
  dashboard text variable had been removed by the first `wrangler deploy` (DATA_NOTES §21.1).
  Hours were covered by GitHub cron (about 7 % of slots fired), manual dispatches and catch-up.
- Fixed by `aa20c8f` (`keep_vars`, `scheduler: cloudflare` input) and a new fine-grained PAT
  stored as a Worker Secret (**OWNER**, done 11-01). Verified 18:02:40Z (run 41334535515).
