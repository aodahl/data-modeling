from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from app.operational import load_operational


@pytest.fixture(scope="session")
def source_path() -> Path:
    return Path(__file__).parents[1] / "data" / "fhir"


@pytest.fixture
def op(source_path):
    conn=sqlite3.connect(":memory:")
    conn.row_factory=sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    load_operational(conn,source_path)
    yield conn
    conn.close()


@pytest.fixture
def ana():
    conn=sqlite3.connect(":memory:")
    conn.row_factory=sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    yield conn
    conn.close()

