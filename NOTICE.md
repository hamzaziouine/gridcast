# Notices and data attribution

GridCast's source code is licensed under the MIT License (see `LICENSE`). The data it reads and
the data it publishes come from the sources below, under their own terms. This file collects the
attribution each of them requires (BUILD_PLAN §4.8.9).

## Independent Electricity System Operator (IESO)

Demand actuals (`PUB_Demand`, `RealtimeTotals`) and IESO's own demand forecasts (`Adequacy3`) are
from the IESO public reports site, https://reports-public.ieso.ca/public/.

> © Independent Electricity System Operator (IESO). Information reproduced and used under the
> limited licence in the IESO Terms of Use, https://www.ieso.ca/en/Terms-of-Use. The IESO does not
> endorse GridCast and is not responsible for any use made of its information.

Only short excerpts (test fixtures of 7 days or less, and sample rows in `docs/DATA_NOTES.md`) are
committed to this repository; bulk IESO files are not redistributed. GridCast's forecasts and
any value derived from IESO data are GridCast's own estimates, not IESO data.

## Open-Meteo

Weather forecasts (Historical Forecast API and Forecast API, model `gfs_seamless`) are from
Open-Meteo.

> Weather data by Open-Meteo.com (https://open-meteo.com/), licensed under the Creative Commons
> Attribution 4.0 International licence (CC BY 4.0, https://creativecommons.org/licenses/by/4.0/).
> GridCast stores and transforms the data (per-city hourly columns, derived weather features).

GridCast uses the free, non-commercial Open-Meteo API.

## Demo data (Phase D, placeholders)

These sources are not used yet. Step D2 fills in the exact lines, including the per-source
attributions listed in the Overture release used.

- **OpenStreetMap:** _placeholder_ — "© OpenStreetMap contributors", ODbL 1.0
  (https://opendatacommons.org/licenses/odbl/1-0/).
- **Overture Maps Foundation:** _placeholder_ — "Building footprints and heights © OpenStreetMap
  contributors and Overture Maps Foundation, ODbL 1.0", plus the per-source attributions from
  that release's attribution file.
- **City of Toronto:** _placeholder_ — "Contains information licensed under the Open Government
  Licence – Toronto" (3D Massing).
- **Statistics Canada:** _placeholder_ — "Adapted from Statistics Canada, 2021 Census boundary
  files and Geographic Attribute File. This does not constitute an endorsement by Statistics
  Canada of this product."
- **Canada Post:** _placeholder_ — "FSA boundaries contain data from Canada Post Corporation,
  used with permission."
