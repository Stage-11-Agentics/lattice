"""The suite's hermetic-environment fixture holds under pytest-xdist (LAT-320).

``tests/conftest.py::_hermetic_env`` gives every test its own system temp dir
and strips ambient ``LATTICE_*``/``C11_*``/``CMUX_*`` state. These tests prove
both properties, so a regression shows up as a failure rather than a flake.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from lattice.core.review import cleanup_temp_files

REPO_ROOT = Path(__file__).resolve().parent.parent

# A test that runs the fake agent without setting LATTICE_FAKE_BEHAVIOR itself,
# so it is only correct when the ambient environment is stripped.
REPRESENTATIVE_TEST = (
    "tests/test_core/test_agent_spawn_reuse.py::test_spawn_one_callable_outside_review"
)


def test_system_temp_is_private_to_this_test() -> None:
    """``tempfile`` and child processes see one fresh, empty temp dir."""
    tmp = tempfile.gettempdir()
    assert os.environ["TMPDIR"] == os.environ["TMP"] == os.environ["TEMP"] == tmp
    assert list(Path(tmp).iterdir()) == []
    child = subprocess.run(
        [sys.executable, "-c", "import tempfile; print(tempfile.gettempdir())"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert child.stdout.strip() == tmp


def test_system_temp_is_disjoint_across_workers(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """The temp dir lives under this worker's own basetemp, never a shared root."""
    tmp = Path(tempfile.gettempdir()).resolve()
    basetemp = tmp_path_factory.getbasetemp().resolve()
    assert tmp.is_relative_to(basetemp)
    worker = os.environ.get("PYTEST_XDIST_WORKER")
    if worker:
        # xdist gives each worker its own basetemp (``.../popen-gw0``).
        assert basetemp.name == f"popen-{worker}"


def test_cleanup_cannot_see_another_tests_temp_files(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """``cleanup_temp_files()`` removes only this test's review temp files.

    The other namespace stands in for a sibling test (or worker) that sits
    between creating and copying its ``lattice-review-*`` payload.
    """
    other = tmp_path_factory.mktemp("other-systmp")
    theirs = other / "lattice-review-theirs.md"
    theirs.write_text("in flight\n")
    mine = tempfile.NamedTemporaryFile(prefix="lattice-review-", suffix=".md", delete=False)
    mine.close()

    assert cleanup_temp_files() == 1
    assert not Path(mine.name).exists()
    assert theirs.read_text() == "in flight\n"


def test_ambient_env_is_stripped_by_prefix() -> None:
    leaked = [n for n in os.environ if n.startswith(("LATTICE_", "C11_", "CMUX_"))]
    assert sorted(leaked) == ["LATTICE_FFMPEG", "LATTICE_NO_UPDATE_CHECK", "LATTICE_SIPS"]
    assert os.environ["LATTICE_FFMPEG"] == os.environ["LATTICE_SIPS"] == "off"


def test_polluted_ambient_env_does_not_change_results() -> None:
    """A representative test passes with hostile ambient state in the shell."""
    env = {
        **os.environ,
        "LATTICE_FAKE_BEHAVIOR": "fail",
        "LATTICE_ROOT": "/nonexistent",
        "LATTICE_DIR": "/nonexistent/.lattice",
        "C11_SURFACE_ID": "surface:polluted",
        "CMUX_SURFACE_ID": "surface:polluted",
    }
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-n", "0", "-p", "no:cacheprovider"]
        + [REPRESENTATIVE_TEST],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout
