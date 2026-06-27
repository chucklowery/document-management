"""Smoke tests for the task 1.1 project skeleton.

These tests verify the tooling wired up by task 1.1 — they do **not** exercise
any walking-slice service. Each domain test added by later tasks should live
in its own file under ``tests/unit``, ``tests/property``, or
``tests/end_to_end`` as appropriate.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import hypothesis
import pytest
from sqlalchemy import text


pytestmark = pytest.mark.unit


def test_walking_slice_package_importable() -> None:
    """The ``walking_slice`` package resolves from the ``src/`` layout."""
    import walking_slice  # noqa: F401


def test_hypothesis_profile_loaded() -> None:
    """A registered Hypothesis profile is active for this session."""
    active = hypothesis.settings()
    # Every registered profile sets a max_examples >= 1; the fallback
    # Hypothesis default (100) would also satisfy this, so we just assert the
    # value is positive and the deadline matches one of our profiles or is None.
    assert active.max_examples >= 1
    assert active.deadline is None or active.deadline.total_seconds() >= 2.0 - 1e-6  # noqa: PLR2004


def test_sqlite_path_is_per_test(sqlite_path: Path) -> None:
    """``sqlite_path`` is a fresh, test-scoped path under pytest's tmp_path."""
    assert sqlite_path.suffix == ".sqlite"
    assert not sqlite_path.exists(), "fixture must hand out a clean path"


def test_engine_applies_required_pragmas(engine) -> None:
    """The engine fixture enables WAL journal mode and foreign-key enforcement."""
    with engine.connect() as conn:
        journal_mode = conn.execute(text("PRAGMA journal_mode")).scalar_one()
        foreign_keys = conn.execute(text("PRAGMA foreign_keys")).scalar_one()
    assert str(journal_mode).lower() == "wal"
    assert int(foreign_keys) == 1


def test_engine_creates_isolated_database(sqlite_path: Path, engine) -> None:
    """Tables created by one test must not leak — the file is unique per test."""
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE smoke (id INTEGER PRIMARY KEY)"))
        conn.execute(text("INSERT INTO smoke (id) VALUES (1)"))
    # Re-open the file directly to confirm the engine actually wrote to
    # ``sqlite_path`` and that the path is real.
    assert sqlite_path.exists()
    with sqlite3.connect(sqlite_path) as raw:
        rows = raw.execute("SELECT id FROM smoke").fetchall()
    assert rows == [(1,)]
