"""Database engine/session handling and initial seeding from configuration."""
from __future__ import annotations

import logging
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy import create_engine, event, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .config import Settings
from .models import AuditLog, Base, Limit, Parameter, ParameterState, SystemState, User
from .security import hash_password

log = logging.getLogger(__name__)


class Database:
    def __init__(self, url: str):
        connect_args = {}
        if url.startswith("sqlite"):
            connect_args = {"check_same_thread": False, "timeout": 30}
            if url.startswith("sqlite:///") and ":memory:" not in url:
                Path(url[len("sqlite:///"):]).parent.mkdir(parents=True, exist_ok=True)
        self.engine: Engine = create_engine(url, connect_args=connect_args, future=True)
        if url.startswith("sqlite"):
            @event.listens_for(self.engine, "connect")
            def _sqlite_pragmas(dbapi_conn, _record):  # noqa: ANN001
                cur = dbapi_conn.cursor()
                cur.execute("PRAGMA journal_mode=WAL")
                cur.execute("PRAGMA synchronous=NORMAL")
                cur.execute("PRAGMA foreign_keys=ON")
                cur.close()
        self.SessionLocal = sessionmaker(bind=self.engine, expire_on_commit=False)

    def create_all(self) -> None:
        Base.metadata.create_all(self.engine)

    @contextmanager
    def session(self) -> Iterator[Session]:
        s = self.SessionLocal()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()


def seed(db: Database, settings: Settings) -> None:
    """Synchronise parameter definitions and seed users/limits (idempotent)."""
    from .processing.registry import outputs_index

    producers = outputs_index()
    with db.session() as s:
        for i, pc in enumerate(settings.parameters):
            p = s.scalar(select(Parameter).where(Parameter.code == pc.code))
            if p is None:
                p = Parameter(code=pc.code, visibility=pc.visibility)
                s.add(p)
                log.info("Registered parameter %s", pc.code)
            # Definitions are configuration-controlled; visibility is admin-managed after creation.
            p.name, p.unit, p.group = pc.name, pc.unit, pc.group
            alg = producers.get(pc.algorithm or pc.code)
            if alg is None:
                log.warning("No algorithm registered for parameter %s", pc.code)
            p.algorithm = f"{alg.name} v{alg.version}" if alg else "unassigned"
            p.description = pc.description
            p.sort_order = i
            s.flush()
            if pc.limits and not p.limits:
                s.add(Limit(parameter_id=p.id, changed_by="config", comment="initial limits from configuration",
                            **pc.limits.model_dump()))
            if p.state is None:
                s.add(ParameterState(parameter_id=p.id))

        for uc in settings.users:
            if s.scalar(select(User).where(User.username == uc.username)) is None:
                s.add(User(username=uc.username, full_name=uc.full_name,
                           pw_hash=hash_password(uc.password), role=uc.role))
                s.add(AuditLog(username="system", action="user.create", target=uc.username,
                               details=f"seeded from configuration, role={uc.role}"))


def get_state(s: Session, key: str, default: str | None = None) -> str | None:
    row = s.get(SystemState, key)
    return row.value if row else default


def set_state(s: Session, key: str, value: str) -> None:
    row = s.get(SystemState, key)
    if row:
        row.value = value
    else:
        s.add(SystemState(key=key, value=value))
