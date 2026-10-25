# ADR-0015: Orphan `status` branch for the README chart, built and pushed by two jobs

- Status: **Accepted**
- Date: 2026-10-25
- Related: BUILD_PLAN §1.2, §5 (status branch), §6.2 step 6, §13 item 13; ADR-0011 (public
  snapshots), ADR-0014 (schedulers); `src/gridcast/status/`, `.github/workflows/daily.yml`,
  `config/urls.toml`; DATA_NOTES §20.2

## Context

The plan wants a README whose first screen shows a 7-day predicted-vs-actual chart, published by
the daily job to an orphan `status` branch and embedded from
`raw.githubusercontent.com/hamzaziouine/gridcast/status/chart_7d.svg`. Since ADR-0011 the public
record lives in R2 behind the Worker, so the branch is no longer the API's data source; the
question was whether to keep it at all, or serve the SVG from the Worker instead.

## Decision

Keep the plan's status branch, with three changes to how it is written:

1. **Two jobs, split privileges.** The `daily` job (DB and bucket credentials,
   `contents: read`) runs `gridcast status build --out status_out` and uploads the files as an
   artifact. A second job, `status` (`needs: daily`), holds `contents: write` and **no other
   secret**: it downloads the artifact and pushes it. The write token never shares a job with
   the database URL, and the job that can push runs no project code.
2. **Single-commit orphan, force-pushed** (`git init -b status` on the generated files,
   `push --force HEAD:refs/heads/status`). The branch never grows, has no history to rewrite,
   and nothing is ever pushed to `main`.
3. **Contents:** `chart_7d.svg`, `metrics.json` (chart numbers, champion pointer fields, the last
   run of each job, the public URLs), a copy of `latest/forecast.json`, `latest/meta.json` and
   `latest/scoreboard.json` (the plan's "mirror"), and a short `README.md`.

Both daily slots (13:20Z and 18:20Z) rebuild it, also when the run ends `skipped` or `failed`
(the chart reads what is in the database). A status failure never changes the daily run's own
status or heartbeat (`continue-on-error`).

**The chart** (`status/chart.py`, plain SVG, ~8 kB): actual (final IESO Ontario Demand), the
champion's p50 and 80 % band, IESO's forecast and the best of the three naive baselines, all at
one fixed horizon: the forecast issued 24 h before each hour, over the last 7 days plus the next
24 h. The subtitle gives each series' MAPE on the same scored hours and their count. The x axis
is labelled in EST (IESO market time); the y axis does not start at 0 and is labelled in MW.
Its own `<style>` switches colours with `prefers-color-scheme` **including an opaque
background**: the query follows the viewer's OS, not the page, so a transparent chart would put
light text on a white page for an OS-dark / GitHub-light viewer. The attribution line (IESO,
Open-Meteo CC BY 4.0) is inside the image.

**`config/urls.toml`** is the one place for public URLs (plan §4.8.8). The status build reads it
(chart footer, `metrics.json`); a unit test checks that README and the Worker use the same
values.

## Consequences

- README image URL is stable (`raw.githubusercontent.com/.../status/chart_7d.svg`); GitHub's
  image proxy caches it for minutes, so the chart can lag a push slightly.
- A daily commit on a branch is also repository activity, a second line behind the keepalive
  enable call for GitHub's 60-day rule (not relied on, plan §6.7).
- One more job per daily run (~15 s). `contents: write` exists in exactly one job of one
  workflow.

## Alternatives rejected

- **Serve the SVG from the Worker/R2** (`/v1/chart.svg`): no write permission needed, but the
  plan names the branch, the branch gives a readable mirror on GitHub itself with no Worker in
  the path, and `contents: write` is contained by the two-job split. Worth revisiting only if
  the branch push ever fails repeatedly.
- **Append commits instead of force-pushing an orphan**: an unbounded binary-ish history for
  files that are regenerated daily.
- **matplotlib SVG** (plan §5): ~10× larger, fonts embedded as paths, no dark-mode switch, and a
  heavy dependency for one chart.
- **Shortest-horizon forecasts** (each hour's freshest forecast): would make every line look
  better than the 24 h-ahead product; one fixed 24 h horizon for every series is the honest view.
