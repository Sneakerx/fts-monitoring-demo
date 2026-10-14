"""Schema migrations (Alembic), run at start-up and from the CLI."""
from __future__ import annotations

import logging
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import inspect

log = logging.getLogger(__name__)
SCRIPT_LOCATION = Path(__file__).parent / "migrations"
BASELINE_REVISION = "0001"


def alembic_config(url: str, nominal_baseline: str = "B03") -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(SCRIPT_LOCATION))
    cfg.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    cfg.attributes["nominal_baseline"] = nominal_baseline
    return cfg


def head_revision() -> str:
    return ScriptDirectory.from_config(alembic_config("sqlite://")).get_current_head()


def current_revision(engine) -> str | None:
    with engine.connect() as conn:
        return MigrationContext.configure(conn).get_current_revision()


def upgrade(db, nominal_baseline: str = "B03") -> None:
    """Bring the database to the latest revision. Prototype databases created without Alembic
    (tables present, no version table) are stamped with the baseline revision first."""
    cfg = alembic_config(str(db.engine.url.render_as_string(hide_password=False)), nominal_baseline)
    with db.engine.begin() as conn:
        cfg.attributes["connection"] = conn
        tables = set(inspect(conn).get_table_names())
        if "alembic_version" not in tables and "products" in tables:
            log.info("Existing database without migration history - stamping baseline %s", BASELINE_REVISION)
            command.stamp(cfg, BASELINE_REVISION)
        before = MigrationContext.configure(conn).get_current_revision()
        command.upgrade(cfg, "head")
        after = MigrationContext.configure(conn).get_current_revision()
    if before != after:
        log.info("Database migrated %s -> %s", before, after)
