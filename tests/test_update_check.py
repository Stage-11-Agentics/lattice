"""Tests for lattice.update_check."""

from __future__ import annotations

import json
import time
from io import StringIO
from unittest.mock import patch

from lattice.update_check import (
    _CACHE_TTL,
    _parse_version,
    _read_cache,
    _write_cache,
    maybe_print_update_notice,
)


class TestParseVersion:
    def test_simple(self):
        assert _parse_version("1.2.3") == (1, 2, 3)

    def test_two_part(self):
        assert _parse_version("0.2") == (0, 2)

    def test_comparison(self):
        assert _parse_version("0.3.0") > _parse_version("0.2.0")
        assert _parse_version("1.0.0") > _parse_version("0.99.99")
        assert _parse_version("0.2.0") == _parse_version("0.2.0")


class TestCache:
    def test_write_and_read(self, tmp_path, monkeypatch):
        cache_file = tmp_path / "version_check.json"
        monkeypatch.setattr("lattice.update_check._CACHE_FILE", cache_file)
        monkeypatch.setattr("lattice.update_check._CACHE_DIR", tmp_path)

        _write_cache("1.2.3")
        assert _read_cache() == "1.2.3"

    def test_stale_cache_returns_none(self, tmp_path, monkeypatch):
        cache_file = tmp_path / "version_check.json"
        monkeypatch.setattr("lattice.update_check._CACHE_FILE", cache_file)
        monkeypatch.setattr("lattice.update_check._CACHE_DIR", tmp_path)

        # Write a cache entry that's older than TTL
        cache_file.write_text(
            json.dumps({"version": "1.2.3", "ts": time.time() - _CACHE_TTL - 1})
        )
        assert _read_cache() is None

    def test_missing_cache_returns_none(self, tmp_path, monkeypatch):
        cache_file = tmp_path / "nonexistent.json"
        monkeypatch.setattr("lattice.update_check._CACHE_FILE", cache_file)
        assert _read_cache() is None

    def test_corrupt_cache_returns_none(self, tmp_path, monkeypatch):
        cache_file = tmp_path / "version_check.json"
        monkeypatch.setattr("lattice.update_check._CACHE_FILE", cache_file)
        cache_file.write_text("not json")
        assert _read_cache() is None


class TestMaybePrintUpdateNotice:
    """Tests for the main entry point."""

    def _tty_stderr(self):
        """Return a StringIO that claims to be a TTY."""
        s = StringIO()
        s.isatty = lambda: True  # type: ignore[attr-defined]
        return s

    def test_skip_when_env_var_set(self, monkeypatch):
        monkeypatch.setenv("LATTICE_NO_UPDATE_CHECK", "1")
        fake_stderr = self._tty_stderr()
        with patch("sys.stderr", fake_stderr):
            maybe_print_update_notice()
        assert fake_stderr.getvalue() == ""

    def test_skip_when_not_tty(self, monkeypatch):
        monkeypatch.delenv("LATTICE_NO_UPDATE_CHECK", raising=False)
        # Plain StringIO.isatty() returns False
        fake_stderr = StringIO()
        with patch("sys.stderr", fake_stderr):
            maybe_print_update_notice()
        assert fake_stderr.getvalue() == ""

    def test_notice_when_behind(self, tmp_path, monkeypatch):
        monkeypatch.delenv("LATTICE_NO_UPDATE_CHECK", raising=False)
        monkeypatch.setattr("lattice.update_check._CACHE_FILE", tmp_path / "vc.json")
        monkeypatch.setattr("lattice.update_check._CACHE_DIR", tmp_path)

        fake_stderr = self._tty_stderr()
        with (
            patch("sys.stderr", fake_stderr),
            patch(
                "lattice.update_check.pkg_version",
                return_value="0.1.0",
                create=True,
            ),
            patch("lattice.update_check._fetch_latest", return_value="0.3.0"),
            patch(
                "importlib.metadata.version", return_value="0.1.0"
            ),
        ):
            maybe_print_update_notice()

        output = fake_stderr.getvalue()
        assert "0.3.0" in output
        assert "uv tool upgrade lattice-tracker" in output

    def test_no_notice_when_current(self, tmp_path, monkeypatch):
        monkeypatch.delenv("LATTICE_NO_UPDATE_CHECK", raising=False)
        monkeypatch.setattr("lattice.update_check._CACHE_FILE", tmp_path / "vc.json")
        monkeypatch.setattr("lattice.update_check._CACHE_DIR", tmp_path)

        fake_stderr = self._tty_stderr()
        with (
            patch("sys.stderr", fake_stderr),
            patch("lattice.update_check._fetch_latest", return_value="0.2.0"),
            patch("importlib.metadata.version", return_value="0.2.0"),
        ):
            maybe_print_update_notice()

        assert fake_stderr.getvalue() == ""

    def test_cache_hit_avoids_network(self, tmp_path, monkeypatch):
        monkeypatch.delenv("LATTICE_NO_UPDATE_CHECK", raising=False)
        cache_file = tmp_path / "vc.json"
        monkeypatch.setattr("lattice.update_check._CACHE_FILE", cache_file)
        monkeypatch.setattr("lattice.update_check._CACHE_DIR", tmp_path)

        # Pre-populate fresh cache
        cache_file.write_text(
            json.dumps({"version": "0.2.0", "ts": time.time()})
        )

        fake_stderr = self._tty_stderr()
        with (
            patch("sys.stderr", fake_stderr),
            patch("importlib.metadata.version", return_value="0.2.0"),
            patch("lattice.update_check._fetch_latest") as mock_fetch,
        ):
            maybe_print_update_notice()

        mock_fetch.assert_not_called()
        assert fake_stderr.getvalue() == ""

    def test_stale_cache_triggers_fetch(self, tmp_path, monkeypatch):
        monkeypatch.delenv("LATTICE_NO_UPDATE_CHECK", raising=False)
        cache_file = tmp_path / "vc.json"
        monkeypatch.setattr("lattice.update_check._CACHE_FILE", cache_file)
        monkeypatch.setattr("lattice.update_check._CACHE_DIR", tmp_path)

        # Write stale cache
        cache_file.write_text(
            json.dumps({"version": "0.1.0", "ts": time.time() - _CACHE_TTL - 1})
        )

        fake_stderr = self._tty_stderr()
        with (
            patch("sys.stderr", fake_stderr),
            patch("importlib.metadata.version", return_value="0.2.0"),
            patch("lattice.update_check._fetch_latest", return_value="0.2.0") as mock_fetch,
        ):
            maybe_print_update_notice()

        mock_fetch.assert_called_once()

    def test_network_failure_is_silent(self, tmp_path, monkeypatch):
        monkeypatch.delenv("LATTICE_NO_UPDATE_CHECK", raising=False)
        monkeypatch.setattr("lattice.update_check._CACHE_FILE", tmp_path / "vc.json")
        monkeypatch.setattr("lattice.update_check._CACHE_DIR", tmp_path)

        fake_stderr = self._tty_stderr()
        with (
            patch("sys.stderr", fake_stderr),
            patch("importlib.metadata.version", return_value="0.2.0"),
            patch("lattice.update_check._fetch_latest", return_value=None),
        ):
            maybe_print_update_notice()

        assert fake_stderr.getvalue() == ""

    def test_cache_written_after_fetch(self, tmp_path, monkeypatch):
        monkeypatch.delenv("LATTICE_NO_UPDATE_CHECK", raising=False)
        cache_file = tmp_path / "vc.json"
        monkeypatch.setattr("lattice.update_check._CACHE_FILE", cache_file)
        monkeypatch.setattr("lattice.update_check._CACHE_DIR", tmp_path)

        fake_stderr = self._tty_stderr()
        with (
            patch("sys.stderr", fake_stderr),
            patch("importlib.metadata.version", return_value="0.2.0"),
            patch("lattice.update_check._fetch_latest", return_value="0.3.0"),
        ):
            maybe_print_update_notice()

        # Cache should now contain the fetched version
        data = json.loads(cache_file.read_text())
        assert data["version"] == "0.3.0"
