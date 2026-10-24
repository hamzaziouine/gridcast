# ADR-0011: Public JSON snapshots in R2, served by a database-free Worker

- Status: **Accepted**
- Date: 2026-10-24
- Related: BUILD_PLAN §3.0 item 8, §4.1 (published snapshots), §6.1 step 9c, §6.6 (Worker
  fallback), §8.3; `src/gridcast/jobs/publish.py`, `src/gridcast/jobs/daily.py`,
  `infra/worker/`; DATA_NOTES §17.4, §18.4

## Context

Azure for Students was rejected on day 1, so the public API is the plan's fallback: a Cloudflare
Worker that serves files the pipeline publishes. The Worker must hold no database credential and
must never wake Neon (§4.4: nothing may hit Neon more often than every 10 minutes).

## Decision

- The jobs write three small JSON documents to the public bucket `gridcast-public` (written with
  the jobs' read-write key; the Worker only reads):
  - `latest/forecast.json` — hourly; the newest issue hour with champion rows, 24 rows of
    `{horizon, target_time, p10, p50, p90}` plus the same issue's benchmark p50s
    (`ieso_adequacy` + `ieso_staleness_min`, `seasonal_naive_168`, `seasonal_naive_24`,
    `persistence`). ~5.7 kB.
  - `latest/meta.json` — hourly; champion pointer fields, the last run's status and notes
    (exception types only), newest input timestamps, `stale_after_minutes` (90). ~1 kB.
  - `latest/scoreboard.json` — daily; 7 d / 30 d windows on champion-paired rows, since-launch
    from `score_daily`, per-horizon 30 d, daily MAPE series. ~3.4 kB at launch.
  Every document carries `schema_version: 1` and the IESO / Open-Meteo attribution. Field
  lists live in the module docstrings, which are the schema of record.
- The Worker (`infra/worker/src/index.js`, R2 binding `PUBLIC` → `gridcast-public`) serves
  `/v1/forecast/latest`, `/v1/scoreboard` (`cache-control: public, max-age=60`, `X-Model-Version`,
  `X-Issue-Time`, ETag), `/health` (always 200; `stale` from `meta.json`, so UptimeRobot can
  keyword-match `"stale":false`) and `/`. CORS `*` everywhere. A snapshot not yet published, or an
  R2 error, is a 503 JSON body; an unknown path is a 404 JSON body.
- Deploys stay with Cloudflare Workers Builds from Git (root directory `infra/worker`); no
  `deploy-worker.yml` and no Cloudflare token in GitHub. `npm test` (node:test, fake binding) and
  `wrangler deploy --dry-run` are the local checks; CI runs the node tests.

## Consequences

- The public API is as fresh as the last hourly publish (≤ ~1 h + cron delay) and cannot show a
  forecast the jobs did not write; the on-demand fallback of §8.3 does not exist on this host.
- Key names differ from the plan's `api/v1/*.json`; the Worker's paths are the stable contract,
  not the bucket keys.
- The bucket stays private to the internet (no r2.dev); only the Worker binding reads it.

## Alternatives rejected

- **Worker queries Neon over the serverless driver** — a credential on the public host and a Neon
  wake-up per cache miss (§4.4).
- **Status-branch mirror on raw.githubusercontent.com** — needed only if R2 were unavailable
  (B2 fallback); R2 works, so the hourly job keeps `contents: read`.
