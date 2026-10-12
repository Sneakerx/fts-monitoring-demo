# IMF – Instrument Monitoring Facility (prototype)

Working prototype of an Instrument Monitoring Facility for an Earth-observation
**Fourier-Transform Spectrometer (FTS)**. It covers the complete monitoring chain:

1. **Polling** of the latest Level-1 products from the PDGS (OData-style catalogue)
2. **Derivation** of monitoring parameters by plug-in algorithms → **monitoring database**
3. **Limit checking** (soft/hard limits, persistence) → **events** + notifications
4. **Periodic reporting** (daily / weekly, plus ad-hoc) as HTML and PDF with trend plots
5. **Web portal** with role-based access for **admins, experts and guests**

A bundled **PDGS simulator** generates realistic synthetic FTS L1b products (HDF5) with
injected instrument anomalies, so the whole chain can be demonstrated offline.

See [docs/architecture.md](docs/architecture.md) for the design.

## Quick start

Requirements: Python ≥ 3.11 (tested with 3.14 on Windows).

```powershell
py -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
.\.venv\Scripts\python run_demo.py --reset
```

Open <http://127.0.0.1:8000> and sign in with one of the demo accounts
`admin/admin`, `expert/expert`, `guest/guest`.

On first start the simulator provides 90 days of mission history; the IMF ingests it in
about one minute (watch the "ingesting…" indicator), then generates the latest daily and
weekly reports. Afterwards a new orbit product appears every ~30 s (live mode).

| Option | Meaning |
|---|---|
| `--reset` | wipe `data/` (database, reports, simulator clock) and start fresh |
| `--days N` | mission history on first start (default 90) |
| `--orbit-every S` | real seconds per simulated orbit in live mode (default 30) |

Other entry points: `python -m pdgs_sim.server` (simulator only), `python -m imf.web.app`
(IMF only), API docs at <http://127.0.0.1:8000/api/docs>, tests with `python -m pytest`.

## Suggested demo script (≈ 15 min)

1. **Login page** – show the three roles; mention SSO/OIDC for operations.
2. **Guest** – dashboard shows only public parameters and published reports; *Events*,
   *Products* and *Admin* are not available (also enforced server-side → HTTP 403).
3. **Expert → Dashboard** – subsystem tiles (Radiometric shows *ice contamination* warning,
   Geometric shows *co-registration drift*), live KPIs, data-ingestion chart with the
   PDGS outage (day 30) and decontamination gap (day 40), failed (corrupted) products in red.
4. **Parameter page *Ice contamination*** – range `all`: build-up, alarm, decontamination,
   renewed build-up. Shaded = event periods. Click a point → source product (traceability),
   add an annotation ("decontamination planned").
5. **Explorer** – `Metrology laser wavelength drift` + `Spectral calibration shift`:
   the laser mode hop (day 70–74) explains the spectral shift; `Detector temperature` +
   `NESR Band 1/2/3`: cryocooler anomaly (day 52–54) degrades the radiometric noise.
6. **Events** – lifecycle, acknowledge an event with a comment.
7. **Reports** – open a daily report (HTML) and its PDF; generate an ad-hoc 7-day report.
8. **Admin** – system health (PDGS reachable, scheduler jobs, polling runs), change a limit
   (versioned + audit trail), toggle a parameter to public, create a user, audit log.
9. **Live** – keep the dashboard open: every ~30 s a new product is ingested (toast), values update.

## Injected scenario (mission days)

| Day | Anomaly | Visible in |
|---|---|---|
| 0 – 40 | ice build-up on cold optics | ICE_LOSS (warning → alarm), GAIN_DRIFT_B1 |
| 30.2 – 30.7 | PDGS outage | ingestion chart, report completeness |
| 40 – 41 | decontamination (no data), ice reset | ICE_LOSS |
| 45.0 – 45.3 | missing scans burst | MISSING_SCANS |
| 52.4 – 54 | cryocooler anomaly | T_DETECTOR, NESR_B1..B3, IFG_PEAK_AMP |
| 61.0 – 61.4 | micro-vibration | SCAN_SPEED_STAB |
| 70 – 74 | metrology laser mode hop | LASER_DRIFT, SPEC_SHIFT, ZPD_OFFSET |
| 80 – | co-registration drift | COREG_OFFSET (warning, later alarm in live mode) |
| every 337th orbit | corrupted product | product status FAILED |

## Project layout

```
config/imf.yaml          configuration: PDGS, DB, schedules, users, parameters + initial limits
pdgs_sim/                PDGS simulator (scenario, HDF5 generator, OData-like server)
imf/ingestion/           PDGS client + poller (watermark, idempotent, retries)
imf/processing/          L1 reader, algorithm plug-in registry, FTS algorithms, pipeline
imf/monitoring/          limit checking / event generation, notifier
imf/reporting/           report service (HTML + PDF), matplotlib figures
imf/web/                 FastAPI portal: auth/roles, pages, admin, JSON API, templates
imf/services.py          composition root + scheduler (APScheduler)
tests/                   algorithm, limit, ingestion and access-control tests
```
