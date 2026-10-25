# ADR-0014: Cloudflare Cron Triggers dispatch the GitHub jobs

- Status: **Accepted** (amended 2026-10-25: weekly jobs, `scheduler` input, retrain idempotency)
- Date: 2026-10-24
- Related: ADR-0001 (GitHub Actions cron), ADR-0012 (cron slots, per-hour success), ADR-0011
  (Worker), ADR-0013 (retrain); `infra/worker/src/index.js`, `infra/worker/wrangler.jsonc`,
  `src/gridcast/jobs/retrain.py`; DATA_NOTES §20.1

## Context

After the cron change to `2,12,22` (pushed 19:48Z on 2026-10-24), GitHub's scheduler went
silent: no scheduled `hourly.yml` run for the slots 20:02 to 21:22 (eight slots), while the
workflow stayed `active`. The `daily.yml` slot for 18:20 started at 20:57, 2 h 37 min late. The
20:00 and 21:00 issue hours were covered only by manual `workflow_dispatch`. GitHub documents
that scheduled events can be delayed or dropped under load; for a job whose value is "every
hour, unattended", that is the main reliability risk.

The weekly jobs were first left on GitHub cron only ("a delay of hours does not matter"). On
2026-10-25 the Sunday 03:00Z `retrain.yml` slot had still not fired at 06:12Z: GitHub can drop a
weekly slot entirely, which costs a whole week of the loop, not hours.

## Decision

The Worker that already serves the public API also gets four Cloudflare Cron Triggers (4 of the
free plan's 5 per account):

| cron (UTC) | dispatches | inputs |
|---|---|---|
| `2,22 * * * *` | `hourly.yml` | `scheduler=cloudflare` |
| `20 13,18 * * *` | `daily.yml` | `scheduler=cloudflare` |
| `0 3 * * SUN` | `retrain.yml` | `scheduler=cloudflare`, `reason=weekly`, `profile=fast` |
| `0 6 * * MON` | `keepalive.yml` | `scheduler=cloudflare` |

Weekdays are written as names: Cloudflare numbers them 1 = Sunday … 7 = Saturday, unlike
GitHub's 0 = Sunday. The `DISPATCH` keys must equal `wrangler.jsonc` character for character
(`event.cron` is the configured string); a node test checks it, and another checks that every
dispatched workflow declares every input the Worker sends (an undeclared input is a 422).

On each trigger, the Worker calls `POST /repos/hamzaziouine/gridcast/actions/workflows/<file>/dispatches`
with `ref: main` and those inputs. The token is the Worker secret `GH_DISPATCH_TOKEN`: a
fine-grained PAT limited to this repository with Actions: read and write and nothing else.
Without the secret the trigger is a no-op.

**`scheduler=cloudflare` → `run_kind='scheduled'`.** Each workflow maps
`event_name == 'schedule' || inputs.scheduler == 'cloudflare'` to `scheduled`, anything else to
`manual`. Before this amendment every Cloudflare-started hourly run was recorded `manual`, which
the plan's M4 (share of hours whose *scheduled* run finished within 20 min) counts as missed.
A Cloudflare dispatch is the scheduler, not a person, so it is `scheduled`. The rows written
from 2026-10-24 ~21:30Z until this change keep `manual` (history is not rewritten; DATA_NOTES
§20.1 states the window).

GitHub's own `schedule:` stays in place as a second, independent path.

**Duplicate safety, per job:**

- `hourly`, `daily`: success is judged per logical time; a repeat run ends `skipped` (ADR-0012).
- `retrain`: the `model-registry` concurrency group serialises the two starts, and a
  `reason=weekly` run is keyed on its **weekly slot** (`retrain.weekly_slot`: the latest Sunday
  03:00 UTC at or before now) instead of its start hour. The second start for a slot that already
  ended `ok` is `skipped` before it downloads or trains anything (one `pipeline_runs` insert, one
  select, one update). The partial unique index "one `ok` per (job, logical_time)" makes a race
  between two runs end with one `ok` and one `skipped`. A slot whose first run *failed* is
  retried by the other scheduler, which is wanted. Manual and drift runs keep `floor_hour(now)`
  and are never deduped by week.
- `keepalive`: idempotent (re-enabling a workflow twice is harmless; the checks only read).

## Consequences

- Two independent schedulers for every scheduled job; either one alone keeps the loop running.
- Cloudflare free plan: 4 of the 5 account cron triggers used; no cost.
- One more secret to rotate (the PAT expires; set a calendar reminder for its expiry).
- A weekly run delayed past midnight into Monday still maps to Sunday's slot. A weekly run
  started more than 7 days late would be keyed on the next slot; that cannot happen with two
  schedulers firing within the same day.

## Rejected

- cron-job.org or another external pinger: a third-party account holding a token, for no gain
  over a Worker we already run.
- Dropping GitHub `schedule:` entirely: it costs nothing to keep as a fallback.
- Keying retrain on the ISO week: a Sunday 03:00 run delayed past Sunday midnight would land in
  the next ISO week and train twice.
- Deduping retrain on "any registry event in the last 6 days": couples scheduling to the event
  log and would also swallow a legitimate drift retrain.
