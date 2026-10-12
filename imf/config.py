"""Configuration loading and validation (YAML -> pydantic models)."""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG = PROJECT_ROOT / "config" / "imf.yaml"

Role = Literal["admin", "expert", "guest"]


class MissionCfg(BaseModel):
    name: str
    instrument: str
    orbit_period_min: float = 101.0


class PdgsCfg(BaseModel):
    base_url: str
    product_type: str
    poll_interval_s: int = 20
    page_size: int = 100
    download_dir: Path = Path("data/products")
    keep_products: bool = False
    timeout_s: float = 30.0
    retries: int = 3


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


class ParameterCfg(BaseModel):
    code: str
    name: str
    unit: str = ""
    group: str
    description: str = ""
    visibility: Literal["public", "restricted"] = "restricted"
    algorithm: str | None = None  # defaults to the parameter code
    limits: LimitCfg | None = None


class Settings(BaseModel):
    mission: MissionCfg
    pdgs: PdgsCfg
    database: DatabaseCfg = DatabaseCfg()
    web: WebCfg = WebCfg()
    reporting: ReportingCfg = ReportingCfg()
    notifications: NotificationCfg = NotificationCfg()
    users: list[UserCfg] = Field(default_factory=list)
    characterisation: dict = Field(default_factory=dict)
    parameters: list[ParameterCfg] = Field(default_factory=list)

    def resolve_paths(self, root: Path) -> "Settings":
        def r(p: Path) -> Path:
            return p if p.is_absolute() else (root / p)

        self.pdgs.download_dir = r(self.pdgs.download_dir)
        self.reporting.output_dir = r(self.reporting.output_dir)
        self.notifications.log_file = r(self.notifications.log_file)
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
