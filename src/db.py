"""Database connectivity for Cadence.

Every credential is read from the environment (loaded from a local ``.env`` via
python-dotenv). Nothing in this module hardcodes a host, user, or password, so the
same code runs against a local Docker Postgres, CI, or a managed instance.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv
from sqlalchemy import Engine, create_engine, text

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[1]

load_dotenv(PROJECT_ROOT / ".env")

_ENGINE: Engine | None = None


def database_url() -> str:
    """Build a SQLAlchemy connection URL from environment variables.

    Raises:
        RuntimeError: if a required variable is missing, so failures surface at
            startup rather than as a confusing connection error later.
    """
    required = {
        "DB_HOST": os.getenv("DB_HOST"),
        "DB_PORT": os.getenv("DB_PORT", "5433"),
        "DB_USER": os.getenv("DB_USER"),
        "DB_PASSWORD": os.getenv("DB_PASSWORD"),
        "DB_NAME": os.getenv("DB_NAME"),
    }
    missing = [key for key, value in required.items() if not value]
    if missing:
        raise RuntimeError(
            f"Missing database environment variables: {', '.join(missing)}. "
            "Copy .env.example to .env and fill it in."
        )
    return (
        f"postgresql+psycopg2://{required['DB_USER']}:{required['DB_PASSWORD']}"
        f"@{required['DB_HOST']}:{required['DB_PORT']}/{required['DB_NAME']}"
    )


def get_engine() -> Engine:
    """Return a process-wide SQLAlchemy engine, created on first use."""
    global _ENGINE
    if _ENGINE is None:
        _ENGINE = create_engine(database_url(), pool_pre_ping=True, future=True)
        logger.debug("created database engine")
    return _ENGINE


@contextmanager
def connection() -> Iterator:
    """Yield a connection inside a transaction, committing on clean exit."""
    engine = get_engine()
    with engine.begin() as conn:
        yield conn


def read_sql(query: str, params: dict | None = None) -> pd.DataFrame:
    """Run a SELECT and return the result as a DataFrame."""
    with get_engine().connect() as conn:
        return pd.read_sql(text(query), conn, params=params or {})


def read_sql_file(path: str | Path, params: dict | None = None) -> pd.DataFrame:
    """Run a SELECT stored in a ``.sql`` file and return a DataFrame."""
    sql_path = Path(path)
    if not sql_path.is_absolute():
        sql_path = PROJECT_ROOT / sql_path
    return read_sql(sql_path.read_text(), params)


def execute(statement: str, params: dict | None = None) -> None:
    """Execute a single non-SELECT statement inside a transaction."""
    with connection() as conn:
        conn.execute(text(statement), params or {})


def execute_script(path: str | Path) -> None:
    """Execute a multi-statement ``.sql`` script (e.g. the schema DDL)."""
    sql_path = Path(path)
    if not sql_path.is_absolute():
        sql_path = PROJECT_ROOT / sql_path
    logger.info("executing sql script: %s", sql_path.name)
    with get_engine().begin() as conn:
        conn.exec_driver_sql(sql_path.read_text())
