# ADR-0005: `models/champion.json` in the object store is the one source of truth for serving

- Status: **Accepted**
- Date: 2026-11-23 (records the design in use since 2026-10-24)
- Related: BUILD_PLAN §4.3, §4.7; ADR-0010 (bootstrap), ADR-0013 (retrain), ADR-0016 (gate),
  ADR-0008 (intervals); `src/gridcast/jobs/predict.py`, `train.py`, `gate.py`, `daily.py`

## Context

Three places could claim to know "the model in production": the MLflow registry on DagsHub,
the `model_registry_events` table in Neon, and a pointer file next to the artifacts in R2. The
hourly job must find the champion on every run, without a deploy, and must keep forecasting when
DagsHub is down.

## Decision

- **`models/champion.json` in the private bucket is authoritative.** It holds `model_name`,
  `model_version`, `artifact_key`, `sha256`, `intervals_key`, `feature_schema_version`,
  `champion_status`, `bootstrap_reason`, `promoted_at`, `mlflow_run_id`, and after a gated
  promotion `previous_version`.
- **Who may write it**, and nothing else:
  - `gridcast train`, only when no pointer exists (bootstrap, ADR-0010);
  - the gate, on a promotion or a rollback (ADR-0016), under the `model-registry` concurrency
    group;
  - the daily job, which only swaps `intervals_key` and sets `intervals_recalibrated_on`
    (ADR-0008).
  The weekly retrain never writes it; it writes `shadow.json` or `queued.json` (ADR-0013).
- **How it is read.** Every prediction reads the pointer, downloads the artifact, checks its
  sha256 and caches the model for the rest of the process. A promotion therefore takes effect on
  the next hourly run, with no deploy.
- **The other two are records, not switches.** `model_registry_events` is the append-only
  decision log (trained, backtested, rejected, promoted, rolled back, interval recalibrated) with
  every number in `metrics`. MLflow on DagsHub keeps params, metrics and artifacts for
  experiments. An MLflow outage is a note in the run, never a failure, and never blocks a
  forecast or a promotion.

## Consequences

- Swapping models is a two-file operation in R2 (pointer and previous pointer), logged in Neon.
- A broken pointer or a sha256 mismatch fails the hourly run visibly (Healthchecks `/fail`)
  instead of serving an unknown model.
- `/health` and `latest/meta.json` show `champion_version` and `champion_status` from the pointer.
- The pointer never reaches the public bucket; only its public fields are copied into
  `meta.json`.

## Alternatives rejected

- **MLflow Model Registry stage "Production" as the switch.** Every job would need DagsHub, its
  token and its uptime; a DagsHub outage would stop forecasts.
- **A pointer row in Neon.** Workable, but it splits the pointer from the artifacts it names and
  makes a model swap depend on the database being up.
- **Baking the model into a deploy.** Every promotion would need a build and a release.
