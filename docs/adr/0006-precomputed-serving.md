# ADR-0006: Precomputed serving only, no on-demand inference

- Status: **Accepted**
- Date: 2026-11-23 (records the design in use since 2026-10-24)
- Related: BUILD_PLAN §4.1, §6.6, §8.3; ADR-0011 (public snapshots and the Worker); ADR-0005;
  `src/gridcast/jobs/publish.py`, `infra/worker/`

## Context

The plan had two serving modes: precomputed forecasts published by the hourly job, and an
on-demand path on a container host (Azure Container Apps) that could run the model for a
request. Azure for Students was unavailable (2026-10-23), so the public API is a Cloudflare
Worker, which cannot load a LightGBM model and must not hold a database credential.

## Decision

- **Every public number is computed by a scheduled job and published as a file.** The hourly job
  writes `latest/forecast.json` and `latest/meta.json`, the daily job `latest/scoreboard.json`, to
  the public R2 bucket. The Worker only reads those files (ADR-0011).
- **No on-demand inference.** There is no endpoint that runs the model for arbitrary inputs or
  times. A forecast that the hourly job did not write cannot be shown.
- The forecast served is always the champion's rows for the newest issue hour that has them, with
  the same issue's IESO forecast and baselines next to it.

## Consequences

- Serving costs nothing: no compute per request, no Neon wake-up per request, nothing to scale.
- Freshness is bounded by the hourly cadence: the newest forecast is at most about an hour old
  plus the run time (median 3 min after the hour, DATA_NOTES §21.1). `/health` reports `stale`
  after 90 minutes.
- What is served is exactly what is scored: the published rows are the rows in `predictions`,
  insert-only for the champion.
- Interactive "what if" forecasts are out of scope for the API; the demo site reads the same
  files.

## Alternatives rejected

- **On-demand inference on a container host.** Needs a paid or student cloud account, a model
  loader, a database credential on a public host, and it would serve forecasts that are never
  scored.
- **Worker queries Neon directly.** A credential on the public host and a database wake-up per
  cache miss (ADR-0011).
