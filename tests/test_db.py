"""Tests for the environment-driven database configuration.

These run without a live database: they only exercise URL construction and the
fail-fast behaviour when configuration is missing.
"""

from __future__ import annotations

import pytest

from src import db

ENV_KEYS = ("DB_HOST", "DB_PORT", "DB_USER", "DB_PASSWORD", "DB_NAME")


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """Strip every DB_* variable so tests never inherit the developer's .env."""
    for key in ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    return monkeypatch


def test_database_url_built_from_environment(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("DB_HOST", "localhost")
    clean_env.setenv("DB_PORT", "5433")
    clean_env.setenv("DB_USER", "cadence_user")
    clean_env.setenv("DB_PASSWORD", "secret")
    clean_env.setenv("DB_NAME", "cadence")

    assert db.database_url() == "postgresql+psycopg2://cadence_user:secret@localhost:5433/cadence"


def test_database_url_defaults_port_when_unset(clean_env: pytest.MonkeyPatch) -> None:
    clean_env.setenv("DB_HOST", "localhost")
    clean_env.setenv("DB_USER", "cadence_user")
    clean_env.setenv("DB_PASSWORD", "secret")
    clean_env.setenv("DB_NAME", "cadence")

    assert db.database_url().endswith("@localhost:5433/cadence")


@pytest.mark.parametrize("missing", ENV_KEYS[:1] + ENV_KEYS[2:])
def test_database_url_fails_fast_on_missing_config(
    clean_env: pytest.MonkeyPatch, missing: str
) -> None:
    """A missing credential should raise at config time, not as a socket error."""
    values = {
        "DB_HOST": "localhost",
        "DB_USER": "cadence_user",
        "DB_PASSWORD": "secret",
        "DB_NAME": "cadence",
    }
    for key, value in values.items():
        if key != missing:
            clean_env.setenv(key, value)

    with pytest.raises(RuntimeError, match=missing):
        db.database_url()
