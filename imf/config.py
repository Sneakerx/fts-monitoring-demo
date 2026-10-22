"""Configuration loading and validation (YAML -> pydantic models)."""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field, model_validator

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = PROJECT_ROOT / "config" / "imf.yaml"

Role = Literal["admin", "expert", "guest"]


class MissionCfg(BaseModel):
    name: str
    instrument: str
    orbit_period_min: float = 101.0


Level = Literal["L0", "L1"]
Format = Literal["hdf5", "netcdf4"]


class ProductTypeCfg(BaseModel):
    """One PDGS product type to ingest. The IMF handles two product types, one per level.

    mode 'header': only the catalogue attributes (header information) are stored and processed,
    the product itself is not downloaded (e.g. large L0 products). mode 'full': download + reader
    for the file format (HDF5 or netCDF4).
    """
    type: str
    level: Level = "L1"
    mode: Literal["header", "full"] = "full"
    format: Format = "hdf5"
    baseline_filter: bool | None = None  # default: True for L1 (only the nominal baseline is polled)

    @property
    def filters_baseline(self) -> bool:
        return self.level == "L1" if self.baseline_filter is None else self.baseline_filter


class PdgsCfg(BaseModel):
    base_url: str
    baseline: str = "B03"  # nominal processing baseline
    product_types: list[ProductTypeCfg] = Field(default_factory=lambda: [ProductTypeCfg(type="L1")])
    poll_interval_s: int = 20
    page_size: int = 100
    download_dir: Path = Path("data/products")
    keep_products: bool = False
    timeout_s: float = 30.0
    retries: int = 3

    @model_validator(mode="after")
    def _one_type_per_level(self) -> "PdgsCfg":
        levels = [t.level for t in self.product_types]
        if len(set(levels)) != len(levels):
            raise ValueError("pdgs.product_types: at most one product type per level (L0, L1)")
        return self

    def type_cfg(self, product_type: str) -> ProductTypeCfg:
        for t in self.product_types:
            if t.type == product_type:
                return t
        return ProductTypeCfg(type=product_type)

    def type_for_level(self, level: str) -> str | None:
        """Configured product type of a level (None if the level is not ingested)."""
        return next((t.type for t in self.product_types if t.level == level), None)

    def level_of(self, product_type: str) -> str:
        return self.type_cfg(product_type).level


class ExternalToolCfg(BaseModel):
    """External processing tool run per product: `command + [product_path]` prints {"CODE": value} as JSON."""
    name: str
    command: list[str]
    inputs: list[str] = Field(default_factory=lambda: ["L1"])  # product levels
    outputs: list[str]
    version: str = "1.0"
    timeout_s: float = 60.0


class ProcessingCfg(BaseModel):
    outputs_dir: Path = Path("data/outputs")     # files proposed by rules (drafts awaiting approval)
    backup_dir: Path = Path("data/backups")
    export_dir: Path = Path("data/exports")
    analysis_margin_h: float = 4.0               # a window is analysed once data this far beyond its end exists
    plugin_modules: list[str] = Field(default_factory=list)  # additional algorithm / rule / analysis modules
    external_tools: list[ExternalToolCfg] = Field(default_factory=list)


class DatabaseCfg(BaseModel):
    url: str = "sqlite:///data/imf.db"


class WebCfg(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8000
    secret_key: str = "change-me"
    session_max_age_s: int = 8 * 3600


class ReportingCfg(BaseModel):
    output_dir: Path = Path("data/reports")
    check_interval_s: int = 30
    completeness_margin_h: float = 4.0
    daily_backfill: int = 3
    weekly_backfill: int = 2
    auto_publish: bool = True


class NotificationCfg(BaseModel):
    log_file: Path = Path("data/notifications.log")
    min_severity: Literal["WARNING", "ALARM"] = "WARNING"
    smtp_host: str | None = None
    smtp_port: int = 25
    sender: str = "imf@example.org"
    recipients: list[str] = Field(default_factory=list)


class UserCfg(BaseModel):
    username: str
    password: str
    role: Role
    full_name: str = ""


class LimitCfg(BaseModel):
    warn_lo: float | None = None
    warn_hi: float | None = None
    alarm_lo: float | None = None
    alarm_hi: float | None = None
    persistence: int = 1


class RequirementCfg(BaseModel):
    lo: float | None = None
    hi: float | None = None
    text: str = ""
    reference: str = ""


class ParameterCfg(BaseModel):
    code: str
    name: str
    unit: str = ""
    group: str
    description: str = ""
    visibility: Literal["public", "restricted"] = "restricted"
    algorithm: str | None = None  # defaults to the parameter code
    kind: Literal["scalar", "vector"] = "scalar"
    dims: str = ""  # names of the series dimensions, e.g. "band,sensor"
    limits: LimitCfg | None = None
    requirement: RequirementCfg | None = None


class ReferenceDataCfg(BaseModel):
    """Reference data imported at start-up if not yet present (CSV: [dims...,] axis, value)."""
    name: str
    file: Path
    unit: str = ""
    description: str = ""


class Settings(BaseModel):
    mission: MissionCfg
    pdgs: PdgsCfg
    database: DatabaseCfg = DatabaseCfg()
    web: WebCfg = WebCfg()
    reporting: ReportingCfg = ReportingCfg()
    notifications: NotificationCfg = NotificationCfg()
    processing: ProcessingCfg = ProcessingCfg()
    users: list[UserCfg] = Field(default_factory=list)
    characterisation: dict = Field(default_factory=dict)
    rules: dict[str, dict] = Field(default_factory=dict)
    reference_data: list[ReferenceDataCfg] = Field(default_factory=list)
    parameters: list[ParameterCfg] = Field(default_factory=list)

    def resolve_paths(self, root: Path) -> "Settings":
        def r(p: Path) -> Path:
            return p if p.is_absolute() else (root / p)

        self.pdgs.download_dir = r(self.pdgs.download_dir)
        self.reporting.output_dir = r(self.reporting.output_dir)
        self.notifications.log_file = r(self.notifications.log_file)
        self.processing.outputs_dir = r(self.processing.outputs_dir)
        self.processing.backup_dir = r(self.processing.backup_dir)
        self.processing.export_dir = r(self.processing.export_dir)
        for ref in self.reference_data:
            ref.file = r(ref.file)
        prefix = "sqlite:///"
        if self.database.url.startswith(prefix) and not self.database.url.startswith(prefix + "/"):
            db_path = self.database.url[len(prefix):]
            if db_path != ":memory:" and not Path(db_path).is_absolute():
                self.database.url = prefix + (root / db_path).as_posix()
        return self


def load_settings(path: str | Path | None = None) -> Settings:
    path = Path(path or os.environ.get("IMF_CONFIG", DEFAULT_CONFIG))
    with open(path, encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    return Settings.model_validate(raw).resolve_paths(PROJECT_ROOT)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()
