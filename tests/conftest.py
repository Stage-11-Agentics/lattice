"""Shared test fixtures."""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path

import pytest
from click.testing import CliRunner


# Ambient variables the code under test reads. A developer running the suite
# inside a c11 or cmux pane, a Lattice hook, or a review agent has some of
# these set; left alone they change backend selection, board discovery, and
# agent behaviour (``LATTICE_FAKE_BEHAVIOR`` flips the fake agent). Every test
# starts with the whole families removed by prefix, so a variable added later
# is covered too. Tests that need one set it.
_AMBIENT_ENV_PREFIXES = ("LATTICE_", "C11_", "CMUX_")
_AMBIENT_ENV = ("CI",)


@pytest.fixture(scope="session")
def _worker_home(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A throwaway home directory, one per xdist worker (or per serial run)."""
    home = tmp_path_factory.mktemp("home")
    for sub in (".config", ".cache", ".local/share", ".local/state"):
        (home / sub).mkdir(parents=True)
    return home


@pytest.fixture(scope="session")
def _worker_tmp(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The parent of each test's ``tmp_path``, one per xdist worker."""
    return tmp_path_factory.mktemp("t")


@pytest.fixture()
def tmp_path(request: pytest.FixtureRequest, _worker_tmp: Path) -> Path:
    """pytest's ``tmp_path``, made with ``mkdtemp``: pytest's own scans every
    numbered sibling under the session's base dir for the next number, a cost that
    grew with each test the worker had run (G-9). Still under the base dir, so
    pytest's retention of the last few sessions applies; named after the test."""
    name = re.sub(r"\W", "_", request.node.name)[:30]
    return Path(tempfile.mkdtemp(prefix=f"{name}-", dir=_worker_tmp))


@pytest.fixture(scope="session")
def _worker_systmp(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """The parent of each test's fresh system temp dir, one per xdist worker."""
    return tmp_path_factory.mktemp("systmp")


@pytest.fixture(autouse=True)
def _hermetic_env(
    monkeypatch: pytest.MonkeyPatch,
    _worker_systmp: Path,
    _worker_home: Path,
) -> None:
    """Keep every test off the developer's real home, temp dir and environment.

    The suite runs in parallel (pytest-xdist), so anything a test reads from or
    writes to ``~``, ``~/.config``, ``~/.cache``, ``~/.gitconfig`` or the system
    temp dir is shared between workers and with the developer's own machine.

    * ``HOME`` and the XDG base directories point at a per-worker temp dir.
    * The system temp dir (``TMPDIR``/``TMP``/``TEMP`` and the value
      ``tempfile`` caches) is a fresh, empty dir per test, so
      ``cleanup_temp_files()`` in one test cannot delete another test's
      ``lattice-review-*`` file and leak checks see only their own files.
    * Git ignores global and system config; the PyPI update check is off.
    * Every ``LATTICE_*``, ``C11_*`` and ``CMUX_*`` variable is removed before
      the intentional values are set.
    """
    for name in list(os.environ):
        if name.startswith(_AMBIENT_ENV_PREFIXES) or name in _AMBIENT_ENV:
            monkeypatch.delenv(name)
    # mkdtemp, not tmp_path_factory.mktemp: that scans every numbered sibling for
    # the next number, a cost that grew with each test the worker had run.
    sys_tmp = tempfile.mkdtemp(dir=_worker_systmp)
    for name in ("TMPDIR", "TMP", "TEMP"):
        monkeypatch.setenv(name, sys_tmp)
    monkeypatch.setattr(tempfile, "tempdir", sys_tmp)
    monkeypatch.setenv("HOME", str(_worker_home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(_worker_home / ".config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(_worker_home / ".cache"))
    monkeypatch.setenv("XDG_DATA_HOME", str(_worker_home / ".local/share"))
    monkeypatch.setenv("XDG_STATE_HOME", str(_worker_home / ".local/state"))
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("LATTICE_NO_UPDATE_CHECK", "1")


#: Markers whose tests keep the real ``os.fsync`` (see :func:`_no_fsync`).
_REAL_FSYNC_MARKERS = ("real_fsync", "torture", "perf")


def _checked_no_fsync(fd: int) -> None:
    os.fstat(fd)  # a bad descriptor still raises, as fsync would


@pytest.fixture(autouse=True)
def _no_fsync(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip the ``os.fsync`` syscall in the default suite (G-9's speed budget).

    Durability is not observable in a test process: no test cuts power. What the
    tests prove about durable writes goes through seams above the syscall
    (``transactions._fault``, ``fs._fsync_directory``, the fault injectors), which
    this leaves in place, and a test that patches ``os.fsync`` itself still wins.
    On Linux the syscall was half of the default suite's serial time. Torture and
    perf tests, and any test marked ``real_fsync``, keep the real one. The server
    under test shares the process, so its writes skip it too."""
    if any(request.node.get_closest_marker(name) for name in _REAL_FSYNC_MARKERS):
        return
    monkeypatch.setattr(os, "fsync", _checked_no_fsync)


@pytest.fixture()
def lattice_root(tmp_path: Path) -> Path:
    """Return a temporary directory suitable for initializing .lattice/ in."""
    return tmp_path


@pytest.fixture()
def initialized_root(lattice_root: Path) -> Path:
    """Return a temporary directory with .lattice/ already initialized.

    Auto-fire of code-review/plan-review on status transitions (LAT-211) is
    *disabled* in the test fixture so that ``lattice status <id> review``
    in tests does not actually fork a ``lattice code-review`` subprocess.
    Tests that exercise the auto-fire path enable it explicitly.
    """
    from lattice.core.config import default_config, serialize_config
    from lattice.storage.fs import LATTICE_DIR, atomic_write, ensure_lattice_dirs

    ensure_lattice_dirs(lattice_root)
    lattice_dir = lattice_root / LATTICE_DIR
    cfg = default_config()
    cfg["auto_code_review_on_transition"] = False
    cfg["auto_plan_review_on_transition"] = False
    atomic_write(lattice_dir / "config.json", serialize_config(cfg))
    (lattice_dir / "events" / "_lifecycle.jsonl").touch()
    return lattice_root


@pytest.fixture()
def cli_runner() -> CliRunner:
    """Return a Click CliRunner for invoking CLI commands."""
    return CliRunner()


@pytest.fixture()
def cli_env(initialized_root: Path) -> dict[str, str]:
    """Return env dict with LATTICE_ROOT pointing to initialized_root."""
    return {"LATTICE_ROOT": str(initialized_root)}


@pytest.fixture()
def invoke(cli_runner: CliRunner, cli_env: dict[str, str]):
    """Return a helper that invokes CLI commands with the right environment.

    Usage::

        result = invoke("create", "My task", "--actor", "human:test")
    """
    from lattice.cli.main import cli

    def _invoke(*args: str, **kwargs):
        return cli_runner.invoke(cli, list(args), env=cli_env, **kwargs)

    return _invoke


@pytest.fixture()
def invoke_json(invoke):
    """Like invoke, but appends --json and parses the response.

    Returns (parsed_dict, exit_code) tuple.
    """

    def _invoke_json(*args: str) -> tuple[dict, int]:
        result = invoke(*args, "--json")
        parsed = json.loads(result.output)
        return parsed, result.exit_code

    return _invoke_json


@pytest.fixture()
def fill_plan(cli_env: dict[str, str]):
    """Write non-scaffold content into a task's plan file.

    Usage::

        fill_plan(task_id, "My task title")
    """

    def _fill(task_id: str, title: str = "Task") -> None:
        plan_path = Path(cli_env["LATTICE_ROOT"]) / ".lattice" / "plans" / f"{task_id}.md"
        plan_path.write_text(f"# {title}\n\n## Approach\n\n- Implement the feature.\n")

    return _fill


@pytest.fixture()
def create_task(cli_runner: CliRunner, cli_env: dict[str, str]):
    """Factory fixture: create a task and return its snapshot dict.

    Usage::

        task = create_task("My task", "--priority", "high")
    """
    from lattice.cli.main import cli

    def _create(title: str = "Test task", *extra_args: str, actor: str = "human:test"):
        args = ["create", title, "--actor", actor, "--json", *extra_args]
        result = cli_runner.invoke(cli, args, env=cli_env)
        assert result.exit_code == 0, f"create failed: {result.output}"
        return json.loads(result.output)["data"]

    return _create


# ---------------------------------------------------------------------------
# Production-like fixtures (with completion policies)
# ---------------------------------------------------------------------------

STANDARD_COMPLETION_POLICIES = {
    "done": {"require_roles": ["review"]},
}


def _add_policies_to_config(lattice_root: Path, policies: dict) -> None:
    """Inject completion policies into an initialized root's config."""
    from lattice.storage.fs import LATTICE_DIR

    config_path = lattice_root / LATTICE_DIR / "config.json"
    config = json.loads(config_path.read_text())
    config["workflow"]["completion_policies"] = policies
    config_path.write_text(json.dumps(config, sort_keys=True, indent=2) + "\n")


@pytest.fixture()
def initialized_root_with_policies(initialized_root: Path) -> Path:
    """Return an initialized root with standard completion policies.

    Mirrors production config: ``done`` requires a ``review`` role.
    Use this for tests that exercise completion gates, role validation,
    or policy-dependent behavior.
    """
    _add_policies_to_config(initialized_root, STANDARD_COMPLETION_POLICIES)
    return initialized_root


@pytest.fixture()
def cli_env_with_policies(initialized_root_with_policies: Path) -> dict[str, str]:
    """Return env dict pointing to root with standard policies."""
    return {"LATTICE_ROOT": str(initialized_root_with_policies)}


@pytest.fixture()
def invoke_with_policies(cli_runner: CliRunner, cli_env_with_policies: dict[str, str]):
    """Like invoke, but backed by a root with standard completion policies."""
    from lattice.cli.main import cli

    def _invoke(*args: str, **kwargs):
        return cli_runner.invoke(cli, list(args), env=cli_env_with_policies, **kwargs)

    return _invoke


@pytest.fixture()
def fill_plan_with_policies(cli_env_with_policies: dict[str, str]):
    """Like fill_plan, but backed by a root with standard completion policies."""

    def _fill(task_id: str, title: str = "Task") -> None:
        plan_path = (
            Path(cli_env_with_policies["LATTICE_ROOT"]) / ".lattice" / "plans" / f"{task_id}.md"
        )
        plan_path.write_text(f"# {title}\n\n## Approach\n\n- Implement the feature.\n")

    return _fill


# ---------------------------------------------------------------------------
# Real-git topology (LAT-271)
# ---------------------------------------------------------------------------


def git(cwd: Path, *args: str) -> str:
    """Run a git command in ``cwd`` and return stdout, raising on failure."""
    import os
    import subprocess

    env = {
        **os.environ,
        # Isolate from the developer's git config, hooks, and templates: the
        # fixture must be identical everywhere and must not read ~/.gitconfig.
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_AUTHOR_NAME": "Tester",
        "GIT_AUTHOR_EMAIL": "t@t.com",
        "GIT_COMMITTER_NAME": "Tester",
        "GIT_COMMITTER_EMAIL": "t@t.com",
    }
    return subprocess.run(
        ["git", *args], cwd=str(cwd), capture_output=True, text=True, check=True, env=env
    ).stdout


@pytest.fixture()
def caller_git_worktree(tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch):
    """Run the test from an empty git worktree of its own.

    ``code-review``, ``plan-review`` and the auto-fire path resolve the
    caller's git worktree from the current directory. Without this the tests
    silently use whatever checkout pytest was started from, and fail when that
    is not a git worktree (an sdist, a copied tree, ``cd /tmp``).
    """
    worktree = tmp_path_factory.mktemp("caller-wt")
    git(worktree, "init", "-q", "-b", "main")
    monkeypatch.chdir(worktree)
    return worktree


@pytest.fixture()
def worktree_repo(tmp_path: Path):
    """The worktree-per-ticket topology that makes diff resolution hard.

    Builds, with real git and no network:

    * ``origin.git`` — a bare remote carrying several sibling-ticket commits.
    * ``mainco/`` — the checkout holding ``.lattice/``. Its *local* ``main`` is
      several commits behind, its ``origin/main`` remote ref is current, and its
      working tree is dirty. This is the ordinary state of a board checkout
      nobody pulls.
    * ``wt-222/`` — a sibling worktree on a feature branch cut from
      ``origin/main`` carrying exactly one real commit.

    A diff against the *local* ``main`` therefore drags in every sibling
    commit; a diff against ``origin/main`` sees only ``feature.py``.
    """
    from types import SimpleNamespace

    origin = tmp_path / "origin.git"
    origin.mkdir()
    git(origin, "init", "--bare", "-b", "main")

    main = tmp_path / "mainco"
    main.mkdir()
    git(main, "init", "-b", "main")
    (main / "README.md").write_text("base\n")
    git(main, "add", "-A")
    git(main, "commit", "-m", "init")
    git(main, "remote", "add", "origin", str(origin))
    git(main, "push", "-u", "origin", "main")

    # Seven sibling tickets land on origin/main while mainco is not looking.
    sib = tmp_path / "sib"
    git(tmp_path, "clone", str(origin), str(sib))
    for i in range(1, 5):
        (sib / f"sibling_{i}.txt").write_text(f"sibling ticket {i}\n")
        git(sib, "add", "-A")
        git(sib, "commit", "-m", f"KWB-{200 + i}: sibling work")
    git(sib, "push", "origin", "main")

    # mainco learns about the remote (as it would when a worktree is cut) but
    # its local ``main`` is never fast-forwarded.
    git(main, "fetch", "origin")

    branch = "feat/KWB-222-thing"
    worktree = tmp_path / "wt-222"
    git(main, "worktree", "add", "-b", branch, str(worktree), "origin/main")
    (worktree / "feature.py").write_text("def feature():\n    return 'the ticket change'\n")
    git(worktree, "add", "-A")
    git(worktree, "commit", "-m", "KWB-222: the real ticket change")

    # A dirty working tree in the board checkout, like any live board.
    (main / "README.md").write_text("base\nuncommitted local edit\n")

    lattice_dir = main / ".lattice"
    lattice_dir.mkdir(exist_ok=True)

    return SimpleNamespace(
        root=tmp_path,
        origin=origin,
        main=main,
        lattice_dir=lattice_dir,
        worktree=worktree,
        branch=branch,
        sib=sib,
    )


# ---------------------------------------------------------------------------
# Caller-shell isolation (LAT-296)
# ---------------------------------------------------------------------------


def purge_caller_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Purge every ``LATTICE_*`` / ``C11_*`` / ``CMUX_*`` variable, then set intentional values.

    A ``LATTICE_ROOT`` exported in the shell that runs pytest points any test
    that relies on cwd discovery at a real board (it once let a test claim a
    task on the live board), and ``C11_*`` makes the c11 bridge drive the
    caller's real c11 session. Prefix matching covers variables nobody listed,
    with no exception for caller-supplied values.
    """
    import os

    for key in list(os.environ):
        if key.startswith(("LATTICE_", "C11_", "CMUX_")):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("LATTICE_NO_UPDATE_CHECK", "1")


@pytest.fixture(autouse=True)
def _strip_caller_shell_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test starts without the caller's shell; tests that need a value set it."""
    purge_caller_env(monkeypatch)
