import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app import seed  # noqa: E402
from app.db import connect  # noqa: E402

NOW = "2026-11-03T09:00:00Z"


@pytest.fixture(scope="session")
def seeded_path(tmp_path_factory):
    path = str(tmp_path_factory.mktemp("seed") / "seed.db")
    conn = connect(path)
    seed.build(conn, NOW)
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    return path


@pytest.fixture
def db_file(seeded_path, tmp_path):
    """A private copy of the seeded world for each test."""
    path = str(tmp_path / "t.db")
    src = sqlite3.connect(seeded_path)
    dst = sqlite3.connect(path)
    src.backup(dst)
    src.close()
    dst.close()
    return path


@pytest.fixture
def conn(db_file):
    c = connect(db_file)
    yield c
    c.close()


@pytest.fixture
def now():
    return NOW
