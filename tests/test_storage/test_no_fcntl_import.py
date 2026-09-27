"""G-6: local Lattice imports without ``fcntl`` (Windows has none). ``fcntl``
is imported only inside the functions that take a hosted lock."""

from __future__ import annotations

import subprocess
import sys

_PROBE = """
import sys
import warnings

sys.modules["fcntl"] = None  # any import of fcntl now raises ImportError
warnings.simplefilter("ignore")  # filelock warns that only its soft lock is available

import lattice.cli.main
import lattice.boards
import lattice.storage.fs
import lattice.ops
import lattice.remote.cache

from lattice.core.errors import OpError
from lattice.storage.ownership import try_owner_flock

try:
    try_owner_flock(".")
except OpError as exc:
    print(exc.code)
try:
    lattice.remote.cache.catch_up(".")
except OpError as exc:
    print(exc.code)
"""


def test_imports_with_fcntl_blocked() -> None:
    result = subprocess.run(
        [sys.executable, "-c", _PROBE], capture_output=True, text=True, timeout=60, check=False
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.split() == ["HOSTED_UNSUPPORTED_PLATFORM"] * 2


def test_fcntl_is_not_a_module_level_import() -> None:
    import lattice.remote.cache as cache
    import lattice.storage.fs as fs
    import lattice.storage.ownership as ownership

    assert not hasattr(fs, "fcntl") and not hasattr(ownership, "fcntl")
    assert not hasattr(cache, "fcntl")
