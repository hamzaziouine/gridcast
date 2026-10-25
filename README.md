# GridCast

[![GridCast day-ahead forecast vs actual, last 7 days](https://raw.githubusercontent.com/hamzaziouine/gridcast/status/chart_7d.svg)](https://github.com/hamzaziouine/gridcast/tree/status)

<sub>Rebuilt by the daily job at 13:20Z and 18:20Z from the live tables
([ADR-0015](docs/adr/0015-status-branch-and-chart.md)). Every line is the forecast issued 24 h
before the hour; hours in the shaded area have no final actual yet.</sub>

Every hour GridCast forecasts Ontario's electricity demand (IESO **Ontario Demand**, MW) for the
next 24 hours, with an 80 % interval. The model is LightGBM, built from scratch. Each forecast is
scored the next day against the final IESO actuals, next to IESO's own forecast and three naive
baselines. Everything runs unattended on free infrastructure: GitHub Actions cron, Neon Postgres,
Cloudflare R2 and DagsHub MLflow.

## Live

| | |
|---|---|
| Public API | <https://gridcast.hamzaziouine-23.workers.dev>: [`/v1/forecast/latest`](https://gridcast.hamzaziouine-23.workers.dev/v1/forecast/latest), [`/v1/scoreboard`](https://gridcast.hamzaziouine-23.workers.dev/v1/scoreboard), [`/health`](https://gridcast.hamzaziouine-23.workers.dev/health) (Cloudflare Worker in [`infra/worker/`](infra/worker/), deployed by Workers Builds on every push to `main`) |
| Hourly forecasts | Since 2026-10-24 ([`hourly.yml`](.github/workflows/hourly.yml), `:02/:22` from a Cloudflare cron, GitHub cron `:02/:12/:22` as fallback). |
| Daily scoring | First run 2026-10-24 ([`daily.yml`](.github/workflows/daily.yml), 13:20Z and 18:20Z). |
| Weekly retrain + backtest gate | [`retrain.yml`](.github/workflows/retrain.yml), Sunday 03:00Z, at most once per week whichever scheduler starts it ([ADR-0014](docs/adr/0014-cloudflare-cron-dispatches-github-jobs.md)). The first run (2026-10-24) took 13 min and its candidate was rejected (see below). |
| Status branch | [`status`](https://github.com/hamzaziouine/gridcast/tree/status): the chart above, `metrics.json` and a copy of the published JSON. |
| Experiment tracking | [DagsHub MLflow](https://dagshub.com/hamzaziouine/gridcast.mlflow) |

All three endpoints are read from JSON files that the jobs publish to R2
([ADR-0011](docs/adr/0011-public-snapshots-and-worker-api.md)).

## How good is it?

**Live** scoring started on 2026-10-24. On that date only 3 forecast hours had final actuals,
which is too few to quote a MAPE. The daily job writes the live table to
`latest/scoreboard.json` every day: 7- and 30-day windows, with every model scored on the same
hours. It will replace the backtest table below once it covers 7 days. Live numbers are the
ones that count.

**Backtest** (rolling origin, 12 monthly folds 2025-10 … 2026-09, 35,040 forecasts per model,
computed 2026-10-23; [DATA_NOTES §15](docs/DATA_NOTES.md)):

| model | MAPE % | peak-hour MAPE % | 80 % interval coverage |
|---|---:|---:|---:|
| **GridCast LightGBM (champion `lgbm-2026.10.23-49ca`)** | **2.61** | 3.15 | 0.82 (held-out folds) |
| seasonal naive, same hour last week | 6.65 | 8.17 | 0.79 |
| seasonal naive, same hour yesterday | 6.40 | 6.45 | 0.77 |
| persistence (last known hour) | 10.36 | 17.95 | 0.78 |

- **Skill vs. same hour last week:** 61 % (target ≥ 30 %).
- **Bootstrap target** (MAPE ≤ 4.0 %, coverage 0.76–0.84): met, so `champion_status = ok`.
- **IESO's own forecast is not in this table.** GridCast's archive of it starts on 2026-10-23,
  so it is compared live only.

## Honest caveats

- **The backtest is easier than live.** The backtest uses Open-Meteo's *archived* weather
  forecast, which is close to a short-lead forecast. Live forecasts use 24-hour-ahead weather, so
  live MAPE will be higher. Backtest data availability is **modelled**
  ([ADR-0004](docs/adr/0004-availability-model-and-as-of-rule.md)); live availability is
  observed.
- **No input from after the cutoff.** A forecast issued at hour T uses only data available by
  T + 30 min. A late cron run gets older weather, never newer data
  ([ADR-0012](docs/adr/0012-hourly-cron-slots-and-per-hour-success.md)).
- **IESO has more data.** Its forecast uses embedded generation, ICI programs and its own weather
  vendor, none of which GridCast has. The goal is to be within about a point of it, not to beat it.
- **What is scored.** Ontario Demand excludes exports and some embedded-generation effects.
  Scores use the final `PUB_Demand` values, so they arrive a day after the target hour.
- **Intervals.** Until there are 14 days of live errors, the 80 % intervals are backtest residual
  quantiles × 1.15. After that they are recalibrated daily from live errors.
- **Retrained models are gated.** A new model reaches the public forecast only after it is ≥ 2 %
  better on the same backtest folds *and* has spent ≥ 14 days in shadow
  ([ADR-0013](docs/adr/0013-fast-retrain-and-backtest-gate.md)). The first weekly candidate
  scored a MAPE of 2.630 and was rejected.

## How it works

```
IESO reports-public ─┐                          ┌─> Neon Postgres (history, predictions, scores)
Open-Meteo ──────────┼─> hourly.yml / daily.yml ┤
                     │   (gridcast run …)        └─> R2: models/ (private), latest/*.json (public)
                     └─> retrain.yml (weekly) ──> DagsHub MLflow + registry events
                                                   Cloudflare Worker reads latest/*.json (read-only)
```

- **Hourly.**
  - Inputs: IESO's forecast (Adequacy3), RealtimeTotals demand, and an Open-Meteo weather
    snapshot.
  - Output: the champion's 24 forecasts, the baselines and IESO's forecast as of the cutoff, all
    published to `latest/forecast.json`.
- **Daily.**
  - Inputs: the final `PUB_Demand` actuals.
  - Work: scoring, peak hours, daily rollups and drift signals (error-based triggers,
    season-matched PSI, L0 lag parity).
  - Output: `latest/scoreboard.json`.
- **Weekly.** The job trains a candidate and backtests it on the champion's folds. The candidate
  is then registered as shadow, queued or rejected; it is never promoted directly.
- **More detail.** Design decisions are in [`docs/adr/`](docs/adr/). Data facts and every
  measured number are in [`docs/DATA_NOTES.md`](docs/DATA_NOTES.md).

## Run it locally

Requires [uv](https://docs.astral.sh/uv/) and Python 3.11 (`uv python install 3.11`).

```bash
git clone https://github.com/hamzaziouine/gridcast.git && cd gridcast
uv sync
uv run gridcast --help
uv run ruff check && uv run mypy src && uv run pytest   # integration tests use an embedded Postgres
```

No account or secret is needed for this: tests never touch Neon, R2, DagsHub or GitHub.
[`scripts/fresh_clone_check.sh`](scripts/fresh_clone_check.sh) runs exactly these steps with
empty caches and fails above 15 minutes; CI runs it on every push to `main`.

Public URLs live in one file, [`config/urls.toml`](config/urls.toml).

The jobs (`gridcast run hourly|daily|retrain|keepalive`) need the variables listed in
[`.env.example`](.env.example).

## Data attribution and licence

- **Demand data and IESO forecasts:** © Independent Electricity System Operator, used under the
  IESO Terms of Use. The IESO does not endorse GridCast.
- **Weather:** [Open-Meteo.com](https://open-meteo.com/), CC BY 4.0.
- **Full notices:** [`NOTICE.md`](NOTICE.md).
- **Code:** MIT ([`LICENSE`](LICENSE)).
