# GridCast

Ontario electricity demand forecaster: hourly 24-hour-ahead forecasts of IESO Ontario Demand, scored in public against the IESO's own forecast and naive baselines.

**Status: under construction.** Nothing is running in public yet. This README will describe only what is live.

## Local development

Requires [uv](https://docs.astral.sh/uv/) and Python 3.11 (`uv python install 3.11`).

```bash
uv sync
uv run gridcast --help
uv run ruff check
uv run mypy src
uv run pytest
```

## Licence

Code: MIT (see `LICENSE`). Data sources keep their own terms (IESO, Open-Meteo CC BY 4.0); attribution will be listed in `NOTICE.md`.
