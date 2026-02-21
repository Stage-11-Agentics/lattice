"""Startup version check against PyPI.

Prints a one-liner to stderr when a newer version of lattice-tracker is
available. Designed to be silent and safe: any error at all is swallowed,
non-TTY environments are skipped, and results are cached for 24 hours.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

_CACHE_DIR = Path.home() / ".cache" / "lattice"
_CACHE_FILE = _CACHE_DIR / "version_check.json"
_CACHE_TTL = 86400  # 24 hours
_PYPI_URL = "https://pypi.org/pypi/lattice-tracker/json"
_TIMEOUT = 3  # seconds


def _parse_version(v: str) -> tuple[int, ...]:
    """Parse a PEP 440 version string into a comparable tuple."""
    return tuple(int(x) for x in v.strip().split("."))


def _read_cache() -> str | None:
    """Return cached latest version if fresh, else None."""
    try:
        data = json.loads(_CACHE_FILE.read_text())
        if time.time() - data["ts"] < _CACHE_TTL:
            return data["version"]
    except Exception:
        pass
    return None


def _write_cache(version: str) -> None:
    """Persist latest version to cache file."""
    try:
        _CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _CACHE_FILE.write_text(
            json.dumps({"version": version, "ts": time.time()}) + "\n"
        )
    except Exception:
        pass


def _fetch_latest() -> str | None:
    """Fetch the latest version from PyPI. Returns None on any failure."""
    try:
        from urllib.request import Request, urlopen

        req = Request(_PYPI_URL, headers={"Accept": "application/json"})
        with urlopen(req, timeout=_TIMEOUT) as resp:
            data = json.loads(resp.read())
            return data["info"]["version"]
    except Exception:
        return None


def maybe_print_update_notice() -> None:
    """Print an upgrade notice to stderr if a newer version exists.

    Skip conditions (no check, no output):
    - LATTICE_NO_UPDATE_CHECK=1 env var
    - stderr is not a TTY
    - Any error at all
    """
    try:
        if os.environ.get("LATTICE_NO_UPDATE_CHECK") == "1":
            return

        if not hasattr(sys.stderr, "isatty") or not sys.stderr.isatty():
            return

        from importlib.metadata import version as pkg_version

        current = pkg_version("lattice-tracker")

        latest = _read_cache()
        if latest is None:
            latest = _fetch_latest()
            if latest is None:
                return
            _write_cache(latest)

        if _parse_version(latest) > _parse_version(current):
            print(
                f"\nA new version of Lattice is available: {current} \u2192 {latest}"
                f"\nRun: uv tool upgrade lattice-tracker",
                file=sys.stderr,
            )
    except Exception:
        pass
