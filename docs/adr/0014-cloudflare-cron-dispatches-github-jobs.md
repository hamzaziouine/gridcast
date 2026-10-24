# ADR-0014: Cloudflare Cron Triggers dispatch the GitHub jobs

- Status: **Accepted**
- Date: 2026-10-24
- Related: ADR-0001 (GitHub Actions cron), ADR-0012 (cron slots, per-hour success), ADR-0011
  (Worker); `infra/worker/src/index.js`, `infra/worker/wrangler.jsonc`

## Context

After the cron change to `2,12,22` (pushed 19:48Z on 2026-10-24), GitHub's scheduler went
silent: no scheduled `hourly.yml` run for the slots 20:02 to 21:22 (eight slots), while the
workflow stayed `active`. The `daily.yml` slot for 18:20 started at 20:57, 2 h 37 min late. The
20:00 and 21:00 issue hours were covered only by manual `workflow_dispatch`. GitHub documents
that scheduled events can be delayed or dropped under load; for a job whose value is "every
hour, unattended", that is the main reliability risk.

## Decision

The Worker that already serves the public API also gets two Cloudflare Cron Triggers:

| cron (UTC) | dispatches |
|---|---|
| `2,22 * * * *` | `hourly.yml` |
| `20 13,18 * * *` | `daily.yml` |

On each trigger, the Worker calls `POST /repos/hamzaziouine/gridcast/actions/workflows/<file>/dispatches`
with `ref: main`. The token is the Worker secret `GH_DISPATCH_TOKEN`: a fine-grained PAT limited
to this repository with Actions: read and write and nothing else. Without the secret the trigger
is a no-op.

GitHub's own `schedule:` stays in place as a second, independent path. Duplicates are already
safe: success is judged per issue hour and a repeat run ends `skipped` (ADR-0012), and every
workflow has a concurrency group.

## Consequences

- Two independent schedulers; either one alone keeps the loop running.
- Cloudflare free plan: 2 of the 5 account cron triggers used; no cost.
- One more secret to rotate (the PAT expires; set a calendar reminder for its expiry).
- `retrain.yml` and `keepalive.yml` stay on GitHub cron only: they are weekly, and a delay of
  hours does not matter there.

## Rejected

- cron-job.org or another external pinger: a third-party account holding a token, for no gain
  over a Worker we already run.
- Dropping GitHub `schedule:` entirely: it costs nothing to keep as a fallback.
