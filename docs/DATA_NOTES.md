# Data notes

First-hand inspection of every live source GridCast reads (step P0.5). All checks were run on
**Fri 2026-10-23 between 19:40Z and 19:55Z (14:40–14:55 EST)** with plain `curl` / Python
`urllib`. No account was needed.

Time notation: `Z` = UTC; `EST` = IESO market time, UTC−5 all year (ADR-0002). IESO's directory
listings, `Created at` lines and `CreatedAt` elements are all in EST (evidence in §2 and §3).

> **IESO notice.** The IESO excerpts below (a few header lines and rows) are © Independent
> Electricity System Operator and are reproduced under the limited licence in the IESO Terms of
> Use. No bulk IESO file is committed to this repository.
>
> **Open-Meteo notice.** Weather data by Open-Meteo.com, CC BY 4.0.

---

## 0. Contradictions with BUILD_PLAN

Each item states what the plan says, what the live source shows, and what has to change. The
plan itself (`docs/internal/BUILD_PLAN.md`) was **not** edited.

1. **RealtimeTotals has no Ontario Demand column (§3.1, §6.1 step 3).**
   Plan: expect `Date, Hour, Interval, Market Total Load, Ontario Load, ...`; provisional hourly
   actual = mean of the 12 intervals.
   Live: the columns are `HOUR,INTERVAL,TOTAL ENERGY,TOTAL LOSS,TOTAL LOAD,TOTAL DISP LOAD SCHED OFF,TOTAL 10S,TOTAL 10N,TOTAL 30R,FLAG`.
   There is no date column (the date is in the file name and the `CREATED AT … FOR yyyy/mm/dd` line)
   and no Ontario Demand.
   Measured on all 24 hours of 2026-10-22: `mean(TOTAL ENERGY)` over the 12 intervals equals
   `PUB_Demand` **Market Demand** exactly (24/24 hours after rounding). `mean(TOTAL LOAD)` is
   4–15 % *above* Ontario Demand (ratio 1.043–1.146), so it is not the target either.
   **Consequence:** a provisional *Ontario Demand* actual cannot be read from this file. Options for
   the builder (decision needed before C2/C4):
   - (a) Derive it: `Ontario ≈ mean(TOTAL ENERGY) − scheduled exports (IntertieScheduleFlow) + c`.
     On 2026-10-22 `Market − Ontario − Σ scheduled exports` was −77 to −142 MW (mean ≈ −104), so
     `c ≈ +104 MW` with a ±30 MW spread on one day. This is an **unverified approximation**. It would
     have to be validated over weeks and labelled "provisional (derived)".
   - (b) Use Market Demand as a real-time *feature* only (`L0_market`), and keep scoring on
     `PUB_Demand` Ontario Demand. This is the plan's own "RealtimeTotals is daily too" branch for
     *scoring*: provisional scores are dropped, and scores arrive once a day.
   - (c) Drop RealtimeTotals entirely, so `L0` comes from `PUB_Demand` (8–32 h lag).
   The cadence part of the plan is otherwise **better** than feared (item 2).

2. **RealtimeTotals cadence is every 5 minutes, and a complete hour is posted before the hour ends.**
   Plan: "posted roughly hourly around :54 (unverified)".
   Live: there is one file per hour-ending, `PUB_RealtimeTotals_YYYYMMDDHH.csv` (HH = HE 01–24).
   It is re-posted every 5 minutes as `_v1 … _v12`, and an unsuffixed copy always holds the latest
   version. The file holding all 12 intervals is posted at **:53–:57 of the same hour**, i.e. 53–57 min
   after `target_time` (the interval start) and 3–7 min *before* the hour ends. The values are
   dispatch-schedule totals (a file created at 14:52:57 EST already carries interval 12,
   14:55–15:00). "Around :54" is right for the *complete* file, but the feed is not hourly.
   → provisional **`REALTIME_LAG_H = 1`** (§1.4), not the default 2.

3. **Adequacy3 horizon and reissue cadence (§3.2, §6.1 step 0).**
   Plan: files go "up to ~10 days ahead" and are reissued "about every 30 minutes", so the
   benchmark snapshot is "at most ~30–60 min older than our own inputs".
   Live: delivery dates run up to **2026-11-26 (~34 days ahead)**. A date gets its first file about
   34 days before, then about 2 issues a day (~09:2x and ~15:5x EST). On D−1 it gets 3 issues
   (04:27, 07:57, 12:57 EST). Only from about **20:28 EST on D−1** is it reissued every ~30 min, at
   about :25–:28 and :53–:57. For an issue time during the day on D, the target hours that fall on
   D+1 therefore have an IESO snapshot that is **up to ~7.5 h old** (for example, at 12:00 EST the
   newest file for D+1 is the 07:57 issue).
   **Consequence:** the ">60 min staleness ⇒ excluded from headline M3" rule would exclude most D+1
   target hours of daytime issues, which is roughly half of all target hours. The rule needs
   rethinking (for example, staleness measured against IESO's own issue schedule, or M3 reported
   per lead bucket). The fairness rule itself (latest snapshot ≤ `data_cutoff`) still holds.

4. **The §3.1 damaged-hour filter is a no-op on the target series.**
   Plan §3.1 lists post-Market-Renewal damage (2025-04-30 HE24 = 4,598 MW; 12 hours with
   |Diff| > 1,500 MW). Those values come from the **zonal** file's `Ontario Demand` column.
   `PUB_Demand` (the target) has **13,795 MW** for 2025-04-30 HE24, which is plausible.
   Running the plan's filter (< 50 % or > 150 % of the same hour ±1 week) on `PUB_Demand` flags
   **0 hours** in 2025 and 0 in 2026.
   Separately, `PUB_Demand` and `PUB_DemandZonal` disagree on `Ontario Demand` in **22 hours of 2025**
   and **1 hour of 2026** (table in §4.3). The disagreements are mostly +1,100…+3,200 MW in the
   zonal file. Which file is right is not known; the filter would not catch either version.
   → C2 should log these disagreeing hours as "suspect" in this file, rather than rely on the
   ±50 % filter alone.

5. **Open-Meteo historical-forecast starts earlier than planned (§3.4).**
   Plan: "roughly 2021/2022 onward". Live: `best_match` at Toronto has data from **2018-01-01T00:00Z**
   for all 9 planned variables. At Toronto, `best_match` **is `gfs_seamless`**, identically, in the
   historical API (checked 2021, 2023, 2025) and in the live Forecast API (72/72 h on 2026-10-23).
   This is not a blocker; C3's 2022-01-01 start is still fine. Recommendation: request
   `models=gfs_seamless` explicitly in both APIs, so that a silent change of `best_match` cannot
   create a train/serve skew.

6. **IESO-wide publishing gap on 2026-10-23 ~10:00–12:25 EST.** This is not a plan contradiction,
   but it is evidence for R-risks. In that window:
   - RealtimeTotals HE11 stopped at 2 intervals (file posted 10:06 EST), and HE12 is **404**
     (still missing at 14:54 EST).
   - IntertieScheduleFlow skipped 10:32 and 11:32.
   - Adequacy3 had no issue between 09:55 and 12:25.

   The plan's "never use an hour with < 12 intervals" rule is needed. Also, `_vN` numbering is not
   an interval count: 2026-10-22 HE22 lists only up to `_v7`, but its final file has 12 intervals.

7. **RealtimeTotals retention is about 1 month.** The listing is complete only from 2026-09-22 (a few
   stray files go back to 2026-07-07). Modelled `realtime_totals` stamps can therefore only exist
   for about the last 30 days, so any historical `L0` from this source must come from somewhere
   else. The plan does not state this.

Plan facts **confirmed**: `PUB_Demand` format, junk lines, the 07:30 EST daily posting and
`D+1 13:00Z` slack; the 2025-05-01 HE1 gap; Adequacy3 `ForecastOntDemand/…/EnergyMW` path;
`CreatedAt` in fixed EST (with a July proof); ~120 versions per delivery date; about 3 months of
Adequacy3 history online; the `PUB_DemandZonal` header, sample row and zones; zonal posting at
~08:00 EST.

---

## 1. RealtimeTotals — `https://reports-public.ieso.ca/public/RealtimeTotals/`

### 1.1 Files

- `PUB_RealtimeTotals.csv` / `.xml`: the latest version of the current hour.
- `PUB_RealtimeTotals_YYYYMMDDHH.csv` / `.xml`: one per hour-ending (HH = 01–24, EST date); always
  the latest version of that hour.
- `PUB_RealtimeTotals_YYYYMMDDHH_vN.csv` / `.xml`: N = 1…12, one per 5-minute re-post.
- The listing held 760 distinct hours on 2026-10-23. It is complete (24 h/day) from about
  2026-09-23; before that only a few stray hours exist (2026-07-07, 07-17, 08-31, 09-15).

Listing excerpt (directory times are EST):

```
PUB_RealtimeTotals_2026102314_v1.csv   23-Oct-2026 13:02  269
PUB_RealtimeTotals_2026102314_v2.csv   23-Oct-2026 13:07  326
...
PUB_RealtimeTotals_2026102314_v11.csv  23-Oct-2026 13:52  847
PUB_RealtimeTotals_2026102314_v12.csv  23-Oct-2026 13:57  905
PUB_RealtimeTotals_2026102314.csv      23-Oct-2026 13:57  905
```

### 1.2 Format (sample: `PUB_RealtimeTotals_2026102214.csv`, final)

```
PM 20261022 	NORMAL DISPATCH TOTALS;

RTEM_TOTALS;
\\CREATED AT 2026/10/22 13:52:59 FOR 2026/10/22
HOUR,INTERVAL,TOTAL ENERGY,TOTAL LOSS,TOTAL LOAD,TOTAL DISP LOAD SCHED OFF,TOTAL 10S,TOTAL 10N,TOTAL 30R,FLAG
14,1,17146.9,406,16740.9,167.3,390.6,883,144.4,DSO-RD
14,2,17246.6,408.6,16838,266.3,381.6,900.6,135.8,DSO-RD
14,3,17168,397.5,16770.5,167.3,391.7,882.9,143.4,DSO-RD
...
14,11,17179.4,410.4,16769,167.3,402.4,873.5,142.1,DSO-RD
14,12,17218.7,412.7,16806,369.4,386.2,895.1,136.6,DSO-RD
```

Parser notes:
- There are 4 preamble lines (one blank, one with a tab).
- `HOUR` is hour-ending.
- The date comes from the file name or the `FOR` part of the `CREATED AT` line.
- Keep only rows starting with a digit.

### 1.3 What the columns mean (2026-10-22, all 24 hours, 12/12 intervals each)

| HE | mean TOTAL ENERGY | PUB Market Demand | mean TOTAL LOAD | PUB Ontario Demand | LOAD / Ontario |
|---:|---:|---:|---:|---:|---:|
| 1 | 16200 | 16200 | 15890 | 13961 | 1.138 |
| 9 | 17726 | 17726 | 17314 | 16346 | 1.059 |
| 14 | 17149 | 17149 | 16745 | 15972 | 1.048 |
| 19 | 19788 | 19788 | 19425 | 17142 | 1.133 |
| 24 | 15840 | 15840 | 15534 | 14161 | 1.097 |

Summary:
- `mean(TOTAL ENERGY)` equals Market Demand in **24/24** hours.
- `TOTAL LOAD / Ontario Demand` has a mean of 1.096 and ranges from 1.043 to 1.146.
- See Contradiction 1.

Export check: `Market − Ontario − Σ scheduled exports` was computed from
`/public/IntertieScheduleFlow/PUB_IntertieScheduleFlow_20261022.xml`, summing
`IntertieZone/Schedules/Schedule/Export` over the 14 intertie zones. Result: −77 to −142 MW per
hour, mean ≈ −104 MW.

IntertieScheduleFlow facts:
- It is posted hourly at about :32–:33 EST as `PUB_IntertieScheduleFlow_YYYYMMDD_vN.xml` (12
  versions by 14:33 EST on 2026-10-23).
- Its `CreatedAt` is `2026-10-23T14:30:47` for a file listed at 14:33, so it is EST.
- The namespace is `http://www.theIMO.com/schema`. It also carries 5-minute `Actuals/…/Flow`.

### 1.4 Posting cadence and `REALTIME_LAG_H`

One probe run (`scripts/observe_realtime_cadence.py --once --lookback 9`) at 19:54:02Z:

| file | HTTP | Last-Modified (Z) | CREATED AT (EST) | intervals | target_time (Z) | LM − target |
|---|---|---|---|---:|---|---:|
| …2026102315.csv | 200 | 19:53:22 | 14:52:57 | 12 | 19:00 | 53.4 min |
| …2026102314.csv | 200 | 18:57:02 | 13:53:08 | 12 | 18:00 | 57.0 min |
| …2026102313.csv | 200 | 17:53:18 | 12:52:58 | 12 | 17:00 | 53.3 min |
| …2026102312.csv | **404** | – | – | 0 | 16:00 | – |
| …2026102311.csv | 200 | 15:06:43 | 10:03:08 | **2** | 15:00 | 6.7 min |
| …2026102310.csv | 200 | 14:53:18 | 09:52:59 | 12 | 14:00 | 53.3 min |
| …2026102306…09 | 200 | hh:53:17–20 | hh−1:52:5x EST | 12 | – | 53.3 min |

Additional evidence:
- The directory listing for 2026-10-22: the final (`_v12` or unsuffixed) file was posted at **:53 for
  22 hours and :57 for 2 hours** (one of them HE19). HE22 is listed only up to `_v7`, but its final file has 12
  intervals.
- An earlier HEAD request on `PUB_RealtimeTotals.csv`: `Last-Modified: 19:42:11Z` for HE15 v9,
  `CREATED AT 14:38:11` EST.
- `Last-Modified − CREATED AT` = 0.5–4 min, and `CREATED AT` is EST. This was checked live: the
  latest file was 19:53Z = 14:53 EST.

**Provisional `REALTIME_LAG_H = 1`.**
- Definition from the plan: `available_at = target_time + 1 h + d`, with `d` = p90 posting delay after
  the hour ends.
- Observed: complete files arrive 3–7 min *before* the hour ends, so `d ≤ 0` and is rounded to 0.
- At `issue_time` T, the hour `[T−1h, T)` is complete by T−3 min, so it qualifies under
  `data_cutoff = T + 30 min`.
- Fall back to **2** if the day-long observation below shows either of these:
  - any hour completing later than `target_time + 1 h`;
  - a missing or partial hour (like HE11/HE12 today) being back-filled later, because a modelled
    stamp would then be optimistic.

The constant only matters if Contradiction 1 is resolved in favour of option (a) or (b).

**Cadence must still be observed over a full day** (P0.5 "done when"). Run
`python scripts/observe_realtime_cadence.py --out realtime_cadence.csv` for ≥ 24 h, then stop it.
Paste the p50/p90 of `lag_vs_target_min` for 12-interval files, plus the list of 404 or partial
hours, here:

- Observation window: _TODO_
- p50 / p90 / max `lag_vs_target_min` (complete files): _TODO_
- Missing / partial hours and whether they were back-filled: _TODO_
- Final `REALTIME_LAG_H`: _TODO (provisional 1)_

---

## 2. `PUB_Demand` — `https://reports-public.ieso.ca/public/Demand/`

### 2.1 Current-year file `PUB_Demand.csv`

HTTP headers at 19:40:35Z:

```
Last-Modified: Fri, 23 Oct 2026 12:32:17 GMT
Content-Length: 181565
Content-Type: text/csv
```

First lines (3 junk lines verbatim, then the header and the first 3 rows):

```
\\Hourly Demand Report,,,
\\Created at 2026-10-23 07:30:09,,,
\\For 2026,,,
Date,Hour,Market Demand,Ontario Demand
2026-01-01,1,19489,16526
2026-01-01,2,19317,16374
2026-01-01,3,19326,16242
```

Last 3 rows:

```
2026-10-22,23,16778,14858
2026-10-22,24,15840,14161
2026-10-23,1,14664,14021
```

**Which hours the 07:30 file carries:**
- every hour from 2026-01-01 HE1 to **yesterday HE24**;
- **plus today's HE1**: 7,081 rows = 295 × 24 + 1;
- every 2026 date has 24 rows, including DST day 2026-03-08.

**Today's HE1 is provisional and gets revised.** In the 2026-10-22 file, 2026-10-22 HE1 was
`16153,13912`. The 2026-10-23 file has `16200,13961` for the same hour (+49 MW Ontario).

Timing:
- `Created at 07:30:09` EST, `Last-Modified 12:32:17Z` (= 07:32 EST), so the header time is EST.
- The plan's modelled `available_at = D+1 13:00Z` leaves **28 min slack** over the observed posting
  time.
- Today's HE1 is available earlier than modelled (`D+1 12:32Z` for HE1 of `D+1`). The rule ignores
  that, which is conservative.

Directory listing (EST):

```
PUB_Demand.csv            23-Oct-2026 07:32  177K
PUB_Demand_2026.csv       23-Oct-2026 07:32  177K
PUB_Demand_2026_v293.csv  21-Oct-2026 07:32  176K
PUB_Demand_2026_v294.csv  22-Oct-2026 07:33  177K
PUB_Demand_2026_v295.csv  23-Oct-2026 07:32  177K
PUB_Demand_2025.csv       31-Jan-2026 07:31  219K
PUB_Demand_2025_v396.csv  31-Jan-2026 07:31  219K
```

`PUB_Demand.csv` = `PUB_Demand_2026.csv` = the newest `_vNNN`. Only the last ~3 versions of the
current year are listed. The 2025 file was finalised on 2026-01-31.

### 2.2 Yearly archive `PUB_Demand_2025.csv`

```
\\Hourly Demand Report,,,
\\Created at 2026-01-31 07:30:13,,,
\\For 2025,,,
Date,Hour,Market Demand,Ontario Demand
2025-01-01,1,17247,13887
2025-01-01,2,17355,13722
2025-01-01,3,17638,13688
...
2025-12-31,22,21290,17789
2025-12-31,23,20515,17415
2025-12-31,24,20042,16946
```

**The file has 8,759 rows = 8,760 − 1.** The only date without 24 rows is **2025-05-01 (23 rows)**,
which has no HE1:

```
2025-04-30,23,16922,14459
2025-04-30,24,15320,13795
2025-05-01,2,13153,12352
2025-05-01,3,13055,12249
```

---

## 3. Adequacy3 — `https://reports-public.ieso.ca/public/Adequacy3/`

### 3.1 Files and cadence

- `PUB_Adequacy3_YYYYMMDD.xml` is the latest version for a delivery date, and
  `PUB_Adequacy3_YYYYMMDD_vN.xml` are all versions.
- The listing has a separate `PUB_Adequacy3.xml` (listed 09:42 EST).
- On 2026-10-23 the listing held 5,545 files for **126 delivery dates, from 2026-07-24 to 2026-11-26**.

Versions per delivery date:

| Delivery date | Versions | First issue (EST) | Last issue (EST) |
|---|---:|---|---|
| 2026-10-22 | 120 | 2026-09-18 09:32 | 2026-10-22 23:57 |
| 2026-10-23 | 97 (so far) | 2026-09-19 09:30 | 2026-10-23 14:26 |
| 2026-10-24 | 68 | 2026-09-20 09:30 | 2026-10-23 12:57 |
| 2026-11-26 | 1 | – | – |

Issue times on D−1 for delivery date 2026-10-23 (EST):

```
10-22 04:27, 07:57, 12:57, 20:28, 21:27, 22:28, 23:26,
10-23 00:27, 00:53, 01:27, 01:56, 02:27, 02:55, …, 09:08, 09:42, 09:55, [gap], 12:25, 12:56, 13:27, 13:53, 14:26
```

So the file is reissued every ~30 min only from ~20:28 EST on D−1 onward (Contradiction 3).
Note for §6.1 step 0: the reissues land at :25–:28 and :53–:57, so the :27 cron slot can race the
:25–:28 issue; the next :07 slot picks it up.

### 3.2 XML element paths (from `PUB_Adequacy3_20261024.xml`, v68)

The default namespace is `http://www.ieso.ca/schema`; `ET` needs `{http://www.ieso.ca/schema}`
prefixes. The root is `Document`.

```
/Document/DocHeader/CreatedAt                                   2026-10-23T12:53:16
/Document/DocBody/DeliveryDate                                  2026-10-24
/Document/DocBody/ForecastDemand/OntarioDemand/ForecastOntDemand/Demand/{DeliveryHour,EnergyMW}   ← benchmark (24)
/Document/DocBody/ForecastDemand/OntarioDemand/PeakDemand/Demand/{DeliveryHour,EnergyMW}
/Document/DocBody/ForecastDemand/OntarioDemand/AverageDemand/Demand/{DeliveryHour,EnergyMW}
/Document/DocBody/ForecastDemand/OntarioDemand/AreaPeakDemand/{Northwest,Northeast,Southeast,Southwest}PeakDemand/Demand/{DeliveryHour,PkDemand}
/Document/DocBody/ForecastDemand/OntarioDemand/AreaAverageDemand/{Northwest,Northeast,Southeast,Southwest}AverageDemand/Demand/{DeliveryHour,AvgDemand}
/Document/DocBody/ForecastDemand/OntarioDemand/{WindEmbedded,SolarEmbedded}/Embedded/{DeliveryHour,EnergyMW}
/Document/DocBody/ForecastDemand/ZonalExports/TotalExports/Schedules/Schedule/{DeliveryHour,EnergyMW}
```

Other `ForecastDemand` children: `RampStatuses`, `GenerationReserveHoldback`, `TotalRequirements`,
`Excess*`, `Unscheduled*`, `DispatchableLoad`, `HourlyDemandResponse`. `DocBody` also has
`ForecastSupply`.

Sample values (v68):
- HE1–3 `ForecastOntDemand` = 13983, 13634, 13592. `AverageDemand` is identical to
  `ForecastOntDemand` here; `PeakDemand` HE1 = 14070.
- HE19 `ForecastOntDemand` = 16185.
- HE19 area averages / peaks:

  | Area | Avg | Peak |
  |---|---:|---:|
  | Northwest | 458 | 460 |
  | Northeast | 1358 | 1361 |
  | Southeast | 8882 | 8896 |
  | Southwest | 5487 | 5544 |

  Note the element names: areas use `PkDemand` / `AvgDemand`, not `EnergyMW`.

### 3.3 `CreatedAt` timezone evidence — fixed EST confirmed

| File | `CreatedAt` | HTTP `Last-Modified` | LM − CreatedAt if EST | if EDT |
|---|---|---|---|---|
| `PUB_Adequacy3_20261024.xml` (v68, October) | 2026-10-23T12:53:16 | Fri, 23 Oct 2026 17:57:00 GMT | +3 min 44 s | −56 min (impossible) |
| `PUB_Adequacy3_20260724_v118.xml` (**July**, DST season) | 2026-07-24T23:48:50 | Sat, 25 Jul 2026 04:49:31 GMT | **+41 s** | −59 min (impossible) |

The July case proves no DST shift: `CreatedAt` is UTC−5 all year. The directory listing times
(e.g. `24-Jul-2026 23:49`) are EST too. The plan's unit-test examples (`10:31:05 → 15:31:05Z`, and
a July case with no shift) are correct.

---

## 4. `PUB_DemandZonal` — `https://reports-public.ieso.ca/public/DemandZonal/`

### 4.1 Current file

`Last-Modified: Fri, 23 Oct 2026 13:02:17 GMT` (= 08:02 EST).

```
\\Hourly Zonal Demand Report,,,,,,,,,,,,,,
\\Created at 2026-10-23 08:00:15,,,,,,,,,,,,,,
\\For 2026,,,,,,,,,,,,,,
Date,Hour,Ontario Demand,Northwest,Northeast,Ottawa,East,Toronto,Essa,Bruce,Southwest,Niagara,West,Zone Total,Diff
2026-01-01,1,16526,688,1474,1049,1085,5568,1219,96,3000,569,1949,16697,171
...
2026-10-22,23,14858,412,1254,939,875,5186,1006,83,3336,522,1620,15232,374
2026-10-22,24,14161,397,1250,878,827,4885,954,82,3200,501,1600,14573,412
```

**The 10 zones:** Northwest, Northeast, Ottawa, East, Toronto, Essa, Bruce, Southwest, Niagara,
West.

Coverage:
- The file ends at yesterday HE24 (7,080 rows) and does **not** carry today's HE1, unlike
  `PUB_Demand`.
- It is posted ~08:00 EST, so the plan's "8–32 h old" holds.

### 4.2 Cutover rows in `PUB_DemandZonal_2025.csv` (`Created at 2026-01-31 08:00:18`)

```
2025-04-30,23,14459,556,1320,789,883,5097,907,100,2796,464,1566,14477,18
2025-04-30,24,4598,534,1303,734,815,4809,839,99,2655,481,1529,13799,9201
2025-05-01,2,12352,522,1336,689,754,4438,776,96,2468,480,1499,13058,706
```

This shows the same 2025-05-01 HE1 gap. The bad Ontario Demand (4,598, Diff 9,201) appears **only
in the zonal file**.

### 4.3 `Ontario Demand`: `PUB_Demand` vs `PUB_DemandZonal` disagreements

| Date | HE | PUB_Demand | Zonal | Zonal − PUB |
|---|---:|---:|---:|---:|
| 2025-04-30 | 24 | 13795 | 4598 | −9197 |
| 2025-05-01 | 9 | 15317 | 17870 | +2553 |
| 2025-05-05 | 14 | 14805 | 16039 | +1234 |
| 2025-05-13 | 13 | 15523 | 16817 | +1294 |
| 2025-05-20 | 8 | 14855 | 16093 | +1238 |
| 2025-06-18 | 11 | 19865 | 21520 | +1655 |
| 2025-06-30 | 12 | 19021 | 22191 | +3170 |
| 2025-06-30 | 13 | 19565 | 21195 | +1630 |
| 2025-07-10 | 13 | 21305 | 23081 | +1776 |
| 2025-07-21 | 3 | 13369 | 15597 | +2228 |
| 2025-07-21 | 4 | 13253 | 15462 | +2209 |
| 2025-07-21 | 6 | 13978 | 15143 | +1165 |
| 2025-07-21 | 7 | 15313 | 16589 | +1276 |
| 2025-07-21 | 8 | 16058 | 17397 | +1339 |
| 2025-07-22 | 9 | 16322 | 17682 | +1360 |
| 2025-07-24 | 10 | 20550 | 22262 | +1712 |
| 2025-07-24 | 18 | 24524 | 26567 | +2043 |
| 2025-08-05 | 10 | 18795 | 20361 | +1566 |
| 2025-08-13 | 11 | 21016 | 22767 | +1751 |
| 2025-09-03 | 3 | 13188 | 14287 | +1099 |
| 2025-09-03 | 6 | 14567 | 15781 | +1214 |
| 2025-09-11 | 1 | 13498 | 12373 | −1125 |
| 2026-03-20 | 1 | 15232 | 13962 | −1270 |

Every other hour of 2025 and 2026 matches exactly. The plan's ±50 % week-over-week filter flags
none of these hours in `PUB_Demand` (Contradiction 4).

---

## 5. Open-Meteo Historical Forecast API

### 5.1 Earliest available date for Toronto

Exact request (one call, 2016–2024, the 9 variables the plan uses):

```
https://historical-forecast-api.open-meteo.com/v1/forecast?latitude=43.65&longitude=-79.38&start_date=2016-01-01&end_date=2024-12-31&hourly=temperature_2m,apparent_temperature,relative_humidity_2m,dew_point_2m,wind_speed_10m,cloud_cover,shortwave_radiation,precipitation,snowfall&timezone=GMT
```

The API returns a time axis back to the requested start, with `null` values before the data
begins. The first non-null values were:

| Variable | First non-null (UTC) | Nulls after it (to 2024-12-31) |
|---|---|---:|
| temperature_2m | 2018-01-01T00:00 | 0 |
| apparent_temperature | 2018-01-01T00:00 | 0 |
| relative_humidity_2m | 2018-01-01T00:00 | 0 |
| dew_point_2m | 2018-01-01T00:00 | 0 |
| wind_speed_10m | 2018-01-01T00:00 | 0 |
| cloud_cover | 2018-01-01T00:00 | 0 |
| shortwave_radiation | 2018-01-01T01:00 | 0 |
| precipitation | 2018-01-01T01:00 | 0 |
| snowfall | 2018-01-01T01:00 | 0 |

The grid cell resolves to 43.646603, −79.38272, elevation 87 m.

### 5.2 Which model `best_match` is

The first-date probe per model used this request:

```
https://historical-forecast-api.open-meteo.com/v1/forecast?latitude=43.65&longitude=-79.38&start_date=2016-01-01&end_date=2024-12-31&hourly=temperature_2m&models=best_match,gem_seamless,gfs_seamless,ecmwf_ifs025,ecmwf_ifs,icon_seamless&timezone=GMT
```

| Model | First non-null temperature_2m |
|---|---|
| best_match | 2018-01-01T00:00 |
| gfs_seamless | 2018-01-01T00:00 |
| ecmwf_ifs | 2017-01-01T00:00 |
| icon_seamless | 2022-11-16T08:00 |
| gem_seamless | 2022-11-23T00:00 |
| ecmwf_ifs025 | 2024-02-03T00:00 |

Comparison with `gfs_seamless`:
- `best_match` was compared with `gfs_seamless` over 2021-06-01…14, 2023-06-01…14 and
  2025-06-01…14. It was identical in **336/336** hours each time. It matched `gem_seamless` in 0, 15
  and 11 hours.
- Live Forecast API on 2026-10-23
  (`https://api.open-meteo.com/v1/forecast?latitude=43.65&longitude=-79.38&hourly=temperature_2m&models=best_match,gfs_seamless,gem_seamless&past_hours=24&forecast_hours=48&timezone=GMT`):
  `best_match` = `gfs_seamless` in **72/72** hours (2026-10-22T19:00 … 2026-10-25T18:00).

Recommendation: pin `models=gfs_seamless` (Contradiction 5).

### 5.3 Usage and limits

Five historical calls and one live call were made in total, far below the 10,000/day non-commercial
limit. Long ranges count as several calls (they are weighted by days × variables), so C3 should
backfill by month as planned.

---

## 6. Availability rules (inputs to ADR-0004 and `ingest/availability.py`)

| Source | `available_at` (modelled, backfill) | P0.5 evidence | Slack |
|---|---|---|---|
| `pub_demand` | `D+1 13:00Z` for every hour of EST date D | posted 07:30–07:33 EST = 12:30–12:33Z | ≈ 27–30 min |
| `realtime_totals` | `target_time + REALTIME_LAG_H`, **provisional 1 h** | complete file at target + 53–57 min | 3–7 min (only ~30 days of history exist) |
| weather, historical forecast | `valid_time − 24 h` (synthetic) | not observable | optimistic by design (R11, S4) |
| weather, live snapshot | `fetched_at` | – | 0 |
| `ieso_adequacy` | `CreatedAt` parsed as EST → UTC | LM − CreatedAt = 0.7–3.7 min | n/a (observed) |

The live slack `fetched_at − modelled available_at` must be ≥ 0. Record it here once C10 runs.

## 7. Not checked in this pass

- **Live Neon CU-hour quota** (P0.5 item). It needs the Neon console after account setup (§9.0).
  _TODO: record the value shown on the project's Usage page._
- `RealtimeDemandZonal` (not used by Core; §3.9b of the plan).
- RealtimeTotals cadence over a full day (§1.4 TODO).

---

## 8. C2 — PUB_Demand backfill (2026-10-23, ~20:30Z)

`scripts/backfill_demand.py` loaded the newest version of each year (2015–2026) into Neon with
modelled stamps. The listing names the newest version per year explicitly (`_v1` for 2015–2017,
`_v253` for 2018, `_v395`–`_v397` for 2019–2025, `_v295` for 2026), so `file_version` is recorded
for every row.

| file | rows parsed | rows inserted |
|---|---:|---:|
| `PUB_Demand_2015_v1.csv` … `2017_v1` | 8,760 / 8,784 / 8,760 | all |
| `PUB_Demand_2018_v253.csv` … `2024_v395` | 8,760 or 8,784 | all |
| `PUB_Demand_2025_v396.csv` | 8,759 | 8,759 |
| `PUB_Demand_2026_v295.csv` | 7,081 | 7,080 (today's provisional HE1 not stored, see below) |

- `actuals` = **103,511** rows = 8,760 × 11 years + 3 leap days × 24 − 1 + 295 days × 24, span
  2015-01-01 05:00Z … 2026-10-23 04:00Z. The **only missing hour is 2025-05-01 05:00Z**
  (2025-05-01 HE1, the Market Renewal cutover). Every other year has 8,760/8,784 rows.
- Re-running the backfill inserted 0 rows and left every `available_at` unchanged (output in §8.3).

### 8.1 Version and availability rules as built (`ingest/ieso_demand.py`)

- A `raw_demand_history` row is written only when the hour's latest stored version differs in
  Market or Ontario Demand.
- Backfill (`modelled`): the first version of an hour gets `D+1 13:00Z`. A row whose modelled stamp
  is after our fetch time is **not stored** — this only ever hits today's provisional HE1, which the
  next file revises (2026-10-21 HE1: 13,721 → 13,783 MW, +62; 2026-10-22 HE1: 13,912 → 13,961, +49;
  diff of `_v293`/`_v294`/`_v295` shows that the provisional HE1 is the only value that changes
  between consecutive daily versions).
- A **revision** of an already-stored hour is stamped `observed` at our fetch time in both modes:
  we know only that it changed by then. Earlier versions keep their stamps.
- Live daily ingest (`observed`, C11) stamps every new or changed row with the fetch time.
- **For C6:** a PUB row's `available_at` is the Ontario Demand stamp. Its Market Demand column is
  available under `availability.market_demand_available_at` (target + `REALTIME_LAG_H`, plan §3.0
  decision 1); apply that rule to the column, not the row stamp.

### 8.2 Damaged-hour check on PUB_Demand (2015–2026)

The plan's ±50 % week-over-week rule flags **2 hours** on `PUB_Demand` over 2015–2026:
2018-09-16 HE14 (19,504 MW) and HE15 (19,838 MW). They are **real**: a hot Sunday (Market Demand
21,430/21,862 MW in the same rows, a smooth ramp 19,170 → 20,338 MW over HE13–16) between two
mild Sundays (12,829 and 12,963 MW at HE14). The ratio is 1.52, just over the 1.5 limit. They are
kept. This confirms plan §3.0 decision 4: the filter is not applied to the target series (it would
delete genuine heat-wave hours); it applies to zonal data only. Suspect zonal-vs-PUB hours stay
listed in §4.3.

### 8.3 Idempotency check (second run, same day)

```
PUB_Demand_2015_v1.csv: parsed=8760 inserted=0 actuals_changed=0
...
PUB_Demand_2025_v396.csv: parsed=8759 inserted=0 actuals_changed=0
PUB_Demand_2026_v295.csv: parsed=7081 inserted=0 actuals_changed=0
files: 12, inserted: 0
raw_demand_history rows: 103511
actuals rows: 103511 (span 2015-01-01 05:00:00+00:00 .. 2026-10-23 04:00:00+00:00, 103512 hours)
missing hours in span: 1
  missing 2025-05-01 05:00:00+00:00
```

The integration test `tests/integration/test_ieso_demand_db.py` asserts the same on the fixture
(second run: 0 rows, every `available_at` unchanged) plus the revision path. Neon size after C2:
`raw_demand_history` 25 MB, `actuals` 11 MB, `pg_database_size` 45 MB.

---

## 9. C3 — Weather storage check and layout decision (2026-10-23)

Plan §3.0 decision 5 moved the weather start from 2022-01-01 to **2018-01-01**. Before loading, the
storage was measured, not estimated: each layout was filled with one year of rows on a local
PostgreSQL 16 (embedded `pgserver`), `VACUUM ANALYZE`d, and read with `pg_total_relation_size`
(heap + indexes).

| Item | Measured | For the Neon budget |
|---|---|---|
| Empty schema 0001 on Neon (`pg_database_size`) | 8.4 MB | 8.4 MB |
| `raw_demand_history` + `actuals`, 2015 → now, on Neon after C2 | 25 MB + 11 MB | 36 MB |
| `predictions`, 260 B/row incl. 3 indexes | 54.7 MB per model-year | 7 names × 1 year = 383 MB; with the §4.4 90-day prune of 3 names = **259 MB** |
| `weather_hourly` as in 0001 (row per city-hour) | 167 B/row → 8.8 MB/year | 2018 → now: **77 MB** |
| `weather_hourly` wide (row per hour, 54 `real` columns) | 344 B/row → 3.0 MB/year | 2018 → now: **27 MB** |

Against the 0.5 GB cap (taken as 500 MB; 60 % = 300 MB), weather + demand + one year of
predictions is 8 + 36 + 259 + 77 = **380 MB (76 %)** with the 0001 layout and 8 + 36 + 259 + 27 =
**330 MB (66 %)** with the wide layout.

**Decision (builder's call): wide layout, migration `0002_weather_wide`.** One row per hour with a
`real` column per (variable, city), named `<variable>_<city>` (`temperature_2m_toronto`,
`snowfall_thunder_bay`, …), plus `source`, `available_at`, `available_at_kind`, `fetched_at`
once per hour. It is lossless (all 9 variables × 6 cities stay; no aggregate, no rounding), it is
65 % smaller, and it is already the feature-matrix shape, so the live snapshot frame
(`openmeteo.fetch_live`) and the training frame have the same columns. `weather_locations` keeps
the 6 cities and their weights (seeded by 0002). `v_training_base` now returns one row per hour
with the 54 weather columns and `weather_available_at`.

Rejected: a city-weighted aggregate (loses the per-city features of plan §4.5); scaled `smallint`
columns (another ~12 MB saved, at the cost of a decode step everywhere).

**The remaining overshoot is predictions, not weather.** Even with weather at 27 MB the year-one
total stays above 60 %, and `scores` (similar row count, not measured yet) comes on top: plan
§4.4 assumed ~136 B per prediction row (200 MB per 1.47 M rows); the measured figure is 260 B. This
is flagged for C9/C10 (see the Day-2 report): the §4.4 levers alone do not keep year one under
0.5 GB.

### 9.0 Backfill result (2026-10-23, run started ~20:31Z)

`scripts/backfill_weather.py`: 106 monthly requests (6 cities each), all succeeded (retries are not logged).

```
2018-01: hours=744 written=744
...
2026-10: hours=528 written=528
written this run: 77208
weather_hourly rows: 77208 (2018-01-01 00:00:00+00:00 .. 2026-10-22 23:00:00+00:00), size 25 MB
pg_database_size: 74153984 bytes (74.2 MB)
```

- 77,208 rows = 3,217 days × 24: **no gaps**. 18 null values in total, all at
  2018-01-01T00:00Z (3 accumulated variables × 6 cities).
- The `WeatherHourly` contract (C5) passes on the whole Neon table.
- `v_training_base` returns 77,207 rows, each with `weather_available_at` (the only weather hour
  without an actual is the 2025-05-01 HE1 gap).
- Measured 25 MB on Neon, against the 27 MB local estimate.

### 9.1 Open-Meteo facts seen live while building C3

- With a single `models=gfs_seamless`, the response keys are the plain variable names (no model
  suffix). Several coordinates in one request return a JSON list in request order.
- The live Forecast API with `past_hours=24&forecast_hours=48` returns **72** hourly rows, from
  `floor(now) − 24 h` to `floor(now) + 47 h` (fetched 2026-10-23 20:27Z: 2026-10-22 20:00Z …
  2026-10-25 19:00Z). The plan's span "`[issue_time − 24 h, issue_time + 48 h]`" is therefore
  `[issue_time − 24 h, issue_time + 47 h]` in interval starts; target hours `issue + 1 … issue + 24`
  are covered.
- In the historical archive, `shortwave_radiation`, `precipitation` and `snowfall` are null for
  2018-01-01T00:00Z in every city (accumulated variables start one hour later); everything else is
  non-null.

---

## 10. C4 — live RealtimeTotals and Adequacy3 ingest (2026-10-23 20:42Z)

`gridcast ingest realtime --lookback 24` and `gridcast ingest adequacy --max-files 400` were run
once against Neon.

**RealtimeTotals:** `due=24 stored_before=0 fetched=23 inserted=22 missing=1 partial=1`.
22 complete hours stored (2026-10-22 20:00Z … 2026-10-23 19:00Z) as `observed`, Market Demand only.
- The 9 hours that overlap `PUB_Demand` (2026-10-22 HE16–24) match its Market Demand within
  **0.43 MW** (rounding of the 12-interval mean), confirming plan §3.0 decision 1.
- 2026-10-23 **HE11 is still partial (2 intervals) and HE12 still 404 at 20:42Z**, ~10.5 h after
  the outage. Neither was back-filled during the day. So far this supports keeping
  `REALTIME_LAG_H = 1` (the fallback-to-2 trigger was a late back-fill); the 25 h cadence
  observation still decides it. The ingest skips both without failing (plan §3.0 decision 6).

**Adequacy3:** `listed=5551 pending=232 files=232 rows=5568` in 12 min 39 s (~3.3 s per file,
mostly the per-file round trips to Neon).

| delivery date | versions stored | first `created_at` (UTC) | newest `created_at` (UTC) |
|---|---:|---|---|
| 2026-10-23 | 99 (v1–v99) | 2026-09-19 13:57:09 | 2026-10-23 20:35:02 |
| 2026-10-24 | 68 | 2026-09-20 13:57:36 | 2026-10-23 17:53:16 |
| 2026-10-25 | 65 | 2026-09-21 13:58:46 | 2026-10-23 20:35:03 |

- `CreatedAt` parsed as fixed EST: the newest 2026-10-23 version, `CreatedAt 15:35:02`, is stored
  as 20:35:02Z, a few minutes before our 20:4xZ fetch, as expected.
- The raw XML of all 232 versions is archived gzipped under the git-ignored
  `data/raw/ieso/adequacy3/YYYY/MM/DD/` (2.1 MB). **No object store exists yet** (P0.2 is still
  open), so this local directory is the archive until the bucket exists; the plan's one-off copy
  script moves it there.

**Storage warning for C10.** `ieso_forecast_snapshots` measures **210 B/row** on Neon (1,144 kB
for 5,568 rows). A delivery date enters the today..today+2 window with its whole history of
versions (~65 issued over the previous month) and collects ~50 more on D−1/D, so the hourly job
stores ~120 versions × 24 rows per delivery date: ≈ 1.05 M rows ≈ **220 MB per year**. That is not
in the §4.4 budget. A version created before D−2 00:00 EST is never the latest snapshot at any
issue time whose 24 targets touch D, except the single newest one of them; storing only those
(plus the newest older one) roughly halves this. Decision left to C10/the orchestrator.

---

## 11. C5 — data contracts (pandera)

`src/gridcast/contracts/__init__.py`. Each ingest path validates its frame before it opens a
write transaction, so a failure writes nothing (integration test
`test_contract_failure_writes_nothing`).

| Contract | Used in | Main rules |
|---|---|---|
| `RawDemand` | `ieso_demand.ingest_pub_demand`, `ieso_realtime.ingest_recent` | `target_time` UTC, on the hour, `= he_to_utc(local_date, hour_ending)`; Ontario 5–30 GW, Market 5–35 GW; PUB rows carry both, realtime rows carry Market only with `n_intervals = 12`; `available_at ≤ fetched_at + 5 min`; unique `(target_time, source, file_name)` |
| `WeatherHourly` | `openmeteo.backfill` | 54 value columns in physical ranges, nullable; historical rows have `available_at = valid_time − 24 h`; unique hourly `valid_time` |
| `WeatherFrame` + span | `openmeteo.fetch_live` (`validate_weather_snapshot`) | ≤ 5 % nulls; covers `[issue − 24 h, issue + 47 h]` hourly, `issue = floor(fetched_at)` |
| `IesoSnapshot` | `ieso_adequacy.ingest_recent` | 24 hourly rows, 5–30 GW; `created_at ≤ fetched_at + 5 min`; after a run that read every pending version, the newest `created_at ≥ fetched_at − 3 days` |

Verified on real data: the PUB_Demand backfill re-run with contracts active (all 12 yearly files
pass, 0 rows inserted), the whole `weather_hourly` table, and live `gridcast ingest realtime` /
`ingest adequacy` runs at 21:02Z.

---

## 12. C6 — feature builder v1 (2026-10-23)

`src/gridcast/features/` (`build.py`, `calendar.py`, `schema.py`), `FEATURE_SCHEMA_VERSION = 1`,
96 feature columns: horizon, 13 calendar, 11 issue-level demand, 3 target-level demand lags,
54 per-city weather, 14 weather aggregates.

**Availability rules as built** (one code path for training, backtest and live):

| Input | `available_at` used by the as-of filter |
|---|---|
| Ontario Demand (`ont_*`, baselines) | the PUB row stamp: modelled `D+1 13:00Z` (backfill) or observed (live, revisions) |
| Market Demand, first PUB version of an hour (`mkt_*`) | `availability.market_demand_available_at` = target + `REALTIME_LAG_H` (1 h) |
| Market Demand, PUB revision | its own (observed) row stamp |
| Market Demand, RealtimeTotals row | observed stamp (modelled rows: target + `REALTIME_LAG_H`) |
| Weather | not filtered: the frame handed in (historical store `valid_time − 24 h`, or one live snapshot `fetched_at`) is checked row by row |

For each hour the newest version with `available_at ≤ data_cutoff` wins. `L0` = Market Demand at
`issue_time − REALTIME_LAG_H`; live, if that hour is missing, the newest Market hour of the
5 before it is used and `mkt_l0_age_h` records the age. `ont_last` = newest visible Ontario hour
(8–32 h old under the PUB rule), with `ont_last_age_h`.

**Leakage tripwire.** Each feature row carries `input_available_at` (max over every input row it
read). `assert_asof` raises `LeakageError` if it exceeds `issue_time + 30 min`. It is independent of
the selection code: the unit tests show that a weather row stamped `cutoff + 1 min` raises, and that
a demand row stamped `cutoff + 1 min` is ignored by the correct selection (L0 falls back one hour)
but raises as soon as the as-of mask is replaced by one that ignores the cutoff.

**Done-when output (Neon, 2026-10-23):**

```
build(2025-06-15 12:00Z): rows=24 features=96 null_feature_cells=1 L0_lag_h={1.0} ont_last_age_h=32 max(input_available_at)=2025-06-15 12:00:00+00:00 <= cutoff 2025-06-15 12:30:00+00:00
build(2023-01-10 03:00Z): rows=24 features=96 null_feature_cells=1 L0_lag_h={1.0} ont_last_age_h=23 max(input_available_at)=2023-01-10 03:00:00+00:00 <= cutoff 2023-01-10 03:30:00+00:00
```

The one null cell is `mkt_lag24` at h = 24 (target − 24 h = issue_time, not yet available), by design.

**Live fact: the Civic Holiday is not in the `holidays` package's public calendar.**
`holidays.country_holidays("CA", subdiv="ON")` (v0.107) lists New Year's, Family Day, Good Friday,
Victoria Day, Canada Day, Labour Day, Thanksgiving, Christmas and Boxing Day only. The Civic
Holiday (first Monday of August) is under its *optional* category, together with Easter Monday and
Remembrance Day. Plan §3.5 counts the Civic Holiday, so `features/calendar.py` adds it from the
optional category and leaves the other two out (not widely observed in Ontario).

---

## 13. C7 — baselines and the rolling-origin harness (2026-10-23)

`models/baselines.py`, `models/intervals.py`, `backtest/harness.py`; `gridcast backtest`.

- **Folds:** the 12 EST months 2025-10 … 2026-09 (the last month whose test targets all have final
  actuals; `actuals` ends 2026-10-23 04:00Z). Fold k trains on targets before `month start − 2 days`.
  Test issue times every 6 h (00/06/12/18Z), 24 horizons: 1,460 issue times, 35,040 forecasts per
  model.
- **Baselines** read Ontario Demand under the PUB rule (8–32 h old): `seasonal_naive_168` =
  target − 168 h; `seasonal_naive_24` = target − 24 h, else − 48 h, else − 72 h (the plan lists
  24 → 48; at early-morning issue times even − 48 h is not yet published, so a third step was
  needed); `persistence` = newest visible hour. Their p10/p90 come from their own relative
  residuals over the 60 days before each fold's training cutoff.
- **Peak hours:** P90 (linear interpolation, = `percentile_cont`) of the 720 final actuals of the
  30 EST days before the target's EST day; 4,040 of 35,040 test targets are peak hours.
- **Leakage assertion** active on every fold; **lag parity:** `issue_time − L0 hour` = 1 h for every
  test issue of every fold; weather margin (cutoff − newest weather input) = 23.5 h everywhere
  (the synthetic `valid_time − 24 h` stamp, R11).

**Baseline backtest** (`gridcast backtest --no-lgbm`, Neon, 2026-10-23; MAPE in %):

| model | pooled MAPE | winter | spring | summer | fall | peak-hour MAPE | coverage_80 |
|---|---:|---:|---:|---:|---:|---:|---:|
| seasonal_naive_168 | **6.652** | 5.428 | 6.023 | 9.600 | 5.520 | 8.166 | 0.786 |
| seasonal_naive_24 | 6.397 | 4.683 | 7.034 | 8.183 | 5.644 | 6.447 | 0.774 |
| persistence | 10.362 | 8.126 | 9.527 | 13.254 | 10.493 | 17.954 | 0.783 |

`seasonal_naive_168` = 6.65 %, inside the plan's plausible 5–8 % range. Per fold it runs from
4.41 % (2025-11) to 10.65 % (2026-06 and 2026-07: summer heat).

**2023 folds** (`gridcast backtest --no-lgbm --month 2023-01 --month 2023-07`), assertion active:

```
 fold      month                 train_end  n_test_issues l0_lag_h weather_margin_h  null_l0  null_mean24  null_lag168
    1 2023-01-01 2022-12-30T05:00:00+00:00            124    1:124       23.5..23.5        0            0            0
    2 2023-07-01 2023-06-29T05:00:00+00:00            124    1:124       23.5..23.5        0            0            0
```
