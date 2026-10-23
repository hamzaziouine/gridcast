# ADR-0001: GitHub Actions cron + Typer CLI, not Airflow / Kubernetes / Kafka

- Status: Accepted
- Date: 2026-10-23
- Related: BUILD_PLAN §4.1, §6, §3.7

## Context

The workload is small and fully batch:
- an hourly job (ingest, predict, publish) that runs for 1–2 minutes;
- a handful of daily, weekly and monthly jobs (actuals backfill and re-score, retrain, promotion
  gate, prune, keepalive).

There are no streaming inputs. IESO and Open-Meteo are polled files and APIs, and the fastest
source re-posts every 5 minutes (`docs/DATA_NOTES.md` §1).

The project has to run at $0 for a solo maintainer, and it has to stay up unattended for months.
A self-hosted orchestrator (Airflow, Dagster, Prefect server) or a Kubernetes cluster needs an
always-on host. No free tier offers one reliably (§3.7: Fly.io has no free tier, Render sleeps,
HF Spaces are restricted).

## Decision

- Every job is a **Typer CLI command** (`gridcast jobs hourly`, `gridcast jobs daily`,
  `gridcast train`, …).
- **GitHub Actions `schedule:` workflows** call those commands (`hourly.yml` at `7,27 * * * *`,
  `daily.yml`, `retrain.yml`, `gate.yml`, `prune.yml`, `keepalive.yml`), plus `workflow_dispatch`
  for manual and catch-up runs.
- Run state lives in Postgres (`pipeline_runs`, the dedupe and skip rule). Liveness comes from
  healthchecks.io pings.
- Jobs are idempotent and catch-up safe, so a delayed or doubled cron run is harmless.

## Consequences

Benefits:
- Zero infrastructure to run or pay for. Logs, secrets (OIDC to Azure) and history come with the
  repository.
- The same command runs locally, in CI, in cron, or later inside any orchestrator, so portability
  is kept.

Costs, and how the design absorbs them:
- GitHub cron is best-effort. Runs can be delayed 5–30 min at busy minutes, or skipped. Two
  slots per hour, the `data_cutoff` rule (ADR-0004) and a 6-hour catch-up absorb this, and M4
  measures it honestly.
- Scheduled workflows are disabled after 60 days without repository activity, which
  `keepalive.yml` handles.
- There is no DAG UI, backfill UI or task-level retry. Dependencies are expressed as ordered steps
  inside one command, and failures surface through `pipeline_runs`, healthchecks and Actions logs.
- Concurrency is controlled with workflow `concurrency:` groups, not by a scheduler.

## Alternatives rejected

- **Airflow / Dagster / Prefect (self-hosted).** They need an always-on scheduler, a metadata
  database and a webserver, i.e. more operational surface than the workload itself, with no free
  host.
- **Kubernetes CronJobs.** These need a cluster. Managed free tiers are gone or too small, and
  running a cluster for a 2-minute hourly job is unjustifiable.
- **Kafka / streaming.** The inputs are files polled hourly or every 5 minutes, so there is nothing
  to stream.
- **Managed workflow services (Cloud Composer, MWAA).** Not free.
- **A long-running process with an in-process scheduler on the API host.** The API scales to zero
  (ADR-0006), and coupling jobs to the serving host would make one outage take down both.
