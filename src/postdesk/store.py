from __future__ import annotations

import os
import sqlite3
from datetime import datetime
from pathlib import Path

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session

from .errors import ConfigError
from .models import Base

DEFAULT_DB_NAME = "postdesk.sqlite3"

# Python 3.14 requires applications to provide SQLite datetime adapters.
sqlite3.register_adapter(datetime, lambda value: value.isoformat())


def resolve_store(value: str | Path | None, *, init: bool = False) -> Path:
    raw = str(value or os.environ.get("POSTDESK_STORE", "")).strip()
    if not raw:
        raise ConfigError("Set --store or POSTDESK_STORE.", path="store")
    path = Path(raw).expanduser()
    looks_like_dir = path.exists() and path.is_dir()
    if init or looks_like_dir or (not path.suffix and not path.exists()):
        path = path / DEFAULT_DB_NAME
    return path.resolve()


def make_engine(path: Path) -> Engine:
    path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        f"sqlite:///{path}",
        future=True,
        connect_args={"check_same_thread": False, "timeout": 30},
    )

    @event.listens_for(engine, "connect")
    def configure_sqlite(dbapi_connection, _record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.close()

    return engine


def initialize(path: Path) -> Engine:
    engine = make_engine(path)
    Base.metadata.create_all(engine)
    return engine


def session_for(path: Path) -> Session:
    engine = initialize(path)
    return Session(engine, expire_on_commit=False)
