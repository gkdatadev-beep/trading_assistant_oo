"""Tests for RuntimeConfig — key-value config with file persistence."""
from __future__ import annotations

import tempfile
from pathlib import Path

from app.config import RuntimeConfig, DEFAULTS, DISCIPLINE


class TestRuntimeConfigDefaults:
    def test_default_values_present(self):
        cfg = RuntimeConfig(data_dir=tempfile.mkdtemp())
        assert cfg.get_str("SYMBOL") == "XAUUSD"
        assert cfg.get_float("LOCK_ENTRY") == 0.5
        assert cfg.get_int("LIMITE_OPERACOES") == 40

    def test_get_default_when_missing(self):
        cfg = RuntimeConfig(data_dir=tempfile.mkdtemp())
        assert cfg.get("NONEXISTENT", "fallback") == "fallback"

    def test_get_float_coercion(self):
        cfg = RuntimeConfig(data_dir=tempfile.mkdtemp())
        cfg.update({"LOCK_ENTRY": "1.25"})
        assert cfg.get_float("LOCK_ENTRY") == 1.25

    def test_get_float_invalid_returns_default(self):
        cfg = RuntimeConfig(data_dir=tempfile.mkdtemp())
        cfg.update({"LOCK_ENTRY": "abc"})
        assert cfg.get_float("LOCK_ENTRY", 99.0) == 99.0

    def test_get_int_coercion(self):
        cfg = RuntimeConfig(data_dir=tempfile.mkdtemp())
        cfg.update({"LIMITE_OPERACOES": "80"})
        assert cfg.get_int("LIMITE_OPERACOES") == 80

    def test_get_int_invalid_returns_default(self):
        cfg = RuntimeConfig(data_dir=tempfile.mkdtemp())
        cfg.update({"LIMITE_OPERACOES": "abc"})
        assert cfg.get_int("LIMITE_OPERACOES", 10) == 10

    def test_get_str(self):
        cfg = RuntimeConfig(data_dir=tempfile.mkdtemp())
        cfg.update({"SYMBOL": "EURUSD"})
        assert cfg.get_str("SYMBOL") == "EURUSD"

    def test_update_multiple(self):
        cfg = RuntimeConfig(data_dir=tempfile.mkdtemp())
        cfg.update({"SYMBOL": "XAGUSD", "LOT_SIZE": "0.5"})
        assert cfg.get_str("SYMBOL") == "XAGUSD"
        assert cfg.get_str("LOT_SIZE") == "0.5"

    def test_to_dict_contains_all_defaults(self):
        cfg = RuntimeConfig(data_dir=tempfile.mkdtemp())
        d = cfg.to_dict()
        for key in DEFAULTS:
            assert key in d


class TestRuntimeConfigPersistence:
    def test_save_and_load(self):
        d = tempfile.mkdtemp()
        cfg1 = RuntimeConfig(data_dir=d)
        cfg1.update({"SYMBOL": "TEST_SYMBOL", "LOT_SIZE": "0.99"})
        # New instance in same dir should pick up changes
        cfg2 = RuntimeConfig(data_dir=d)
        assert cfg2.get_str("SYMBOL") == "TEST_SYMBOL"
        assert cfg2.get_str("LOT_SIZE") == "0.99"

    def test_handles_corrupt_file(self):
        d = tempfile.mkdtemp()
        # Write invalid JSON
        (Path(d) / "runtime_config.json").write_text("{invalid json!!!", encoding="utf-8")
        cfg = RuntimeConfig(data_dir=d)
        # Should not crash — fall back to defaults
        assert cfg.get_str("SYMBOL") == DEFAULTS["SYMBOL"]

    def test_handles_missing_file(self):
        d = tempfile.mkdtemp()
        cfg = RuntimeConfig(data_dir=d)
        assert cfg.get_str("SYMBOL") == "XAUUSD"


class TestRuntimeConfigLock:
    def test_not_locked_initially(self):
        cfg = RuntimeConfig(data_dir=tempfile.mkdtemp())
        assert not cfg.is_locked_today("2026-07-31")

    def test_lock_for_today(self):
        cfg = RuntimeConfig(data_dir=tempfile.mkdtemp())
        cfg.lock_for_today("2026-07-31")
        assert cfg.is_locked_today("2026-07-31")

    def test_lock_different_day(self):
        cfg = RuntimeConfig(data_dir=tempfile.mkdtemp())
        cfg.lock_for_today("2026-07-30")
        assert not cfg.is_locked_today("2026-07-31")


class TestDiscipline:
    def test_discipline_values(self):
        assert DISCIPLINE["LIMITE_LUCRO_DIARIO"] == 750.0
        assert DISCIPLINE["LIMITE_PERDA_DIARIO"] == -600.0
        assert DISCIPLINE["LIMITE_OPERACOES"] == 40
        assert DISCIPLINE["MAX_PROFIT_RETENTION_PCT"] == 50.0

    def test_defaults_match_discipline(self):
        assert DEFAULTS["LIMITE_LUCRO_DIARIO"] == DISCIPLINE["LIMITE_LUCRO_DIARIO"]
        assert DEFAULTS["LIMITE_OPERACOES"] == DISCIPLINE["LIMITE_OPERACOES"]
