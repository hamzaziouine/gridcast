# Test fixtures

Small excerpts of live public files, used by the parser and contract tests. Each covers 7 days
or less. No bulk source file is committed (BUILD_PLAN §3.1).

| File | Source | Content |
|---|---|---|
| `ieso_pub_demand_sample.csv` | IESO `PUB_Demand_2026_v295.csv` | 3 header lines, 2026-10-17 HE1 to 2026-10-22 HE24, plus the provisional 2026-10-23 HE1 |
| `ieso_realtime_sample.csv` | IESO `PUB_RealtimeTotals_2026102314.csv` | one complete hour (12 intervals), CRLF converted to LF |
| `ieso_realtime_partial_sample.csv` | IESO `PUB_RealtimeTotals_2026102311.csv` | a partial hour (2 intervals) from the 2026-10-23 publishing outage |
| `ieso_adequacy_sample.xml` | IESO `PUB_Adequacy3_20261024_v68.xml` | cut to `DocHeader`, `DeliveryDate` and `ForecastOntDemand` (notice in an XML comment) |
| `openmeteo_historical_sample.json` | Open-Meteo Historical Forecast API, `gfs_seamless`, 6 cities | 2018-01-01 and 2018-01-02, 9 hourly variables (`generationtime_ms` removed) |
| `openmeteo_live_sample.json` | Open-Meteo Forecast API, `gfs_seamless`, 6 cities | snapshot fetched 2026-10-23 20:27Z, `past_hours=24`, `forecast_hours=48` |

**IESO notice.** The IESO excerpts in this directory are © Independent Electricity System
Operator (IESO), reproduced under the limited licence in the IESO Terms of Use
(https://www.ieso.ca/en/Terms-of-Use). They are unmodified apart from being cut to a few days.

**Open-Meteo notice.** Weather data by Open-Meteo.com (https://open-meteo.com/), licensed under
CC BY 4.0 (https://creativecommons.org/licenses/by/4.0/).
