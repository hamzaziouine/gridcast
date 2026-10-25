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

---

## 14. Storage fixes for year one (2026-10-23): Adequacy3 retention and a compact predictions row

§9–§10 showed year one would pass Neon's 0.5 GB. Two fixes, migration `0003_compact_predictions`.

### 14.1 Adequacy3 retention

Rule (`ieso_adequacy.prune_snapshots`, run at the end of every `ingest adequacy` and by
`gridcast prune adequacy`): for delivery date D keep every version created at or after
**(D − 2 days) 00:00 EST**, plus the **newest version created before that**. Issue times whose 24
targets touch D start at D−1 00:00 EST, so the benchmark (latest snapshot ≤ `data_cutoff`) is always
a kept version. A new ledger `ieso_adequacy_files` (one row per file version read: `created_at`,
`archive_key`, `rows_kept`) replaces "distinct `source_file` in the snapshots" as the record of
what was downloaded, so pruned versions are never fetched again. New raw XML goes to the private
bucket (`raw/ieso/adequacy3/YYYY/MM/DD/<file>.xml.gz`) whenever `OBJSTORE_*` is set.

Run on Neon, 2026-10-23 ~22:30Z, in this order:

```
gridcast db upgrade                      -> 0003_compact_predictions (ledger seeded: 232 files)
scripts/copy_archive_fallback.py         -> local files: 232, uploaded: 232, already in bucket: 0,
                                            ledger rows updated: 232, objects under raw/ieso/adequacy3/: 232
gridcast prune adequacy --vacuum-full    -> files_pruned=186 rows_deleted=4464
                                            ieso_forecast_snapshots = 245760 bytes
```

Kept: 2026-10-23 = 37 versions (newest pre-cutoff 10-20 20:34Z + 36 since 10-21 05:00Z),
2026-10-24 = 6, 2026-10-25 = 3. The archive in the bucket was verified byte for byte before any
row was deleted; the local copy under `data/raw/` is still in place.

A complete delivery date keeps about **54 versions** (≈ 2 on D−2, 3 on D−1 before 20:28 EST, ~7
after, ~48 on D, + 1 older) instead of ~120: ≈ 473k rows/year × 222 B/row (measured on Neon after
`VACUUM FULL`) ≈ **105 MB/year** (was ≈ 220 MB).

### 14.2 Compact `predictions`

`predictions` was empty, so 0003 drops and re-creates it:

| | 0001 | 0003 |
|---|---|---|
| MW columns | `numeric(9,2)` | `real` |
| horizon | smallint | smallint |
| model_name / model_version | text / text | `model_id smallint` → `prediction_models`; `version_id int` → `model_versions` |
| run_id, run_kind, created_at | per row | per (issue_time, model) in `prediction_runs` |
| target_time, ieso_staleness_min | stored | derived in the view `v_predictions` (issue_time + horizon h; cutoff − snapshot created_at) |
| indexes | PK (issue, target, model_name) + 2 | PK **(issue_time, model_id, horizon)** only |

`v_predictions` returns the plan's §4.3 column set, so readers do not change. The PK starts with
`issue_time` so hourly inserts always land on the right-most leaf: with `model_id` first, the same
data measured 130 B/row because mid-index leaf splits leave pages half empty.

**Measured on PostgreSQL 16.2 (embedded pgserver), 1,008,000 rows = 6,000 issues × 7 names × 24,
`VACUUM ANALYZE`, `pg_total_relation_size` incl. indexes** (`scripts/measure_predictions_size.py`):

```
0002_weather_wide: 230.5 MB total, 228.7 B/row (predictions 230.5 MB)
0003_compact_predictions: 97.0 MB total, 96.2 B/row (predictions 91.5 MB, prediction_runs 4.6 MB, model_versions 0.9 MB)
scores (0001 layout, unchanged): 200.3 MB total, 198.7 B/row (scores 200.3 MB)
```

(§9's 260 B/row for 0001 used longer version strings; on identical data 0003 is 58 % smaller.)

### 14.3 Neon size and the year-one projection

| | `pg_database_size` |
|---|---|
| before (2026-10-23 ~21:15Z, after C2–C5) | 74,907,648 B (74.9 MB) |
| after 0003 + archive + prune + `VACUUM FULL` | 74,219,520 B (74.2 MB) |

The immediate saving is small (the snapshot table held only 5,568 rows); the fixes change the
growth rate. Year one, 7 model names × 24 × 8,760 = 1.47 M prediction rows; the §4.4 90-day prune
of `seasonal_naive_24`, `persistence` and `prev_champion` keeps ≈ 1.00 M of them:

| | MB, year one |
|---|---:|
| today (demand, actuals, weather, rest) | 74 |
| demand + actuals + weather growth | 7 |
| predictions, 0003 layout (1.00 M × 96.2 B) | 96 (was 259) |
| ieso_forecast_snapshots with retention | 105 (was 220) |
| **scores, 0001 layout** (1.47 M × 198.7 B; §4.4 prunes predictions only) | **292** |
| **total** | **574 MB — still over 500** |

**Open for C11 (flagged):** `scores` is now the largest item and is still empty. The same treatment
before its first row (`real` errors, `model_id`/`version_id`, drop the derivable `target_time`,
`actual_mw`, `abs_pct_err`, `actual_source`; PK `(issue_time, model_id, horizon)`) should bring it
to ≈ 100 B/row ≈ 147 MB, or ≈ 100 MB if the 90-day prune also covers the scores of the three weak
names (their `score_daily` aggregates stay). With that: ≈ 74 + 7 + 96 + 105 + 100–147 ≈ 380–430 MB.
The `ieso_forecast_snapshots` row (222 B: `source_file` text duplicated by the ledger, `numeric`
MW) could shrink the same way, to ≈ 35 MB/year. `zonal_actuals` (Phase D, ~40 MB) comes on top.
→ Done before the first score row: §14.4.

### 14.4 Compact `scores` (migration `0004_compact_scores`, 2026-10-24) and the new projection

`scores` was empty, so 0004 drops and re-creates it. A score row stores only what is not
derivable: `target_time`, `model_id smallint`, `horizon smallint`, `actual_revision smallint`,
`actual_mw real` (the actual it was scored against). The view `v_scores` joins `predictions`
and returns the plan's §4.3 columns (`issue_time`, `model_name`, `model_version`, `err_mw`,
`abs_pct_err`, `pinball_10/50/90`, `in_80_interval`, `actual_source` = `'pub_demand'` always).
`scored_at` is dropped (`pipeline_runs` records when the daily job ran). One index, the PK
**(target_time, model_id, horizon)**: the daily job scores one EST day of targets per batch,
oldest first, so each batch appends at the right-most leaf. Keyed by `issue_time` instead,
consecutive daily batches interleave (the issue times of day D's targets overlap day D−1's by
23 h) and split mid-index pages.

**Measured on PostgreSQL 16.2 (embedded pgserver), 604,800 rows = 150 EST days × 24 targets × 7
names × 24 horizons, written in the daily job's order (one COPY per target day, sorted by each
table's PK), `VACUUM ANALYZE`, `pg_total_relation_size`** (`scripts/measure_scores_size.py`):

```
PostgreSQL 16.2; 604,800 score rows (150 EST days x 24 x 7 models x 24)
scores after 0003_compact_predictions: 133.3 MB, 220.4 B/row
scores after 0004_compact_scores: 50.6 MB, 83.7 B/row
```

(The 0001 layout measured 198.7 B/row in §14.2 when filled in pure PK order; in the daily
job's batch order its issue-first key costs 220.4 B/row.)

**Year-one projection** (1.47 M scorable prediction rows; the 90-day prune of
`seasonal_naive_24`, `persistence` and `prev_champion` predictions is to delete their score rows
in the same statement, leaving ≈ 1.00 M):

| | MB, year one |
|---|---:|
| today (demand, actuals, weather, rest) | 74 |
| demand + actuals + weather growth | 7 |
| predictions, 0003 layout (1.00 M × 96.2 B) | 96 |
| ieso_forecast_snapshots with retention | 105 |
| **scores, 0004 layout (1.00 M × 83.7 B)** | **84** (was 292) |
| hourly bookkeeping (`pipeline_runs` ≈ 26 k rows, `weather_snapshot_index` 8.8 k rows) | ≈ 5 |
| **total** | **≈ 371 MB** (74 % of 500; target ≤ 430 met) |

Without the scores prune (all 1.47 M rows kept): 123 MB → ≈ 410 MB, still ≤ 430.
`zonal_actuals` (Phase D, ~40 MB) would bring the pruned total to ≈ 411 MB.

---

## 15. C8 — LightGBM backtest and the bootstrap target (2026-10-23)

`models/lgbm.py`: 24 per-horizon boosters, objective `l1`, learning rate 0.03, 63 leaves,
`min_data_in_leaf` 50, feature/bagging fraction 0.8, seed fixed, `deterministic=True`; at most 400
rounds, early stopping (40) on the last 90 days of each training window, then a refit on the whole
window with the best round count. Artifact: gzipped JSON of LightGBM's text model format (no pickle).
`models/intervals.py`: p10/p90 from per-horizon 10/90 % quantiles of relative residuals on folds
1–10 × 1.15, coverage measured on the held-out folds 11–12 only. Training issue times are hourly
from 2018-01-02 (≈ 67–76 k rows per horizon and fold).

**Backtest, 12 folds 2025-10 … 2026-09, 35,040 forecasts per model (MAPE in %):**

| model | pooled | winter | spring | summer | fall | peak hours | h 1–6 | h 7–12 | h 13–24 | coverage_80 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| **lgbm** | **2.612** | 2.034 | 2.578 | 3.198 | 2.625 | 3.147 | 2.407 | 2.594 | 2.723 | **0.823** (folds 11–12) |
| seasonal_naive_168 | 6.652 | 5.428 | 6.023 | 9.600 | 5.520 | 8.166 | 6.658 | 6.653 | 6.649 | 0.786 |
| seasonal_naive_24 | 6.397 | 4.683 | 7.034 | 8.183 | 5.644 | 6.447 | 5.854 | 6.310 | 6.713 | 0.774 |
| persistence | 10.362 | 8.126 | 9.527 | 13.254 | 10.493 | 17.954 | 10.186 | 10.194 | 10.534 | 0.783 |

LightGBM per fold: 2.26, 3.27, 2.12, 2.01, 1.99, 2.55, 2.58, 2.59, 2.92, 3.12, 3.55, 2.35. Skill vs
`seasonal_naive_168` = 1 − 2.612 / 6.652 = **60.7 %**. LightGBM interval coverage: 0.786 (fold 11,
2026-08), 0.862 (fold 12, 2026-09), 0.823 pooled held-out; 0.853 on folds 1–10 (in-sample for the
quantiles, the 1.15 allowance makes them wider than nominal there, as designed).

**Target (§4.7): met.** MAPE 2.612 ≤ 4.0; 60.7 % ≥ 30 % better than `seasonal_naive_168`; held-out
coverage 0.823 inside `COVERAGE_BAND` (0.76, 0.84). Logged to MLflow (local store; DagsHub not set
up yet) as run `5279075b2a8748f0991e3138779b3ef6`, experiment `gridcast-backtest`.

Caveats, stated plainly: (1) backtest weather is the historical-forecast archive with the synthetic
`valid_time − 24 h` stamp, optimistic by design (R11, S4); live MAPE will be higher. (2) Held-out
coverage covers only August and September; the per-fold spread (0.786 vs 0.862) shows the band
check is noisy on two folds. (3) Most horizons stopped at the 400-round cap, not by early stopping
(e.g. h 12: 398), so the model is not tuned; no tuning was done (the target was met on the first run).

**Runtime (new risk for retrain.yml):** the full backtest took **6,270 s (104 min)** on this
16-thread laptop (576 booster fits: probe + refit × 24 horizons × 12 folds). A GitHub runner has
4 vCPUs, so `gridcast train` (backtest + final fit) would take several hours there. Levers before
C17: drop the refit and keep the early-stopped probe, `max_bin` 63, a 2-hourly training grid, or
train from a cached feature Parquet.

MLflow ≥ 3 refuses the plain `./mlruns` file store (`MLFLOW_ALLOW_FILE_STORE` needed), so the local
fallback is SQLite `./mlruns/mlflow.db` with artifacts in `./mlruns/` (git-ignored); setting
`MLFLOW_TRACKING_URI` switches to DagsHub with no code change.

---

## 16. C9 — `gridcast train`, the bootstrap champion and the first prediction rows (2026-10-24)

**Training run** (`gridcast train`, Neon + R2, 2026-10-23 23:2xZ → 2026-10-24 01:1xZ): the
backtest reproduced §15 exactly (deterministic seeds: pooled MAPE 2.612 %, skill 60.7 %, held-out
coverage 0.823); the final fit used every hour from 2018-01-02 whose 24 targets have weather.
Artifacts in the private bucket under `models/lgbm-2026.10.23-49ca/`: `model.json.gz`
(sha256 `49ca5a49…ab4409`), `intervals.v0001.json`, `backtest.json`. MLflow run
`00560ff0f3cd4ca7ada0c86d28764272` (local store, experiment `gridcast-train`).

**Failure and fix.** After ~2 h of CPU the first `model_registry_events` insert failed with
`psycopg.OperationalError: consuming input failed: SSL connection has been closed unexpectedly`:
Neon had scaled the compute to zero and the SQLAlchemy pool handed back the dead connection.
Fix: `db.make_engine` now sets `pool_pre_ping=True`. `train` was split so the registry and bootstrap
step (`jobs/train.register`) is idempotent and can run alone on an uploaded artifact
(`gridcast train --register-only <version> --mlflow-run-id <id>`, sha256 re-checked from the
bucket, numbers read from the artifact's `backtest.json`); it was run that way, so the 2-hour
training was not repeated. Two latent bugs found by the new end-to-end test were also fixed:
the training/final-fit issue grids now stop where the weather store can cover all 24 targets
(`weather_hourly` ends 10-22 23:00Z, before the last actual 10-23 04:00Z), and NaN metrics are
written to `jsonb` as `null`.

**Bootstrap (§4.7, ADR-0010): target met → `reason='bootstrap'`, `champion_status='ok'`.**

```
models/champion.json = {"model_name": "lgbm", "model_version": "lgbm-2026.10.23-49ca",
 "artifact_key": "models/lgbm-2026.10.23-49ca/model.json.gz", "sha256": "49ca5a49…",
 "feature_schema_version": 1, "promoted_at": "2026-10-24T01:18:02Z",
 "mlflow_run_id": "00560ff0f3cd4ca7ada0c86d28764272", "champion_status": "ok",
 "bootstrap_reason": "bootstrap", "intervals_key": "models/lgbm-2026.10.23-49ca/intervals.v0001.json"}
model_registry_events: 1 trained, 2 backtested, 3 promoted (reason 'bootstrap', triggered_by 'manual')
```

**First prediction rows.** `ingest realtime` + `ingest adequacy` (9 new versions, archived to R2),
then `gridcast predict --issue-time 2026-10-24T01:00:00 --weather live` at 01:21Z (live
Open-Meteo snapshot fetched 01:21:08Z ≤ cutoff 01:30Z): 24 rows each for `champion`,
`seasonal_naive_168`, `seasonal_naive_24`, `persistence`, `ieso_adequacy` = 120 rows,
`run_kind='manual'`. IESO snapshot ages 37 and 457 min (the D+1 hours come from the 07:57 EST issue,
as §0 item 3 predicted). Sample p50 (MW): h1 champion 15,277 / IESO 15,500 / sn168 15,222;
h24 15,146 / 15,311 / 15,269.

**Why `--weather live`:** `weather_hourly` (historical archive) ends 2026-10-22 23:00Z and the
Adequacy3 history starts with delivery date 2026-10-23, so no past hour has both full weather and
IESO coverage from the store. A recent past hour needs the live snapshot; it must be fetched no
later than `issue_time + 30 min`. C10 stores these snapshots in the bucket.

---

## 17. C10 — the hourly job (`gridcast run hourly`, `.github/workflows/hourly.yml`, 2026-10-24)

`src/gridcast/jobs/hourly.py` (steps in its docstring), `ingest/weather_snapshots.py`,
`jobs/publish.py`, `jobs/runs.py`.

### 17.1 Cadence and cron

- Cron `2,12,22 * * * *` since 2026-10-24 (ADR-0012; was the plan's `7,27`). On the first
  scheduled day GitHub dropped the 18:07 and 18:27 slots and started 19:07 at 19:24. At :02 the
  L0 hour (`issue − REALTIME_LAG_H`, 1 h) has been complete since :53–:57 of the previous hour
  (§1.4). Slots queue in the `hourly` concurrency group (`cancel-in-progress: false`); the first
  to write the hour's 24 champion rows marks it `ok`, later ones end `skipped` (success ping) and
  still ingest Adequacy3 reissues.
- A run that starts after the cutoff cannot use its own snapshot (`available_at = fetched_at` >
  cutoff); it falls back to the newest earlier snapshot that covers the hour, as catch-up does.
- `REALTIME_LAG_H` stays at the provisional 1 (§1.4's day-long observation is still open).

### 17.2 Weather snapshots (the as-of record for live weather)

- One snapshot per UTC hour slot: `weather/snapshots/YYYY/MM/DD/HH.parquet` in the private bucket
  (72 rows × 54 columns) + a `weather_snapshot_index` row with
  `available_at = fetched_at` (observed). A slot that already has one is not fetched again.
- Hour H may use a snapshot only if `available_at ≤ H + 30 min` and it covers `H − 23 h … H + 24 h`
  (the 24 h rolling weather features need the 23 h before the first target). The newest such
  snapshot wins. Current hour and catch-up hours use the same rule, so a missed hour never sees a
  forecast fetched after its cutoff.

### 17.3 Catch-up, degradation, idempotency

- Catch-up: the last 6 issue hours with no champion row and no `ok` hourly run, oldest first,
  `run_kind='catchup'`, each with its own cutoff. No qualifying snapshot → no champion row
  (visible gap); baselines and `ieso_adequacy` are still written; the hour gets an `ok`
  (`catchup`) `pipeline_runs` row with a note so it is not retried every hour. A catch-up hour
  that errors is recorded `failed` and retried by the next run.
- IESO outages never fail the job (§3.0 item 6): RealtimeTotals 404/partial hours are counted and
  skipped (the feature builder falls back up to 5 h for L0); an Adequacy3 listing/download error
  marks only the `ieso_adequacy` sub-row `failed`; a newest Adequacy3 snapshot older than 6 h is
  a note (`adequacy_stale`), not a failure. **Deviation from plan §6.1 step 8b**, which fails
  the run on stale Adequacy3: the orchestrator's rule (IESO outages degrade, never fail) wins.
- The run is `failed` (exit 1, Healthchecks `/fail`) only when the issue hour lacks its 24
  champion rows (no qualifying weather, model or database error) or publishing failed.
- Re-running the same issue hour is `skipped` (dedupe on `pipeline_runs`); champion rows are
  insert-only, baseline/IESO rows are deterministic upserts.
- Notes with exception messages go to `pipeline_runs.notes` only; logs and `meta.json` get the
  step and exception type (a driver message can name the database host; the repo is public).

### 17.4 Deviations from the plan (C10)

| Plan | Built | Why |
|---|---|---|
| publish `api/v1/forecast/latest.json`, `api/v1/health.json`, … | `latest/forecast.json`, `latest/meta.json` (+ `latest/scoreboard.json` daily), schema in `jobs/publish.py` | orchestrator's Day-4 brief; the Worker maps them to `/v1/*` |
| `HC_HOURLY_URL` / `HC_DAILY_URL` | `HEALTHCHECKS_HOURLY_URL` / `HEALTHCHECKS_DAILY_URL`, optional | orchestrator's Day-4 brief; `.env.example` updated |
| step 8 quick score each hour | none; scoring is daily only | §3.0 decision 1 (no provisional actuals) |
| step 9b deep `/health` probe + `HC_API_URL` | not built | the Worker host has no DB; UptimeRobot on `/health` is the external probe (C16) |
| champion cached in the Actions cache by sha256 | in-process cache only (one download per run) | the artifact is small; R2 egress is free |
| `jobs hourly` | `run hourly` | orchestrator's Day-4 brief |

### 17.5 First real runs (Neon + R2, local `.env`, 2026-10-24)

```
16:57:54Z gridcast run hourly --run-kind manual   (issue 16:00Z, 2 min 36 s)
  adequacy: listed=5539 pending=94 files=20 rows=480 pruned=0
  realtime: due=6 fetched=6 inserted=6 missing=0 partial=0
  weather: stored (slot 16, fetched ~16:59Z > cutoff 16:30Z -> unusable for 16:00Z)
  catch-up 10:00-15:00Z: no snapshot existed -> baselines + IESO only (4 x 24 rows each)
  status=failed: champion rows for issue_time 0/24   <- honest: started 28 min after the hour
17:01:13Z gridcast run hourly --run-kind manual   (issue 17:00Z, 2 min 16 s)
  adequacy: pending=74 files=20 rows=480 pruned=216
  realtime: stored_before=5 inserted=1
  weather: stored (slot 17, fetched 17:01Z <= cutoff 17:30Z)
  catch-up 16:00Z: the 16:59Z snapshot is after its 16:30Z cutoff -> gap, baselines + IESO
  rows_written: champion 24, ieso_adequacy 24, persistence 24, seasonal_naive_168 24,
                seasonal_naive_24 24; status=ok
  published: latest/forecast.json 5,739 B, latest/meta.json 955 B
```

The Adequacy3 backlog (94 versions since the last ingest on 10-24 01:2xZ) drains at the plan's cap
of 20 files per run (~66 s per run at ~3.3 s per file); later runs only see the ~2 new versions
per hour. Weather snapshots measure **49.4 kB** each (Parquet, 72 × 55, float64), ≈ 430 MB/year
in the private bucket — inside R2's 10 GB, but ~14× the plan's 30 MB/year estimate (the plan
assumed float32 and fewer columns); casting to float32 would halve it if it ever matters.

---

## 18. C11 — the daily job (`gridcast run daily`, `.github/workflows/daily.yml`, 2026-10-24)

`src/gridcast/jobs/daily.py` (steps and the `scoreboard.json` schema in its docstring).

### 18.1 What it does

- Cron `20 13 * * *` (08:20 EST, ~50 min after the 07:30 EST `PUB_Demand` update) and
  `20 18 * * *`. logical_time = 00:00 EST of the run's EST day; the first slot ends `skipped`
  (not `ok`) while yesterday HE24 is still missing, so the 18:20 slot retries.
- Ingest reads the directory listing and downloads the newest **versioned**
  `PUB_Demand_<year>_vNNN.csv` (plus last year's in January), observed stamps. Bug avoided:
  `raw_demand_history` is keyed by `(target_time, source, file_name)`, so reading the
  unsuffixed `PUB_Demand.csv` every day would make the next day's revision of the provisional
  HE1 collide with the version it revises (`ON CONFLICT DO NOTHING`) and be lost. The
  integration test exercises two successive revisions.
- Scores: every prediction whose target has an actual, targets in the 45 EST days before the
  run's day (that day's provisional HE1 is excluded); inserted in target order; re-scored in
  place when `actuals.revision` or the value moves.
- `peak_hours`, `score_daily` (45-day delete + insert, `final_only = true`, 4 horizon bands) as
  plan §4.3/§6.2.

### 18.2 Drift signals (plan §6.2 step 4, error-based part)

`drift_metrics` rows per run (run_time = the logical time, idempotent upsert), each with
`consecutive_days` of breach in `context`:

| metric | value | threshold / breach |
|---|---|---|
| `rolling_mape_7d` | champion MAPE, last 7 EST days | > 1.5 × `ref_mape_30d` |
| `ref_mape_30d` | champion MAPE, trailing 30 d excl. the last 7 — the backtest MAPE (`model_registry_events` `backtested.lgbm_mape`) until 30 live days exist | – |
| `naive_mape_7d` | `seasonal_naive_168` MAPE, last 7 days (context: a shared breach = weather regime) | – |
| `coverage_30d` | champion 80 % coverage, 30 days | outside [0.72, 0.88] |
| `neon_storage_mb` | `pg_database_size` | warn > 400 (context), **fail > 450** (§6.2 step 6c) |

PSI and `l0_lag_parity`: built on Day 5 (§19.3). Still not built: the Neon CU-h/egress guard
(needs `NEON_API_KEY`).

### 18.3 Why no retrain trigger, and permissions

The plan wires breach → `gh workflow run retrain.yml` in the daily job (§6.2 step 4, permissions
`contents: write, actions: write`). C11's "done when" (§9 Day 5) is the minimum without drift
actions, and a full `gridcast train` backtest took **104 min on a 16-thread laptop** (§15): on a
4-vCPU runner that is several hours, past any sane `timeout-minutes`. So the daily job only
**records** the signals; nothing dispatches a retrain, `retrain.yml` does not exist yet, and
`daily.yml` keeps `contents: read`. The status-branch commit (§6.2 step 6, C15) is not built
either, so `contents: write` is not needed yet. Levers before wiring retrain (C17): drop the
refit and keep the early-stopped probe, `max_bin` 63, a 2-hourly training grid, cached feature
Parquet.

### 18.4 Deviations (C11)

| Plan | Built | Why |
|---|---|---|
| `jobs daily`; `HC_DAILY_URL` | `run daily`; `HEALTHCHECKS_DAILY_URL` (optional) | orchestrator's Day-4 brief |
| status-branch `scoreboard.json` + `metrics.json` + SVG | `latest/scoreboard.json` in the public bucket | Worker host reads the bucket; status branch is C15 |
| 6b: fail when `score_daily` has no row for yesterday | built Day 5, armed only after ≥ 1 full live day (§19.3) | with < 1 day of live forecasts it would fail every run |
| `gridcast intervals recalibrate` (step 3b) | built Day 5 inside the daily job (§19.3); acts from ≥ 14 live days (earliest ≈ 2026-11-08) | |
| scores keyed by (issue, target, model_name) | (target_time, model_id, horizon) | §14.4 |

### 18.5 First real run (Neon + R2, 2026-10-24 17:10:52Z, 37 s)

```
gridcast run daily --run-kind manual   (day 2026-10-24 EST)
  pub_demand_rows: 25 (10-23 HE1..HE24 + the provisional 10-24 HE1)
  scores: inserted 15 (the 10-24 01:00Z issue's h1-h3 x 5 models: targets before 10-24 05:00Z)
  peak_hours: 1,080 (45 days x 24)   score_daily: 10 rows   drift_metrics: 5 rows
  drift: rolling_mape_7d 3.199 (3 champion pairs), ref_mape_30d 2.612 (backtest), naive_mape_7d
         2.454, coverage_30d 1.0 (breached: 3 pairs, noise), neon_storage_mb 74.9
  published: latest/scoreboard.json 3,411 B; newest_actual 2026-10-24T05:00:00Z; status=ok
```

Neon after both jobs: `pg_database_size` = **74,956,800 B (75.0 MB)**; predictions 912 rows,
prediction_runs 38, scores 15, score_daily 10, peak_hours 1,080, weather_snapshot_index 2,
ieso_forecast_snapshots 2,064, ieso_adequacy_files 281, raw_demand_history 103,570, actuals 103,536.

---

## 19. Day 5 (2026-10-24 evening): keepalive, fast retrain, drift, README

### 19.1 C16 `keepalive.yml` (plan §6.7)

- Weekly (`0 6 * * 1`) + `workflow_dispatch`. Step 1 re-enables every workflow file containing a
  `schedule:` trigger (itself included) with `gh api -X PUT
  repos/{repo}/actions/workflows/{file}/enable`. Job permissions: `actions: write` (the enable
  call) and `contents: read` (checkout), nothing else. It never commits or pushes. The plan notes
  that "enable resets the 60-day clock" is not verified; it is the plan's chosen mechanism, and
  the Healthchecks heartbeats remain the backstop.
- Step 2, `gridcast run keepalive` (`jobs/keepalive.py`): `hourly_stale` (no `ok` hourly in 6 h),
  `daily_stale` (no `ok` daily in 48 h), and `loop_stale` (no `trained` / `backtested` /
  `rejected` / `shadow_started` / `promoted` / `candidate_queued` event in 8 days). The loop
  check is armed only once a `retrain` pipeline run exists. Failed checks → `failed`, exit 1,
  `HEALTHCHECKS_KEEPALIVE_URL/fail`. If the enable step fails, a final step pings `/fail` with curl.
- **Deviation:** plan step 2 (status-branch commit < 48 h) is not built, because there is no
  status branch (ADR-0011). The gate-liveness part of step 3 waits for `gate.yml`.
- Secret name `HEALTHCHECKS_KEEPALIVE_URL` (was `HC_KEEPALIVE_URL` in `.env.example`), following
  the `HEALTHCHECKS_*` names chosen on Day 4. It is optional.

### 19.2 C18 fast retrain (`gridcast run retrain`, `retrain.yml`, ADR-0013)

**Local measurement** (`OMP_NUM_THREADS=4 gridcast backtest --profile fast`, Neon data as of
2026-10-24, same 12 folds as the champion, 19:41:07Z → 19:53:55Z, i.e. **12 min 48 s** wall,
including the Neon load and ~11 s of issue-level features for 76,662 issue hours; a pytest run
shared the machine for ~3 min): ~25 s per horizon for 12 folds, 288 booster fits.

| model | pooled | winter | spring | summer | fall | peak | h 1–6 | h 7–12 | h 13–24 | coverage_80 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| lgbm `fast` | **2.627** | 2.029 | 2.598 | 3.213 | 2.654 | 3.191 | 2.433 | 2.597 | 2.738 | **0.828** (folds 11–12) |
| lgbm `full` (§15) | 2.612 | 2.034 | 2.578 | 3.198 | 2.625 | 3.147 | 2.407 | 2.594 | 2.723 | 0.823 |

Per fold (`fast`): 2.32, 3.31, 2.11, 2.01, 1.98, 2.56, 2.60, 2.62, 2.93, 3.18, 3.53, 2.34
(`full`: 2.26, 3.27, 2.12, 2.01, 1.99, 2.55, 2.58, 2.59, 2.92, 3.12, 3.55, 2.35). Skill vs
`seasonal_naive_168` 60.5 % (full 60.7 %). Cost of the speed-up: +0.015 MAPE points (+0.57 %
relative), +0.044 peak points. Against the 104 min of the `full` run on 16 threads, the fit
work is roughly 30× smaller (half the fits, half the rows, 63 bins).

**What the gate does with it today:** the champion is `full`, so a `fast` candidate is a
challenger and needs MAPE ≤ 0.98 × 2.612 = 2.560 on these folds. Expect `rejected` from every
weekly run until a challenger really wins (ADR-0013 consequences).

**Shadow rows:** when `models/shadow.json` exists, the hourly job writes 24 `shadow` rows per
issue hour (from the shadow's `started_at`). That adds ≈ 210 k prediction + 210 k score rows a
year (≈ 20 + 18 MB) while a shadow runs, inside the §14.4 headroom (371 → ≈ 410 MB worst case).

**Drift → retrain:** `daily.retrain_trigger` (7-day MAPE breached 3 days running, ≥ 7 live days,
no retrain in 72 h) → daily.yml `gh workflow run retrain.yml -f reason=drift:mape`. daily.yml now
has `actions: write` for that call only.

### 19.3 Drift and ops checks (daily job; migration `0005_live_features`)

- **`live_features`** (new table, additive migration applied to Neon 2026-10-24 ~20:25Z before the
  code that writes it was pushed): one row per champion issue hour with the horizon-1 PSI
  features and `mkt_l0_age_h`, insert-only, written by `predict.run`. A write error is a note,
  never a failed forecast. ≈ 8.8 k rows/year, < 1 MB.
- **PSI** (`drift/psi.py`, `drift/checks.py`): 8 features (`temperature_2m_w`,
  `apparent_temperature_w`, `dew_point_2m_w`, `hdd_w`, `cdd_w`, `shortwave_radiation_w`,
  `wind_speed_10m_w`, `ont_lag168`). The last 14 EST days of `live_features` are compared with
  the champion's training rows whose target day-of-year is within ±15 days of a window day;
  10 quantile bins from that slice, epsilon 1e-4. A value equal to an edge goes to the lower
  bin, so a point mass (`cdd_w = 0` all winter) is a single bin. It needs ≥ 7 days of live rows,
  else no rows and a note. `drift_metrics` row per feature (`metric='psi'`, `feature=<name>`,
  threshold 0.25, `consecutive_days`).
  **Deviation:** numpy instead of Evidently, and a reference **sample**
  (`models/<version>/reference_features.parquet`, 77,184 h = 1 rows, 2.43 MB, float32) instead of
  per-day-of-year histograms. Season matching happens at read time, so the same file serves
  every window. Built for the current champion with `gridcast drift reference` (27 s) on
  2026-10-24; `retrain` writes one for every candidate. That champion was trained on weather to
  2026-10-22; its reference runs to 10-23, one extra day out of 77 k rows.
- **`l0_lag_parity`**: the mean live `mkt_l0_age_h` over the last 24 issue hours vs
  `REALTIME_LAG_H` (1 h); breached when they differ by > 1 h. The context holds the share of
  fallback hours and the max lag. It is a diagnostic: nothing retrains on it automatically.
- **Triggers** (`daily.retrain_trigger`), both needing ≥ 7 live days and no retrain in 72 h:
  `drift:mape` (§19.2) and `drift:psi+mape`, which fires when PSI > 0.25 on ≥ 2 features for 3
  days and the 7-day MAPE is > 1.2 × the reference. `drift:psi+mape` is suppressed while a shadow
  runs (§4.7 item 7). The coverage rule recalibrates, it does not retrain.
- **Interval recalibration** (§4.5 (ii)): from ≥ 14 EST days of final-scored live residuals of
  the current champion version, each daily run writes `intervals.vNNNN.json` (per-horizon
  10/90 % relative-residual quantiles, trailing min(60, available) days, no allowance) and
  repoints `champion.json.intervals_key` (other fields unchanged; `intervals_recalibrated_on`
  makes it idempotent per day). It also logs `interval_recalibrated`. `seasonal_naive_168`
  already refits its offsets on the trailing 60 days at every prediction.
- **`score_daily_missing`** (§6.2 step 6b): `failed` when champion forecasts targeted yesterday
  but no champion `score_daily` row exists for it. Armed only once the first champion issue is
  ≥ 1 h before yesterday began (so ≥ 1 full day of live data), and only when forecasts did hit
  yesterday. A day with no forecast at all is the hourly job's alarm.
- **Public API probe** (§6.1 step 9b): an hourly.yml step that does nothing until the repository
  variable `PUBLIC_API_URL` is set (the Worker is not deployed yet). It runs `GET /health`, needs
  `"stale":false`, and pings optional `HEALTHCHECKS_API_URL` (`/fail` otherwise). It is
  `continue-on-error`, so it never touches the forecast's status.

### 19.4 First `retrain.yml` run (workflow_dispatch, run 40128743334) and the MLflow run-id repoint

```
2026-10-24 20:12:26Z dispatched (reason=manual, profile=fast); runner: 4 vCPU, 15 GB RAM
  [  3.7s] weather refresh 2026-10: hours=552 written=24        (10-23 added)
  [ 10.4s] loaded raw=103572 weather=77232 actuals=103536       (Neon)
  [ 13.6s] issue-level features for 76662 hourly issue times
  [ 43.2s .. 678.5s] lgbm h=1..24, ~27.5 s per horizon (12 folds)
  [738.9s] final fit: 77184 issue hours, last target 2026-10-24 05:00Z
  MLflow (DagsHub) gridcast-retrain run e2bbb7ea634d441cb33576d1d96ee1b2
  candidate lgbm-2026.10.24-3709: kind=challenger (champion profile full)
  MAPE 2.630 vs 0.98 x 2.612 = 2.560 (+0.70 %); held-out coverage_80 0.824 in band
  decision: rejected      (events 4 trained, 5 backtested, 6 rejected)
  runtime_s 759.4; job 20:12:26 -> 20:25:24Z = 13 min; champion.json untouched
```

The runner's 2.630 against the local 2.627 (same folds, same data up to fold ends) is
cross-platform floating-point difference: LightGBM's `deterministic=True` holds on one platform,
not between Windows and Linux builds. The weekly run reaches 13 min against the 60 min target
and the 90 min timeout.

**Run-id repoint** (`scripts/repoint_mlflow_run_ids.py --apply`, 2026-10-24 ~20:35Z). The Day-3
local runs were copied to DagsHub as `19dc3a899b1b4f1eb895c43274f0d7f5` (train; tag
`migrated_from_local_run_id=00560ff0…`) and `aecef0f962014095965dd7854ab176be` (backtest). Both
were verified with `MlflowClient.get_run`: `FINISHED`, `lgbm_mape` 2.6118. Events 1–3 now carry
the DagsHub id, with the old one kept in `metrics.mlflow_local_run_id`. `models/champion.json`
`mlflow_run_id` was changed the same way, and no other field. A second run changes nothing.
Logged as an `INTERVENTION`-class data repair (plan §12), done once by hand from a script in
the repo.

---

## 20. Day 6 (2026-10-25): weekly jobs on Cloudflare cron, status branch, prune, gate

### 20.1 Weekly jobs on the Cloudflare cron (ADR-0014 amendment)

- Observed: the GitHub `schedule:` slot of `retrain.yml` for Sunday 2026-10-25 03:00Z had not
  started by 06:12Z (no run in `gh run list`). Every hourly issue hour from 2026-10-24 22:00Z on
  was carried by the Worker's dispatch; GitHub's own hourly slots ran twice in that window
  (04:47Z and 00:17Z starts).
- The Worker now has 4 cron triggers (`2,22 * * * *`, `20 13,18 * * *`, `0 3 * * SUN`,
  `0 6 * * MON`), 4 of the free plan's 5.
- **`run_kind` of Cloudflare-started runs.** Until this change a Worker dispatch arrived as a
  plain `workflow_dispatch`, so hourly runs from ~2026-10-24 21:30Z to the deploy of this change
  were recorded `run_kind='manual'` in `pipeline_runs` and `prediction_runs`. M4 (plan §2) counts
  only `scheduled` runs, so those hours count as missed under the strict definition. They are not
  rewritten (no hand edits of live tables, plan §12); the M4 report states the window. From this
  change on the Worker sends `scheduler=cloudflare` and the workflows record `scheduled`.
- Retrain duplicate safety: `reason=weekly` runs are keyed on the Sunday 03:00Z slot; a second
  start ends `skipped` in a few seconds (integration test
  `test_weekly_runs_train_once_per_slot_whoever_starts_them`: Cloudflare start → `ok` +
  3 events; GitHub start 3 h later and a Monday start → `skipped`, 0 events; next Sunday → `ok`).
- Observed latency since the Worker took over (2026-10-24 21:30Z → 2026-10-25 06:30Z, 21 hourly
  runs): Cloudflare-dispatched runs are created at **:02:46 / :22:46** (46 s after the slot),
  start with no queueing, and take **34 s median, 74 s max**. GitHub's own `schedule:` started 3
  of ~25 slots in that window (:09, :17, :47 past the hour). Healthchecks settings derived from
  this: §20.3.
- **Regression and rollback (2026-10-25).** After the Workers Builds deploy of `a58f7f9` (pushed
  06:29Z: 4 triggers incl. `0 3 * * SUN` / `0 6 * * MON`, dispatch body with `inputs`), the Worker
  dispatched nothing: no run for the 07:02, 07:22 or 08:02 slots (later pushes at 06:48, 06:57
  and 07:12Z also redeployed it; 08:02 was 50 min after the last one, past the documented ≤ 15
  min trigger propagation). GitHub's own `schedule:` started nothing in that window either. The
  body itself is valid: replaying `{"ref":"main","inputs":{"scheduler":"cloudflare"}}` with `gh
  api` at 07:13Z created run 40171966968 (`run_kind='scheduled'` in `pipeline_runs` and
  `prediction_runs`; issue hour 07:00Z is therefore labelled scheduled although it was started
  by hand). Issue hour 08:00Z was dispatched by hand as `manual` (run 40175308604). The Worker
  files were restored to `2a75998` (2 triggers, body `{ref: main}`) at ~08:10Z; whether
  dispatching resumes is in §20.5. **Owner check:** Cloudflare dashboard → Workers → gridcast →
  Deployments (did the build after `a58f7f9` fail at the trigger step?) and Settings →
  Triggers (which crons are live; the account's 5-trigger limit counts every Worker).

### 20.2 C15 status branch, `chart_7d.svg`, `config/urls.toml`, fresh clone (ADR-0015)

- `gridcast status build` (daily.yml, after the daily run, both slots) writes `chart_7d.svg`,
  `metrics.json`, a copy of `latest/{forecast,meta,scoreboard}.json` and a README; the `status`
  job (`contents: write`, no other secret) force-pushes them as a one-commit orphan branch.
  Deviation from plan §6.2 step 6: the build and the push are two jobs, so the write token never
  shares a job with the database URL; no `api/v1/*` names (ADR-0011 keys).
- Chart rule: every series at horizon 24 (issued 24 h before the hour), window
  [floor(now) − 7 d, newest champion issue + 24 h], best baseline = lowest MAPE of
  seasonal_naive_168/24/persistence on the scored hours (seasonal_naive_168 when none). First
  local build on live data (2026-10-25 06:32Z): 7,825 B SVG, 0 scored day-ahead hours (the
  first champion day-ahead target is 2026-10-25 01:00Z; its actual lands with the 10-26 07:30 EST
  PUB_Demand file), so the chart shows 7 days of actuals and the first ~29 h of forecasts.
- Fresh clone (`scripts/fresh_clone_check.sh`, empty `UV_CACHE_DIR` and
  `UV_PYTHON_INSTALL_DIR`, no `.env`, Windows 11 laptop, 2026-10-25 ~06:35Z, commit `a58f7f9`):
  clone 2 s → `uv sync --locked` done at 172 s → `gridcast version` → `pytest` 209 passed in
  239 s → **419 s total** (limit 900 s). CI runs the same script on every push to `main`
  (`fresh-clone` job, ubuntu, setup-uv cache off).
- First branch push: daily.yml `workflow_dispatch` run 40170455473 (2026-10-25 06:49Z, 34 s; the
  daily slot itself ended `skipped`, yesterday HE24 not posted yet): jobs `daily` and `status`
  both `success`, `refs/heads/status` = `8b1ca96`, `raw.githubusercontent.com/.../status/chart_7d.svg`
  → 200 `image/svg+xml`, 7,840 B.

### 20.3 C16 `prune.yml` and the Healthchecks settings

**Retention as built** (`jobs/prune.py`, plan §4.4 levers 1 and 3, §6.8): (1) predictions of
`seasonal_naive_24`, `persistence`, `prev_champion` with `issue_time` older than 90 days are
deleted **with their `scores` rows and `prediction_runs` rows in one transaction**, scores first
(§14.4: the 0004 layout has no FK from scores to predictions, so the prune must take the scores
with it or `v_scores` would silently drop them while `scores` kept their bytes); `score_daily`
keeps their aggregates because the daily job only rebuilds the trailing 45 days
(`PRUNE_DAYS 90 > SCORE_DAYS 45`, tested). (2) `raw_demand_history` `realtime_totals` rows older
than 60 days except the newest version per hour. (3) `VACUUM (ANALYZE)` of the touched tables
over `DATABASE_URL_DIRECT`, only when something was deleted. Nothing else is ever deleted;
champion, shadow, seasonal_naive_168 and ieso_adequacy rows are kept forever. Adequacy3
retention stays in the hourly ingest (§14.1).

**Dry run by default.** `gridcast run prune` counts only (`pipeline_runs` status `skipped`, note
`dry-run`, no heartbeat); `--apply` deletes. `prune.yml`: monthly `0 5 1 * *` applies; a manual
dispatch is a dry run unless `apply` is ticked. GitHub cron only: monthly and idempotent, so a
dropped slot costs nothing that the next run or a manual dispatch does not recover.

**Dry run on live Neon (2026-10-25 06:5xZ, local, read-only apart from its own `pipeline_runs`
row):** cutoffs 2026-07-27 06:00Z (weak names) and 2026-08-26 06:00Z (realtime); rows that
would be deleted: scores 0, predictions 0, prediction_runs 0, realtime versions 0 (live
predictions start 2026-10-24; RealtimeTotals stores one complete version per hour). Sizes:
`pg_database_size` 75,595,776 B (75.6 MB); `raw_demand_history` 26.7 MB, `weather_hourly`
26.1 MB, `actuals` 12.0 MB, `ieso_forecast_snapshots` 0.86 MB, `predictions` 0.29 MB,
`pipeline_runs` 0.09 MB. The first real deletion happens on the 2027-02-01 run (the 90-day mark
of 2026-10-24 is 2027-01-22). Year-one projection unchanged from §14.4: ≈ 371 MB with the prune,
≈ 410 MB if a shadow runs all year (§19.2), both under the 450 MB fail line and the 500 MB quota.

**Healthchecks.io settings (owner sets these in the Healthchecks UI; "Simple" schedule).** Derived
from §20.1: a Cloudflare dispatch creates the run ~46 s after its cron minute, it starts with no
queue, and hourly runs last 34 s median / 74 s max, so pings land at ~:03:30 and ~:23:30. Every
`skipped` run also pings success, and `/fail` pings alert at once whatever the grace.

| check (secret) | period | grace | alerts when |
|---|---|---|---|
| hourly (`HEALTHCHECKS_HOURLY_URL`) | 1 h | 30 min | ~90 min without a ping: both Cloudflare slots of an hour and every GitHub fallback missed (was plan 1 h / 40 min) |
| api (`HEALTHCHECKS_API_URL`, pinged by hourly's probe) | 1 h | 30 min | same cadence as hourly; a stale `/health` sends `/fail` at once |
| daily (`HEALTHCHECKS_DAILY_URL`) | 1 day | 2 h | both daily slots of a day missed (pings ~13:22Z and ~18:22Z; was plan 1 d / 6 h) |
| retrain (`HEALTHCHECKS_RETRAIN_URL`) | 7 days | 6 h | no weekly run by Sun ~09:15Z: run ≈ 13 min after 03:00:46Z, 6 h leaves room for the GitHub fallback (seen 2 h 37 min late) |
| keepalive (`HEALTHCHECKS_KEEPALIVE_URL`) | 7 days | 6 h | no Monday run by ~12:00Z |
| prune (`HEALTHCHECKS_PRUNE_URL`) | 31 days | 2 days | GitHub cron only; only scheduled/`--apply` runs ping, so a manual dry run never resets the timer |
| gate (`HEALTHCHECKS_GATE_URL`, C19) | 1 day | 1 day | two daily gate slots in a row missed (GitHub cron only; a one-day slip only delays a decision) |

Secret renames for consistency with the `HEALTHCHECKS_*` names chosen on Day 4: `HC_PRUNE_URL` →
`HEALTHCHECKS_PRUNE_URL`, `HC_GATE_URL` → `HEALTHCHECKS_GATE_URL` (`.env.example`); all optional,
unset = no heartbeat.

CI's `fresh-clone` job on the C15 commit (`2a8d7f7`, ubuntu, empty uv cache): clone 0 s, `uv sync`
3 s, 217 tests in 109 s, **116 s total**.

### 20.4 C19 `gate.yml`: shadow promotion, wait, rejection, rollback (ADR-0016)

- `gridcast gate evaluate | rollback`; dry run by default (no pointer, event, `pipeline_runs` row
  or heartbeat), `--apply` writes. gate.yml: daily `30 6 * * *` applies; a manual dispatch is a
  dry run unless `apply` is ticked. `model-registry` concurrency group with retrain.
- Rules exactly as plan §4.7 items 2-8 (table in ADR-0016). Deviations: rollback lives in the gate
  (not the daily job); only Ontario holidays excuse a rollback gap (no ICI flag exists); the
  shadow's coverage is measured on the intervals it served (backtest × 1.15), the champion's on
  its live-recalibrated ones; the 7-day cooldown is read from the `rolled_back` event's
  `cooldown_until`; exact-boundary values pass.
- The hourly job now writes `prev_champion` rows for 7 days after a gated promotion
  (`champion.json` with `promoted_at` and `previous_version`, pointer `models/champion.prev.json`).
  The bootstrap champion has no `previous_version`, so nothing changes until the first promotion;
  each hourly run makes one extra HEAD request on R2 for the prev pointer.
- `ObjectStore.delete` (DeleteObject) for `shadow.json` / `queued.json` / `champion.prev.json`.
  Permission checked on R2 with the jobs key, 2026-10-25 ~07:00Z: put
  `gate/_delete_probe.json` → exists → delete → gone → delete again (no error).
- keepalive: `gate_stale` (no `ok` gate run in 48 h), armed by the first applied gate run.
- **Live dry runs (local, read-only), 2026-10-25 07:08Z:** `gate evaluate` → pointers champion
  `lgbm-2026.10.23-49ca`, shadow / prev_champion / queued none, cooldown none →
  `hold: no shadow running`. `gate rollback` → refused: `no models/champion.prev.json: nothing to
  roll back to`. No `pipeline_runs` row with `job='gate'` exists afterwards (dry runs write
  none). Nothing was promoted or rolled back.
- Tests: `tests/unit/test_gate_rules.py` (25 table cases: promote, hold for age / targets / peak
  sample / cooldown, reject for MAPE / peak / coverage / blocked version, drift_early for refits
  only, rollback rule incl. the holiday excuse, identical-pair statistics, prev_champion window);
  `tests/integration/test_gate_db.py` (life cycle on Postgres + moto: hold → dry run writes
  nothing → promote + queue hand-over → automatic rollback after 4 days at 5 % vs 2 % → cooldown
  hold → blocked version rejected; MAPE reject; manual rollback; rollback with nothing to restore
  fails); `test_retrain_db.py::test_prev_champion_rows_follow_a_gated_promotion_for_7_days`;
  keepalive `gate_stale`.
- `pg_database_size` on Neon after this session's work: **75,595,776 B (75.6 MB)**.

### 20.5 Open: the Cloudflare dispatch stopped (2026-10-25 06:3xZ) and did not come back

- After the Worker restore (`7d7ff48`, pushed 08:10:28Z) the 08:22 slot (inside the ≤ 15 min
  propagation window) and the **09:02 slot (52 min later) dispatched nothing either**. So the
  stop is not explained by the 4-trigger code alone: something on the Cloudflare side changed
  around the first redeploy (06:3xZ) and persists (candidates: the deploy failed and the triggers
  were dropped; the `GH_DISPATCH_TOKEN` secret or the PAT behind it is gone, expired or
  revoked; an account-level trigger problem). `/health` keeps answering, so the fetch handler
  runs.
- GitHub's own `schedule:` started no hourly run between 04:47Z and 09:05Z although every
  workflow is `active`. Issue hours 07:00Z, 08:00Z and 09:00Z were dispatched by hand (runs
  40171966968, 40175308604, 40179245472).
- **Owner, now:** Cloudflare dashboard → Workers & Pages → gridcast → (1) Deployments/Builds: did
  the builds since 06:29Z succeed? (2) Settings → Triggers: are `2,22 * * * *` and
  `20 13,18 * * *` listed? (3) Settings → Variables and Secrets: is `GH_DISPATCH_TOKEN` present?
  Then GitHub → Settings → Developer settings → fine-grained tokens: is that PAT active? (4)
  Workers Logs (observability is on): the `scheduled` events print one JSON line per trigger,
  e.g. `{"status":"http-401"}` or `"no-token"`. Until this is fixed the hourly loop runs only
  on GitHub cron, which is dropping most slots.
