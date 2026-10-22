# IMF – Architecture and Design

## 1. Purpose and scope

The Instrument Monitoring Facility (IMF) continuously assesses the health and performance of
the instrument from its L0 headers and Level-1 products. It

* retrieves new products of the two configured product types (L0 header, L1) from the Payload Data
  Ground Segment (PDGS) and reads L1 files in HDF5 or netCDF4,
* derives monitoring parameters (scalars, series per sub-band / band / sensor, spectra) and
  archives them in the monitoring database (MDB) with full traceability,
* analyses data across products and over time windows (L0→L1 completeness, requirement compliance),
* detects limit violations and rule conditions, raises / notifies events, recommends actions and
  drafts the corresponding outputs for expert approval,
* maintains a mission timeline generated from the PDGS data,
* processes reprocessing campaigns in parallel to the nominal operation,
* produces periodic reports with trend plots, and
* gives admins, instrument experts, guests and external tools role-dependent access to all of this.

The core is instrument-independent: it works with configurable product levels (default: L0 and
L1, one product type each) and two file formats (HDF5, netCDF4). Everything instrument-specific - the L1 reader adapter, algorithms, analyses, rules
and the parameter catalogue - is a plug-in plus configuration. The repository ships a
Fourier-Transform Spectrometer (FTS) as example instrument, with a PDGS simulator as data source. The mapping of the feature requests onto this design is documented in
[feature_gap_analysis.md](feature_gap_analysis.md).

## 2. Context

```mermaid
flowchart LR
  PDGS[(PDGS<br/>L0 / L1 catalogue + archive)] -- "OData query / download" --> IMF
  IMF -- "e-mail / notification log" --> Team[Instrument team]
  IMF -- "approved proposals<br/>(values, file drafts)" --> Ops[Operations / FOS<br/>manual hand-over]
  Admin((Admin)) -- HTTPS --> IMF
  Expert((Expert)) -- HTTPS --> IMF
  Guest((Guest)) -- HTTPS --> IMF
  Tool[External tools] -- "write API (token)" --> IMF
  IdP[Identity provider<br/>OIDC - operations] -.-> IMF
```

There is no interface to the FOS: the IMF never sends commands or files itself.

## 3. Components

```mermaid
flowchart TB
  subgraph IMF
    SCH[Scheduler<br/>APScheduler] --> POL
    SCH --> POST[Post-processing job]
    POL[Poller<br/>watermark per product type] --> CLI[PDGS client<br/>OData adapter]
    CMP[Reprocessing campaigns<br/>own watermark / worker] --> CLI
    POL --> PIPE[Processing pipeline]
    CMP --> PIPE
    PIPE --> RDR[Readers<br/>HDF5 · netCDF4 · header-only]
    PIPE --> REG[Algorithm registry<br/>plug-ins · entry points · external tools]
    PIPE --> LIM[Limit checker<br/>per series · persistence]
    POST --> ANA[Windowed analyses]
    POST --> TRD[Trend rules]
    POST --> REP[Report service<br/>HTML + PDF]
    LIM --> RUL[Rule engine<br/>product · limit · trend]
    ANA --> LIM
    TRD --> RUL
    RUL --> EVT[Events + proposed outputs] --> NOT[Notifier]
    RUL --> TL[Mission timeline]
    PIPE --> DB[(MDB)]
    MDB[MDB management<br/>import · purge · export<br/>backup · check · repair] --> DB
    WEB[Web portal + JSON / write API<br/>FastAPI · Plotly] --> DB
    WEB --> AUTH[AuthN / role guards / API tokens]
    CLIx[CLI<br/>python -m imf.cli] --> MDB
  end
  CLI --> PDGS[(PDGS)]
```

| Component | Module | Responsibility |
|---|---|---|
| PDGS client | `imf/ingestion/pdgs_client.py` | Catalogue query per product type (`PublicationDate gt <watermark>`, optional baseline attribute filter, `$expand=Attributes`), download with SHA-256, retry with back-off. Only component that knows the PDGS interface. |
| Poller | `imf/ingestion/poller.py` | Polls the configured product types (L0 first, then L1 of the nominal baseline), one watermark per type, skips known products, one DB transaction per product. Header-only types are not downloaded. Failed products are stored as FAILED and do not block the chain. |
| Reprocessing campaigns | `imf/ingestion/campaigns.py` | Ingest another baseline over the mission or a period, with their own watermark, in a worker thread or a separate process (`imf.cli campaign run`). |
| Readers | `imf/processing/readers.py`, `fts_reader.py` | File formats: `HDF5File` and `NetCDF4File` give uniform access (attributes, arrays by path); the format is set per product type. Product levels: an instrument registers an adapter per level (`register_reader("L1", L1Product)`) that reads through them; header-only products are represented by their catalogue attributes. A real ICD replaces only the adapter. |
| Algorithm registry | `imf/processing/registry.py` | `@algorithm("CODE", ..., inputs=("L0" / "L1"))` - plug-ins refer to levels, not to mission product names; results are floats, `Dim` (series values) or `Vector` (spectra). Plug-ins from built-in modules, `processing.plugin_modules`, entry points `imf.algorithms`, and external executables (JSON contract). Name and version are stored as the source of each sample. |
| Algorithms (example FTS) | `fts_algorithms.py` | L0 header (scans, calibration views, packet errors); NESR (deep space, index per sub-band, spectral), transmission, gain drift, ice, spectral shift, ILS, ISRF feature, SSD, laser, ZPD, interferogram, reference/science delay, temperatures (incl. two bench sensor types), quality flags, geometry. |
| Windowed analyses | `imf/processing/analyses.py`, `fts_analyses.py` | Per day (data time), once all input levels are complete: L0→L1 completeness, product links and timeline gaps; SSD requirement compliance. Per baseline. |
| Limit checker | `imf/monitoring/limits.py` | Classifies each sample, confirms level changes after *N* consecutive samples per series (parameter, baseline, dims), opens and clears events. |
| Rule engine | `imf/monitoring/rules.py`, `fts_rules.py` | Product, limit and trend rules (E1–E6). Attaches recommended actions and generates proposed outputs (values, files, command proposals). Nominal baseline only. |
| Mission timeline | `imf/monitoring/timeline.py` | Data gaps, L1 processing gaps, calibration modes, anomalies, actions, campaigns; manual, imported and external entries. |
| Notifier | `imf/monitoring/notifier.py` | Notification log + optional SMTP, including recommended actions. |
| Report service | `imf/reporting/` | Data-time-driven scheduling, statistics per parameter, requirement compliance, actions, timeline, NESR versus template, trend plots; HTML and PDF. |
| MDB management | `imf/mdb.py`, `imf/cli.py` | Manual parameters and values, CSV imports (samples, timeline, reference data), purge, export, backup / restore, check, repair, API tokens. |
| Migrations | `imf/migrate.py`, `imf/migrations/` | Alembic revisions, applied at start-up; prototype databases are stamped and upgraded. |
| Web portal / API | `imf/web/` | Dashboard, parameters (series, spectra, requirements), explorer with baseline selector, map, timeline, products, events with approval of proposed outputs, reports, administration; JSON API and write API `/api/v1` (OpenAPI at `/api/docs`). |

## 4. Data flow

```mermaid
sequenceDiagram
  participant S as Scheduler
  participant P as Poller
  participant G as PDGS
  participant A as Algorithms
  participant L as Limit checker / rules
  participant D as MDB
  S->>P: every 20 s
  loop product types (L0, L1 nominal)
    P->>G: GET Products?$filter=ProductType eq T and PublicationDate gt W_T
    G-->>P: entries + attributes
    loop each product (own transaction)
      alt header-only
        P->>A: header algorithms (catalogue attributes)
      else full
        P->>G: GET Products(Id)/$value (SHA-256)
        P->>A: algorithms registered for the level of T (reader for its format)
      end
      A-->>P: scalars / series / spectra
      P->>L: check each series, product and limit rules
      L->>D: samples, vectors, states, events, proposals, timeline
      P->>D: product, advance W_T
    end
  end
  S->>D: post-processing: windowed analyses -> trend rules -> reports
```

Time handling: all monitoring is based on **data (sensing) time**, not wall-clock time. Plots, time
windows, analysis windows, trend evaluation steps and periodic reports are anchored at the data
time. Backlog ingestion, live operation and reprocessing therefore produce identical results.

## 5. Monitoring database

```mermaid
erDiagram
  PRODUCTS ||--o{ SAMPLES : "derived from (optional)"
  PRODUCTS ||--o{ VECTOR_SAMPLES : "derived from"
  PRODUCTS ||--o{ PRODUCT_LINKS : "L0 -> L1"
  PARAMETERS ||--o{ SAMPLES : has
  PARAMETERS ||--o{ VECTOR_SAMPLES : has
  PARAMETERS ||--o{ LIMITS : "versioned"
  PARAMETERS ||--o{ REQUIREMENTS : "versioned"
  PARAMETERS ||--o{ PARAMETER_STATE : "per series"
  PARAMETERS ||--o{ EVENTS : raises
  EVENTS ||--o{ PROPOSED_OUTPUTS : proposes
  EVENTS ||--o{ TIMELINE_ENTRIES : "logged as"
  PARAMETERS ||--o{ ANNOTATIONS : has
  USERS ||--o{ AUDIT_LOG : performs
  CAMPAIGNS ||--o{ PRODUCTS : reprocesses
  REFERENCE_DATA
  API_TOKENS
  REPORTS
  POLL_RUNS
```

* **Products** carry level (L0 / L1), baseline, processor and product version, SHA-256 and
  header attributes. Only the checksum of a downloaded product is kept (`keep_products: false`).
* **Samples** (parameter, product, time, value, level, dims, baseline, entry type, source, entry
  time) form the time-series table, indexed on (parameter, time). Manual, imported and external
  samples have no product. In operations: PostgreSQL + TimescaleDB hypertable.
* **Series**: `dims` (e.g. `sb=3` or `sensor=PRT`) identifies a series of a parameter. Limits apply to all
  series of a parameter, the persistence state is kept per (parameter, baseline, dims).
* **Baselines**: L1-derived data carries its processing baseline. L0 and manual data are
  baseline-independent (`""`) and appear in every baseline view. Rules act on the nominal baseline only.
* **Vector samples** store spectra and profiles (axis + values, JSON).
* `limits` and `requirements` are **versioned** (never updated in place): who changed what, when and why.
* Full traceability: samples, vectors and events reference their source product; analysis outputs
  reference their analysis, and proposed outputs reference their event and rule.
* Schema changes are Alembic migrations (`imf/migrations/versions`).

## 6. Events

| Item | Behaviour |
|---|---|
| Categories | **limit** (limit violation), **action** (rule condition with recommended action and proposed outputs), **timeline** (informational, e.g. calibration mode) |
| Levels | INFO / WARNING (soft limit) / ALARM (hard limit), low and high limits |
| Persistence | Level change confirmed after *N* consecutive samples (per series) |
| Escalation | WARNING → ALARM opens a new ALARM event |
| Clearing | Back to nominal (with persistence) sets the *cleared* time; trend rules clear with hysteresis |
| Lifecycle | OPEN → ACKNOWLEDGED (expert, with comment) → CLOSED once acknowledged and cleared |
| Proposed outputs | DRAFT → APPROVED / REJECTED by an expert (audited); files are kept with SHA-256 |
| Notification | On raise and clear (with the recommended action); minimum severity configurable; INFO not notified |

| Rule | Kind | Trigger | Proposed output |
|---|---|---|---|
| E1 | product | L0 header `observationMode` ≠ NOMINAL | Timeline entry "calibration mode" (consecutive L0 products merged) |
| E2 | limit | `SPEC_SHIFT` threshold | New visible laser wavenumber (value) |
| E3 | limit | `ISRF_FEATURE` threshold | Radiometric scaling of the ISRF (value) |
| E4 | limit | `REF_SCI_DELAY` threshold | Complex transfer function update (CSV) |
| E5 | trend | `ZPD_OFFSET` slope | ZPD offset correction (value) |
| E6 | trend | `NESR_IDX_MEAN` slope | New version of the `NESR_TEMPLATE` reference (CSV, re-importable) from the orbit-averaged spectral NESR |

E1-E6 belong to the example instrument (`fts_rules.py`); the rule kinds and the proposal workflow are generic.

Trend rules use a robust Theil–Sen slope over a sliding window, evaluated in fixed data-time steps.
They need confirmation over consecutive steps, clear with hysteresis, and exclude samples that
already violate a limit, because excursions are handled by limit events.

## 7. Users and roles

| Capability | Guest | Expert | Admin | API token |
|---|:-:|:-:|:-:|:-:|
| Dashboard, public parameters, map, timeline, published reports | ✔ | ✔ | ✔ | |
| All parameters, products, events, CSV / MDB export | | ✔ | ✔ | |
| Acknowledge events, approve proposed outputs, annotations, manual values, timeline entries, ad-hoc reports | | ✔ | ✔ | |
| Limits, requirements, visibility, users, campaigns, imports, purge, backup, repair, tokens, jobs, audit log | | | ✔ | |
| Write samples / spectra / timeline entries (`/api/v1`) | | ✔ | ✔ | ✔ (expert / admin token) |
| Create parameters via API | | | ✔ | ✔ (admin token) |

Access control is enforced server-side for both pages and API (FastAPI dependencies);
the UI merely hides what is not accessible. Role/active state is re-validated on every request.
All security-relevant actions are written to the audit log. API tokens are stored as SHA-256 hashes.

## 8. Configuration

`config/imf.yaml` (validated with pydantic) contains:

* PDGS endpoint, the product levels (`pdgs.levels`, default `[L0, L1]`, in ingestion order), one product type per level (header / full mode, file format hdf5 / netcdf4), nominal baseline and polling;
* database URL, reporting schedule, notification;
* processing directories, plug-in modules and external tools;
* seed users, instrument characterisation data, reference data files and rule parameters;
* the parameter catalogue with initial limits, requirements, series dimensions and kind.

Parameter definitions are configuration-controlled. Limits, requirements and visibility are
subsequently maintained by administrators in the DB.

## 9. Prototype vs. operational deployment

| Aspect | Prototype | Operational proposal |
|---|---|---|
| PDGS interface | Simulator, OData-like (L0 headers, L1 in two baselines, HDF5 or netCDF4) | ICD-compliant adapter (e.g. PRIP / CDSE OData), authentication, checksums |
| Database | SQLite (WAL) with Alembic migrations | PostgreSQL + TimescaleDB, backups (`pg_dump` via the CLI), HA |
| Processing | In-process; campaigns in a thread or a separate CLI process | Worker pool / queue for re-processing campaigns, containerised |
| Authentication | Local accounts (scrypt), API tokens | SSO via OIDC (Keycloak federated with agency IdP), MFA |
| Deployment | `run_demo.py`, single container ([DOCKER.md](../DOCKER.md)) | Compose / Kubernetes: `imf-web`, `imf-worker`, PostgreSQL, reverse proxy with TLS, monitoring of the IMF itself |
| Notifications | Log file (SMTP optional) | E-mail, ticketing / anomaly-report system integration |
| Reports | HTML + PDF | + configurable templates, report sign-off workflow |
| Algorithms | Python plug-ins, entry points, external executables | Configuration-controlled, validated against reference data, versioned with re-processing |

## 10. Extension points

* **New parameter**: write an `@algorithm` function (or an external tool), add the parameter to the
  YAML. Use `Dim` for series and `Vector` for spectra.
* **Other product names / file format**: set the type name and `format` (hdf5 / netcdf4) of the L0 and
  L1 entries in `pdgs.product_types`; plug-ins are unaffected because they refer to the level.
* **New product layout**: register an adapter for the level (`register_reader("L1", MyL1Product)`),
  derived from `DatasetProduct`, so it works for both file formats.
* **Additional level** (e.g. auxiliary data): add it to `pdgs.levels` before the levels that use it
  (`[L0, AUX, L1]`), add one product type with `level: AUX`, and write plug-ins with `inputs=("AUX",)`
  (rules: `watches=("AUX",)`, analyses: `ctx.products("AUX", ...)`). Without a registered adapter the
  product is opened as a generic `DatasetProduct` (`product.read("Group/var")`). Levels other than L1
  are baseline-independent.
* **New analysis**: `@analysis("CODE", inputs=(...))` in a plug-in module, evaluated per day.
* **New rule**: `@rule("CODE", "product" | "limit" | "trend", ...)`; parameters in the `rules:` configuration.
* **External tools**: Python packages via the entry-point groups `imf.algorithms`, `imf.rules`,
  `imf.analyses`; executables via `processing.external_tools`; any language via the write API `/api/v1`.
* **New mission / instrument**: a reader adapter + algorithm / analysis / rule modules + parameter
  catalogue (`processing.plugin_modules`, entry points), same core.
