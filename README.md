# IMF – Instrument Monitoring Facility (prototype)

Working prototype of an Instrument Monitoring Facility for an Earth-observation
**Fourier-Transform Spectrometer (FTS)**. It covers the complete monitoring chain:

1. **Polling** of new L0 (header information), auxiliary and Level-1 products from the PDGS (OData-style catalogue)
2. **Derivation** of monitoring parameters by plug-in algorithms – scalars, series (sub-bands, scan
   directions, sensors) and spectra – into the **monitoring database (MDB)** with entry type, source and entry time
3. **Windowed analyses** across products: L0→L1 completeness, ZPD versus ancillary data, requirement compliance
4. **Limit checking** (soft/hard limits, persistence per series) and **rules** → events with
   **recommended actions** and **proposed outputs** (laser wavenumber, ISRF scaling, CTF update,
   telecommand proposal, NESR AUX file draft) for expert approval
5. **Mission timeline** generated from the PDGS data, plus manual / imported entries
6. **Reprocessing campaigns** of another processing baseline, in parallel to nominal operation
7. **Periodic reporting** (daily / weekly, plus ad-hoc) as HTML and PDF
8. **Web portal** with role-based access for **admins, experts and guests**, plus a **write API** for external tools
9. **MDB management**: import, purge, export, backup / restore, check / repair (portal and CLI)

A bundled **PDGS simulator** generates synthetic FTS products (L0 headers, ancillary data, special
calibration modes, L1b in two processing baselines) with injected instrument anomalies, so the whole
chain can be demonstrated offline.

See [docs/architecture.md](docs/architecture.md) for the design and
[docs/feature_gap_analysis.md](docs/feature_gap_analysis.md) for the feature requests and their traceability.

## Quick start

Requirements: Python ≥ 3.11 (tested with 3.14 on Windows and in a Linux container).

```powershell
py -m venv .venv
.\.venv\Scripts\python -m pip install -r requirements.txt
.\.venv\Scripts\python run_demo.py --reset
```

Open <http://127.0.0.1:8000> and sign in with one of the demo accounts
`admin/admin`, `expert/expert`, `guest/guest`.

On first start the simulator provides 90 days of mission history. The IMF ingests it in about
2–3 minutes (watch the "ingesting…" indicator), then runs the analyses and trend rules and generates
the latest daily and weekly reports. Afterwards a new orbit appears every ~30 s (live mode).

An existing prototype database is migrated automatically. New parameters are derived only for
products ingested after the upgrade; use `--reset` to see the full scenario.

| Option | Meaning |
|---|---|
| `--reset` | wipe `data/` (database, reports, simulator clock) and start fresh |
| `--days N` | mission history on first start (default 90) |
| `--orbit-every S` | real seconds per simulated orbit in live mode (default 30) |

Other entry points: `python -m pdgs_sim.server` (simulator only), `python -m imf.web.app`
(IMF only), `python -m imf.cli` (operations CLI, below), API docs at <http://127.0.0.1:8000/api/docs>,
tests with `python -m pytest`. Container: see [DOCKER.md](DOCKER.md).

## Suggested demo script (≈ 25 min)

1. **Login page** – show the three roles; mention SSO/OIDC for operations.
2. **Guest** – dashboard, public parameters, *Map* and *Timeline*; *Events*, *Products* and *Admin*
   are not available (also enforced server-side → HTTP 403).
3. **Expert → Dashboard** – subsystem tiles, KPIs incl. *proposed outputs awaiting approval*,
   L1 ingestion vs. L0 earth views (PDGS outage day 30, decontamination day 40, L1 processing gap day 63).
4. **Products** – catalogue per type and baseline: L0 products are header-only (attributes, no
   download), L1 / AUX products keep their SHA-256; product page shows L0 → L1 / AUX → L1 links.
5. **Parameter *NESR index per sub-band*** – 12 series (6 sub-bands × scan direction); cryocooler
   anomaly (day 52–54) and NESR drift from day 75. ***Spectral NESR*** – orbit-averaged spectra vs. the
   spectral template (reference data).
6. **Parameter *Spectral sampling distance*** – requirement band and compliance statistics; the laser mode
   hop (day 70–74) violates the requirement (*SSD requirement compliance* alarm).
7. **Explorer** – `ZPD position offset` + `Turn-around position offset` + `Fringe counter deviation`
   + `ZPD: L1 vs. ancillary`; `Bench temperature` + `Bench sensors: correlation` (thermistor degradation day 65).
8. **Events → category Actions** – E2 (laser wavenumber), E3 (ISRF scaling), E4 (CTF update file),
   E5 (ZPD telecommand proposal), E6 (NESR AUX file draft): open an event, download the proposed file,
   approve / reject (audited).
9. **Timeline** – data gaps, L1 gap, calibration modes (AUX_EFSI), anomalies and actions; add a manual entry.
10. **Admin → Reprocessing** – create campaign `REPRO-B04`, baseline `B04`: it runs in parallel; then
    compare the baselines with the baseline selector (B04 removes the spectral offset and fills the L1 gap).
11. **Admin → MDB management** – backup, check, export ZIP, purge preview, CSV import, new manual parameter,
    API token for an external tool (`POST /api/v1/samples`).
12. **Reports** – daily report with requirement compliance, actions, timeline and NESR vs. template.

## Injected scenario (mission days)

| Day | Anomaly | Visible in |
|---|---|---|
| 0 – 40 | ice build-up on cold optics | ICE_LOSS (warning → alarm), GAIN_DRIFT_B1, TRANSMISSION |
| 15.3, 47.6, 78.9 | special calibration mode (AUX_EFSI) | timeline, E1 |
| 30.2 – 30.7 | PDGS outage | ingestion chart, timeline DATA_GAP, report completeness |
| 40 – 41 | decontamination (no data), ice reset | ICE_LOSS, timeline DATA_GAP |
| 45.0 – 45.3 | missing scans, L0 packet errors | MISSING_SCANS, L0_EO_SCANS, L0_PACKET_ERRORS |
| 52.4 – 54 | cryocooler anomaly | T_DETECTOR, NESR_B1..B3, NESR_IDX, IFG_PEAK_AMP |
| 60 – | slow ZPD drift | ZPD_OFFSET, ANC_TA_OFFSET, ANC_FRINGE_DEV, ZPD_FRINGE_CORR → E5 |
| 61.0 – 61.4 | micro-vibration | SCAN_SPEED_STAB |
| 63.0 – 63.25 | L1 processing anomaly (baseline B03) | L1_COMPLETENESS, timeline L1_GAP |
| 65 – | thermistor degradation | T_BENCH_STAB, T_SENSOR_CORR, T_SENSOR_DIFF |
| 70 – 74 | metrology laser mode hop | LASER_DRIFT, SPEC_SHIFT → E2, SSD / SSD_COMPLIANCE, ZPD_L1_ANC_DIFF |
| 75 – | NESR drift | NESR_IDX_MEAN → E6 (NESR AUX file draft) |
| 77 – | reference / science delay step | REF_SCI_DELAY → E4 (CTF update) |
| 80 – | co-registration drift | COREG_OFFSET (warning, later alarm in live mode) |
| 85 – 86.5 | radiometric feature in the calibration window | ISRF_FEATURE → E3 |
| every 337th orbit | corrupted L1 product (B03 only) | product status FAILED |

## Operations CLI

```sh
python -m imf.cli db check | repair | migrate
python -m imf.cli db backup [--out DIR]
python -m imf.cli db restore FILE --yes                 # IMF stopped; verified before replacing
python -m imf.cli db export --out mdb.zip [--start T] [--end T] [--baseline B03] [--codes SSD,NESR_IDX]
python -m imf.cli db purge --product-type FTS_L1B --start T --end T [--reset-watermarks] [--yes]
python -m imf.cli import samples|timeline FILE.csv
python -m imf.cli import reference FILE.csv --name NESR_TEMPLATE --unit "nW/(cm²·sr·cm⁻¹)"
python -m imf.cli parameter create MISSION_PHASE "Mission phase" --group Manual
python -m imf.cli campaign create REPRO-B04 --baseline B04 && python -m imf.cli campaign run 1
python -m imf.cli token create my-tool --role expert
```

Installed as a package (`pip install .`), the same commands are available as `imf …`.

## Project layout

```
config/imf.yaml          configuration: PDGS product types, baseline, DB, schedules, users, rules, parameters
config/reference/        reference data imported at start-up (NESR spectral template)
pdgs_sim/                PDGS simulator (scenario, HDF5 generators, OData-like server)
imf/ingestion/           PDGS client, poller (watermark per type), reprocessing campaigns
imf/processing/          readers, algorithm registry + FTS / L0 / AUX algorithms, pipeline, windowed analyses
imf/monitoring/          limit checking, rule engine + FTS rules E1–E6, mission timeline, notifier
imf/reporting/           report service (HTML + PDF), matplotlib figures
imf/web/                 FastAPI portal: auth/roles/tokens, pages, admin, JSON + write API, templates
imf/mdb.py               MDB management (import, purge, export, backup/restore, check/repair, tokens)
imf/migrations/          Alembic schema migrations (applied at start-up)
imf/cli.py               operations CLI
imf/services.py          composition root + scheduler (APScheduler)
tests/                   algorithms, limits, rules, ingestion, MDB management, migrations, access control
```
