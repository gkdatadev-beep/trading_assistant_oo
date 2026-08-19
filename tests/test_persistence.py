"""Tests for StatePersistence — JSON save/load with day-change detection."""
from __future__ import annotations

import tempfile
from datetime import datetime
from pathlib import Path

from app.core.persistence import StatePersistence


class TestStatePersistence:
    def test_load_nonexistent_file(self):
        p = StatePersistence(data_dir=tempfile.mkdtemp())
        assert p.load() is None

    def test_save_and_load(self):
        d = tempfile.mkdtemp()
        p = StatePersistence(data_dir=d)
        data = {"day_key": "2026-07-31", "daily_profit": 150.0, "flags": {}}
        path = p.save(data)
        assert path.exists()
        loaded = p.load()
        assert loaded is not None
        assert loaded["day_key"] == "2026-07-31"
        assert loaded["daily_profit"] == 150.0

    def test_load_corrupt_json(self):
        d = tempfile.mkdtemp()
        p = StatePersistence(data_dir=d)
        p.snapshot_path.write_text("{corrupt", encoding="utf-8")
        assert p.load() is None

    def test_load_non_dict(self):
        d = tempfile.mkdtemp()
        p = StatePersistence(data_dir=d)
        p.snapshot_path.write_text("[1, 2, 3]", encoding="utf-8")
        assert p.load() is None

    def test_is_current_day_true(self):
        p = StatePersistence(data_dir=tempfile.mkdtemp())
        data = {"day_key": "2026-07-31"}
        assert p.is_current_day(data, datetime(2026, 7, 31, 14, 0, 0))

    def test_is_current_day_false(self):
        p = StatePersistence(data_dir=tempfile.mkdtemp())
        data = {"day_key": "2026-07-30"}
        assert not p.is_current_day(data, datetime(2026, 7, 31, 0, 1, 0))

    def test_is_current_day_nested_session(self):
        p = StatePersistence(data_dir=tempfile.mkdtemp())
        data = {"session": {"day_key": "2026-07-31"}}
        assert p.is_current_day(data, datetime(2026, 7, 31, 8, 0, 0))

    def test_is_current_day_no_key(self):
        p = StatePersistence(data_dir=tempfile.mkdtemp())
        assert not p.is_current_day({}, datetime(2026, 7, 31, 0, 0, 0))

    def test_current_day_key(self):
        p = StatePersistence(data_dir=tempfile.mkdtemp())
        assert p.current_day_key(datetime(2026, 7, 31, 14, 30)) == "2026-07-31"

    def test_saved_at_added(self):
        d = tempfile.mkdtemp()
        p = StatePersistence(data_dir=d)
        p.save({"day_key": "2026-07-31"})
        loaded = p.load()
        assert "saved_at" in loaded

    def test_save_creates_dir(self):
        import os
        base = tempfile.mkdtemp()
        sub = os.path.join(base, "nested", "data")
        p = StatePersistence(data_dir=sub)
        p.save({"day_key": "2026-07-31"})
        assert Path(sub).exists()
