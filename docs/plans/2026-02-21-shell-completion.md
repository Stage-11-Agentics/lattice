# Shell Autocompletion Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Add bash/zsh/fish tab-completion to the `lattice` CLI, exposing all commands, flags, and enumerated values, with dynamic task ID completion from `.lattice/ids.json`.

**Architecture:** Use Click 8.1's native `shell_complete` system — a single `_LATTICE_COMPLETE=<shell>_source lattice` environment variable activates completion mode. Static callbacks handle fixed-value options; a new `src/lattice/completion/` module provides dynamic callbacks that read `.lattice/` with full graceful degradation. A new `lattice completion` command handles printing and installing the activation script.

**Tech Stack:** Python 3.12+, Click >=8.1 (`click.shell_completion.CompletionItem`), pytest, existing `find_root()` from `src/lattice/storage/fs.py`.

**Design doc:** `docs/plans/2026-02-21-shell-completion-design.md`

---

## Reference: Key Files

| File | Role |
|------|------|
| `src/lattice/cli/main.py` | Root CLI group, command registration via module imports at bottom |
| `src/lattice/cli/tasks.py` | Example command file using `@cli.command()`, `@click.option()` |
| `src/lattice/storage/fs.py` | `find_root(start=None) -> Path \| None` — locates `.lattice/` |
| `src/lattice/storage/short_ids.py` | `ids.json` reader — `{"schema_version":2,"map":{"LAT-1":"<ulid>"},...}` |
| `tests/conftest.py` | `cli_runner`, `invoke`, `invoke_json` fixtures; `LATTICE_ROOT` env injection |

## Reference: Click Patterns Used in This Codebase

```python
# Choice (auto-completes for free — NO shell_complete= needed):
@click.option("--workflow", type=click.Choice(["classic","opinionated"], case_sensitive=False))

# String option with dynamic completion (needs shell_complete= callback):
@click.option("--status", default=None, shell_complete=complete_status)

# Argument with dynamic completion:
@click.argument("task_id", shell_complete=complete_task_id)
```

## Reference: CompletionItem Import

```python
from click.shell_completion import CompletionItem
```

## Reference: How to Run Tests

```bash
uv run pytest tests/ -v                     # all tests
uv run pytest tests/test_completion.py -v   # just completion tests
uv run ruff check src/ tests/               # lint
```

---

## Task 1: Create `src/lattice/completion/__init__.py` with `complete_task_id`

**Files:**
- Create: `src/lattice/completion/__init__.py`
- Create: `tests/test_completion.py`

**Step 1: Write the failing test**

```python
# tests/test_completion.py
import json
import pytest
from click.shell_completion import CompletionItem
from lattice.completion import complete_task_id


def _make_ids_json(tmp_path, entries: dict) -> None:
    lattice_dir = tmp_path / ".lattice"
    lattice_dir.mkdir()
    ids_file = lattice_dir / "ids.json"
    ids_file.write_text(json.dumps({
        "schema_version": 2,
        "next_seqs": {},
        "map": entries,
    }))


def test_complete_task_id_returns_matching_short_ids(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _make_ids_json(tmp_path, {"LAT-1": "ulid1", "LAT-2": "ulid2", "LAT-10": "ulid10"})
    results = complete_task_id(None, None, "LAT-1")
    values = [r.value for r in results]
    assert "LAT-1" in values
    assert "LAT-10" in values
    assert "LAT-2" not in values


def test_complete_task_id_empty_incomplete_returns_all(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _make_ids_json(tmp_path, {"LAT-1": "ulid1", "LAT-2": "ulid2"})
    results = complete_task_id(None, None, "")
    assert len(results) == 2


def test_complete_task_id_no_lattice_returns_empty(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    results = complete_task_id(None, None, "")
    assert results == []


def test_complete_task_id_returns_completion_items(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _make_ids_json(tmp_path, {"LAT-1": "ulid1"})
    results = complete_task_id(None, None, "")
    assert all(isinstance(r, CompletionItem) for r in results)
```

**Step 2: Run test to verify it fails**

```bash
uv run pytest tests/test_completion.py -v
```

Expected: `ModuleNotFoundError: No module named 'lattice.completion'`

**Step 3: Create the module**

```python
# src/lattice/completion/__init__.py
"""Shell completion callbacks for the Lattice CLI."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from click.shell_completion import CompletionItem

if TYPE_CHECKING:
    import click


def _find_lattice_root() -> Path | None:
    """Locate .lattice/ root independently of Click context.

    Completion callbacks run before Click sets up ctx.obj, so we
    must discover the project root ourselves.
    """
    try:
        from lattice.storage.fs import find_root
        return find_root()
    except Exception:
        return None


def complete_task_id(
    ctx: click.Context | None,
    param: click.Parameter | None,
    incomplete: str,
) -> list[CompletionItem]:
    """Complete task short IDs (e.g. LAT-1) from .lattice/ids.json."""
    try:
        root = _find_lattice_root()
        if root is None:
            return []
        ids_file = root / ".lattice" / "ids.json"
        if not ids_file.exists():
            return []
        data = json.loads(ids_file.read_text())
        id_map: dict[str, str] = data.get("map", {})
        return [
            CompletionItem(short_id)
            for short_id in sorted(id_map)
            if short_id.startswith(incomplete)
        ]
    except Exception:
        return []
```

**Step 4: Run test to verify it passes**

```bash
uv run pytest tests/test_completion.py -v
```

Expected: all 4 tests PASS

**Step 5: Lint**

```bash
uv run ruff check src/lattice/completion/ tests/test_completion.py
```

Expected: no errors

**Step 6: Commit**

```bash
git add src/lattice/completion/__init__.py tests/test_completion.py
git commit -m "feat: add completion module with complete_task_id callback"
```

---

## Task 2: Add `complete_status` callback

**Files:**
- Modify: `src/lattice/completion/__init__.py`
- Modify: `tests/test_completion.py`

**Step 1: Write the failing test**

Add to `tests/test_completion.py`:

```python
from lattice.completion import complete_task_id, complete_status


def _make_config_json(tmp_path, statuses: list[str]) -> None:
    lattice_dir = tmp_path / ".lattice"
    lattice_dir.mkdir(exist_ok=True)
    config = {"workflow": {"statuses": statuses}}
    (lattice_dir / "config.json").write_text(json.dumps(config))


def test_complete_status_reads_from_config(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _make_config_json(tmp_path, ["backlog", "in_progress", "done"])
    results = complete_status(None, None, "")
    values = [r.value for r in results]
    assert values == ["backlog", "in_progress", "done"]


def test_complete_status_filters_by_prefix(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _make_config_json(tmp_path, ["backlog", "in_progress", "done"])
    results = complete_status(None, None, "in")
    values = [r.value for r in results]
    assert values == ["in_progress"]


def test_complete_status_falls_back_when_no_config(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    results = complete_status(None, None, "")
    values = [r.value for r in results]
    # Must contain at least the canonical defaults
    assert "backlog" in values
    assert "done" in values
```

**Step 2: Run test to verify it fails**

```bash
uv run pytest tests/test_completion.py::test_complete_status_reads_from_config -v
```

Expected: `ImportError: cannot import name 'complete_status'`

**Step 3: Add `complete_status` to the module**

Append to `src/lattice/completion/__init__.py`:

```python
_DEFAULT_STATUSES = [
    "backlog",
    "ready",
    "in_progress",
    "in_review",
    "done",
    "cancelled",
    "needs_human",
]


def complete_status(
    ctx: click.Context | None,
    param: click.Parameter | None,
    incomplete: str,
) -> list[CompletionItem]:
    """Complete task status values from config or defaults."""
    statuses = _DEFAULT_STATUSES
    try:
        root = _find_lattice_root()
        if root is not None:
            config_file = root / ".lattice" / "config.json"
            if config_file.exists():
                data = json.loads(config_file.read_text())
                statuses = data.get("workflow", {}).get("statuses", statuses)
    except Exception:
        pass
    return [CompletionItem(s) for s in statuses if s.startswith(incomplete)]
```

**Step 4: Run tests to verify they pass**

```bash
uv run pytest tests/test_completion.py -v
```

Expected: all tests PASS

**Step 5: Commit**

```bash
git add src/lattice/completion/__init__.py tests/test_completion.py
git commit -m "feat: add complete_status callback with config fallback"
```

---

## Task 3: Add `complete_actor`, `complete_resource_name`, `complete_session_name`

**Files:**
- Modify: `src/lattice/completion/__init__.py`
- Modify: `tests/test_completion.py`

**Step 1: Write the failing tests**

Append to `tests/test_completion.py`:

```python
from lattice.completion import complete_task_id, complete_status, complete_actor, complete_resource_name, complete_session_name


def _make_snapshot(tmp_path, task_id: str, actor: str) -> None:
    tasks_dir = tmp_path / ".lattice" / "tasks"
    tasks_dir.mkdir(parents=True, exist_ok=True)
    snapshot = {"id": task_id, "assigned_to": actor, "title": "Test task"}
    (tasks_dir / f"{task_id}.json").write_text(json.dumps(snapshot))


def test_complete_actor_returns_unique_actors(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".lattice").mkdir(exist_ok=True)
    _make_snapshot(tmp_path, "ulid1", "human:fede")
    _make_snapshot(tmp_path, "ulid2", "human:fede")
    _make_snapshot(tmp_path, "ulid3", "agent:claude")
    results = complete_actor(None, None, "")
    values = [r.value for r in results]
    assert len(values) == len(set(values))  # unique
    assert "human:fede" in values
    assert "agent:claude" in values


def test_complete_actor_filters_by_prefix(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".lattice").mkdir(exist_ok=True)
    _make_snapshot(tmp_path, "ulid1", "human:fede")
    _make_snapshot(tmp_path, "ulid2", "agent:claude")
    results = complete_actor(None, None, "human")
    values = [r.value for r in results]
    assert "human:fede" in values
    assert "agent:claude" not in values


def _make_resource(tmp_path, name: str) -> None:
    res_dir = tmp_path / ".lattice" / "resources"
    res_dir.mkdir(parents=True, exist_ok=True)
    (res_dir / f"{name}.json").write_text(json.dumps({"name": name}))


def test_complete_resource_name_returns_names(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".lattice").mkdir(exist_ok=True)
    _make_resource(tmp_path, "gpu-slot")
    _make_resource(tmp_path, "db-lock")
    results = complete_resource_name(None, None, "")
    values = [r.value for r in results]
    assert "gpu-slot" in values
    assert "db-lock" in values


def test_complete_resource_name_filters_by_prefix(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".lattice").mkdir(exist_ok=True)
    _make_resource(tmp_path, "gpu-slot")
    _make_resource(tmp_path, "db-lock")
    results = complete_resource_name(None, None, "gpu")
    values = [r.value for r in results]
    assert values == ["gpu-slot"]


def _make_session(tmp_path, name: str) -> None:
    sess_dir = tmp_path / ".lattice" / "sessions"
    sess_dir.mkdir(parents=True, exist_ok=True)
    (sess_dir / f"{name}.json").write_text(json.dumps({"name": name, "status": "active"}))


def test_complete_session_name_returns_names(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".lattice").mkdir(exist_ok=True)
    _make_session(tmp_path, "argus")
    _make_session(tmp_path, "builder")
    results = complete_session_name(None, None, "")
    values = [r.value for r in results]
    assert "argus" in values
    assert "builder" in values
```

**Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/test_completion.py -k "actor or resource or session" -v
```

Expected: `ImportError` for the new functions

**Step 3: Append the three new callbacks to `src/lattice/completion/__init__.py`**

```python
def complete_actor(
    ctx: click.Context | None,
    param: click.Parameter | None,
    incomplete: str,
) -> list[CompletionItem]:
    """Complete actor IDs from recent task snapshots."""
    try:
        root = _find_lattice_root()
        if root is None:
            return []
        tasks_dir = root / ".lattice" / "tasks"
        if not tasks_dir.exists():
            return []
        actors: set[str] = set()
        snapshot_files = sorted(tasks_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        for snap_file in snapshot_files[:50]:
            try:
                data = json.loads(snap_file.read_text())
                actor = data.get("assigned_to")
                if actor and isinstance(actor, str):
                    actors.add(actor)
            except Exception:
                continue
        return [CompletionItem(a) for a in sorted(actors) if a.startswith(incomplete)]
    except Exception:
        return []


def complete_resource_name(
    ctx: click.Context | None,
    param: click.Parameter | None,
    incomplete: str,
) -> list[CompletionItem]:
    """Complete resource names from .lattice/resources/."""
    try:
        root = _find_lattice_root()
        if root is None:
            return []
        res_dir = root / ".lattice" / "resources"
        if not res_dir.exists():
            return []
        names = [p.stem for p in res_dir.glob("*.json") if p.stem.startswith(incomplete)]
        return [CompletionItem(n) for n in sorted(names)]
    except Exception:
        return []


def complete_session_name(
    ctx: click.Context | None,
    param: click.Parameter | None,
    incomplete: str,
) -> list[CompletionItem]:
    """Complete session names from .lattice/sessions/."""
    try:
        root = _find_lattice_root()
        if root is None:
            return []
        sess_dir = root / ".lattice" / "sessions"
        if not sess_dir.exists():
            return []
        names = [p.stem for p in sess_dir.glob("*.json") if p.stem.startswith(incomplete)]
        return [CompletionItem(n) for n in sorted(names)]
    except Exception:
        return []
```

**Step 4: Run all tests**

```bash
uv run pytest tests/test_completion.py -v
```

Expected: all tests PASS

**Step 5: Commit**

```bash
git add src/lattice/completion/__init__.py tests/test_completion.py
git commit -m "feat: add complete_actor, complete_resource_name, complete_session_name"
```

---

## Task 4: Wire `shell_complete=` into existing CLI options

**Files to modify** (add `shell_complete=` parameter to options/arguments — do NOT change any other behavior):

- `src/lattice/cli/task_cmds.py`
- `src/lattice/cli/query_cmds.py`
- `src/lattice/cli/link_cmds.py`
- `src/lattice/cli/artifact_cmds.py`
- `src/lattice/cli/archive_cmds.py`
- `src/lattice/cli/resource_cmds.py`
- `src/lattice/cli/session_cmds.py`
- `src/lattice/cli/main.py` (for `--status` on `init` and similar top-level options)

**Step 1: Identify all options that need callbacks**

Read each file and for every `@click.argument` or `@click.option` that matches these patterns, add the corresponding `shell_complete=`:

| Pattern | Callback to add |
|---------|----------------|
| argument named `task_id` or `target_task_id` | `shell_complete=complete_task_id` |
| `--status` option (string type) | `shell_complete=complete_status` |
| argument/option for actor (`actor_id`, `--assigned-to`) | `shell_complete=complete_actor` |
| `resource` group `<name>` argument | `shell_complete=complete_resource_name` |
| `session` group `<name>` argument | `shell_complete=complete_session_name` |
| `link`/`unlink` TYPE argument | `shell_complete=complete_relationship_type` |

Also check: if `--type`, `--priority`, `--urgency`, `--complexity` are NOT already `click.Choice`, convert them. If they ARE `click.Choice`, leave them — Click auto-completes them.

**Step 2: Add import to each CLI file that uses callbacks**

At the top of each affected file, add:

```python
from lattice.completion import (
    complete_task_id,
    complete_status,
    complete_actor,
    complete_resource_name,
    complete_session_name,
)
```

Only import what that file uses.

**Step 3: Add `complete_relationship_type` to `src/lattice/completion/__init__.py`**

```python
_RELATIONSHIP_TYPES = [
    "blocks",
    "blocked_by",
    "subtask_of",
    "parent_of",
    "related_to",
    "depends_on",
]


def complete_relationship_type(
    ctx: click.Context | None,
    param: click.Parameter | None,
    incomplete: str,
) -> list[CompletionItem]:
    """Complete relationship types for link/unlink commands."""
    return [CompletionItem(t) for t in _RELATIONSHIP_TYPES if t.startswith(incomplete)]
```

**Step 4: Example transformation** (use this pattern for all affected options)

Before:
```python
@click.argument("task_id")
```

After:
```python
from lattice.completion import complete_task_id
# ...
@click.argument("task_id", shell_complete=complete_task_id)
```

Before:
```python
@click.option("--status", default=None, help="...")
```

After:
```python
@click.option("--status", default=None, help="...", shell_complete=complete_status)
```

**Step 5: Verify no existing tests broken**

```bash
uv run pytest tests/ -v --tb=short
```

Expected: all pre-existing tests still PASS (we only added `shell_complete=`, zero behavior change)

**Step 6: Lint**

```bash
uv run ruff check src/lattice/cli/ src/lattice/completion/
```

Expected: no errors

**Step 7: Commit**

```bash
git add src/lattice/cli/ src/lattice/completion/__init__.py
git commit -m "feat: wire shell_complete= callbacks into CLI options and arguments"
```

---

## Task 5: Create `lattice completion` command

**Files:**
- Create: `src/lattice/cli/complete_cmd.py`
- Modify: `src/lattice/cli/main.py` (add import at bottom)
- Create tests in: `tests/test_completion_cmd.py`

**Step 1: Write the failing tests**

```python
# tests/test_completion_cmd.py
import os
import pytest
from click.testing import CliRunner
from lattice.cli.main import cli


@pytest.fixture()
def runner():
    return CliRunner()


def test_completion_print_bash(runner):
    result = runner.invoke(cli, ["completion", "--shell", "bash", "--print"])
    assert result.exit_code == 0
    assert len(result.output.strip()) > 0


def test_completion_print_zsh(runner):
    result = runner.invoke(cli, ["completion", "--shell", "zsh", "--print"])
    assert result.exit_code == 0
    assert len(result.output.strip()) > 0


def test_completion_print_fish(runner):
    result = runner.invoke(cli, ["completion", "--shell", "fish", "--print"])
    assert result.exit_code == 0
    assert len(result.output.strip()) > 0


def test_completion_install_bash(runner, tmp_path):
    bashrc = tmp_path / ".bashrc"
    bashrc.write_text("# existing content\n")
    result = runner.invoke(
        cli,
        ["completion", "--shell", "bash", "--install"],
        env={"HOME": str(tmp_path)},
        catch_exceptions=False,
    )
    assert result.exit_code == 0
    content = bashrc.read_text()
    assert "_LATTICE_COMPLETE=bash_source lattice" in content


def test_completion_install_idempotent(runner, tmp_path):
    bashrc = tmp_path / ".bashrc"
    bashrc.write_text("# existing\n")
    env = {"HOME": str(tmp_path)}
    runner.invoke(cli, ["completion", "--shell", "bash", "--install"], env=env)
    content_after_first = (tmp_path / ".bashrc").read_text()
    runner.invoke(cli, ["completion", "--shell", "bash", "--install"], env=env)
    content_after_second = (tmp_path / ".bashrc").read_text()
    assert content_after_first == content_after_second


def test_completion_uninstall_bash(runner, tmp_path):
    bashrc = tmp_path / ".bashrc"
    bashrc.write_text('# existing\neval "$(_LATTICE_COMPLETE=bash_source lattice)"\n')
    env = {"HOME": str(tmp_path)}
    result = runner.invoke(cli, ["completion", "--shell", "bash", "--uninstall"], env=env)
    assert result.exit_code == 0
    content = bashrc.read_text()
    assert "_LATTICE_COMPLETE=bash_source lattice" not in content
    # backup exists
    assert (tmp_path / ".bashrc.lattice.bak").exists()


def test_completion_install_json(runner, tmp_path):
    import json
    bashrc = tmp_path / ".bashrc"
    bashrc.write_text("")
    result = runner.invoke(
        cli,
        ["completion", "--shell", "bash", "--install", "--json"],
        env={"HOME": str(tmp_path)},
    )
    assert result.exit_code == 0
    data = json.loads(result.output)
    assert data["ok"] is True
    assert data["data"]["action"] in ("installed", "already_installed")
```

**Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/test_completion_cmd.py -v
```

Expected: `Error: No such command 'completion'`

**Step 3: Create `src/lattice/cli/complete_cmd.py`**

```python
"""lattice completion command — shell autocompletion setup."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import click

from lattice.cli.main import cli

_SHELLS = ["bash", "zsh", "fish"]

_EVAL_LINES = {
    "bash": 'eval "$(_LATTICE_COMPLETE=bash_source lattice)"',
    "zsh": 'eval "$(_LATTICE_COMPLETE=zsh_source lattice)"',
}

_FISH_MARKER = "# Generated by lattice completion"


def _detect_shell() -> str:
    """Detect the current shell from $SHELL, defaulting to bash."""
    shell_path = os.environ.get("SHELL", "")
    name = Path(shell_path).name.lower()
    if name in _SHELLS:
        return name
    return "bash"


def _get_script(shell: str) -> str:
    """Return the activation script for the given shell."""
    env = {**os.environ, f"_LATTICE_COMPLETE": f"{shell}_source"}
    result = subprocess.run(
        [sys.argv[0]],
        env={**os.environ, "_LATTICE_COMPLETE": f"{shell}_source"},
        capture_output=True,
        text=True,
    )
    return result.stdout


def _get_config_file(shell: str, home: Path) -> Path:
    if shell == "bash":
        return home / ".bashrc"
    elif shell == "zsh":
        return home / ".zshrc"
    elif shell == "fish":
        fish_dir = home / ".config" / "fish" / "completions"
        fish_dir.mkdir(parents=True, exist_ok=True)
        return fish_dir / "lattice.fish"
    raise ValueError(f"Unknown shell: {shell}")


def _is_installed(shell: str, config_file: Path) -> bool:
    if not config_file.exists():
        return False
    content = config_file.read_text()
    if shell == "fish":
        return _FISH_MARKER in content
    marker = f"_LATTICE_COMPLETE={shell}_source lattice"
    for line in content.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if marker in stripped:
            return True
    return False


def _install(shell: str, config_file: Path) -> None:
    if shell == "fish":
        script = _get_script("fish")
        config_file.write_text(f"{_FISH_MARKER}\n{script}")
    else:
        eval_line = _EVAL_LINES[shell]
        with config_file.open("a") as f:
            f.write(f"\n{eval_line}\n")


def _uninstall(shell: str, config_file: Path) -> None:
    if not config_file.exists():
        return
    # Write backup
    backup = config_file.with_suffix(config_file.suffix + ".lattice.bak")
    shutil.copy2(config_file, backup)
    if shell == "fish":
        config_file.unlink()
        return
    marker = f"_LATTICE_COMPLETE={shell}_source lattice"
    lines = config_file.read_text().splitlines(keepends=True)
    filtered = [l for l in lines if marker not in l]
    config_file.write_text("".join(filtered))


@cli.command("completion")
@click.option(
    "--shell",
    "shell_name",
    type=click.Choice(_SHELLS, case_sensitive=False),
    default=None,
    help="Target shell. Auto-detected from $SHELL if omitted.",
)
@click.option("--print", "do_print", is_flag=True, default=False, help="Print activation script to stdout.")
@click.option("--install", "do_install", is_flag=True, default=False, help="Write to shell config file.")
@click.option("--uninstall", "do_uninstall", is_flag=True, default=False, help="Remove from shell config file.")
@click.option("--json", "as_json", is_flag=True, default=False, help="Machine-readable output.")
def completion_cmd(
    shell_name: str | None,
    do_print: bool,
    do_install: bool,
    do_uninstall: bool,
    as_json: bool,
) -> None:
    """Set up shell tab-completion for lattice.

    \b
    Examples:
      lattice completion                          # print script for current shell
      lattice completion --shell zsh --print      # print zsh script
      lattice completion --shell bash --install   # install for bash
      lattice completion --install                # install for detected shell
      lattice completion --shell zsh --uninstall  # remove zsh completion
    """
    if not do_print and not do_install and not do_uninstall:
        do_print = True  # default action

    shell = shell_name or _detect_shell()
    home = Path(os.environ.get("HOME", "~")).expanduser()
    config_file = _get_config_file(shell, home)

    if do_print:
        script = _get_script(shell)
        click.echo(script, nl=False)
        return

    if do_install:
        if _is_installed(shell, config_file):
            msg = f"Completion already configured in {config_file} — no changes made."
            action = "already_installed"
            if as_json:
                click.echo(json.dumps({"ok": True, "data": {"shell": shell, "file": str(config_file), "action": action}}))
            else:
                click.echo(f"ℹ {msg}", err=True)
            return
        _install(shell, config_file)
        action = "installed"
        reload_hint = f"source {config_file}" if shell != "fish" else "open a new terminal"
        if as_json:
            click.echo(json.dumps({"ok": True, "data": {"shell": shell, "file": str(config_file), "action": action}}))
        else:
            click.echo(f"✓ Completion installed. Reload your shell or run: {reload_hint}", err=True)
        return

    if do_uninstall:
        if not _is_installed(shell, config_file):
            msg = f"Completion not found in {config_file}."
            if as_json:
                click.echo(json.dumps({"ok": True, "data": {"shell": shell, "file": str(config_file), "action": "not_found"}}))
            else:
                click.echo(f"ℹ {msg}", err=True)
            return
        _uninstall(shell, config_file)
        if as_json:
            click.echo(json.dumps({"ok": True, "data": {"shell": shell, "file": str(config_file), "action": "removed"}}))
        else:
            click.echo(f"✓ Completion removed from {config_file} (backup at {config_file}.lattice.bak)", err=True)
```

**Step 4: Register the command in `src/lattice/cli/main.py`**

At the bottom of `main.py`, after the other module imports, add:

```python
from lattice.cli import complete_cmd as _complete_cmd  # noqa: E402, F401
```

**Step 5: Run tests**

```bash
uv run pytest tests/test_completion_cmd.py -v
```

Expected: all tests PASS

**Step 6: Run full test suite**

```bash
uv run pytest tests/ -v --tb=short
```

Expected: all tests PASS

**Step 7: Lint**

```bash
uv run ruff check src/lattice/cli/complete_cmd.py
```

Expected: no errors

**Step 8: Commit**

```bash
git add src/lattice/cli/complete_cmd.py src/lattice/cli/main.py tests/test_completion_cmd.py
git commit -m "feat: add lattice completion command with --print/--install/--uninstall"
```

---

## Task 6: Manual smoke test and final verification

**Step 1: Test `--print` for all shells**

```bash
uv run lattice completion --shell bash --print | head -5
uv run lattice completion --shell zsh --print | head -5
uv run lattice completion --shell fish --print | head -5
```

Expected: each outputs a non-empty script

**Step 2: Test help text at every level**

```bash
uv run lattice --help
uv run lattice completion --help
uv run lattice resource --help
uv run lattice session --help
```

Expected: `completion` appears in root help; all options visible

**Step 3: Test `--install` idempotency manually**

```bash
uv run lattice completion --shell bash --install
uv run lattice completion --shell bash --install  # should say "already installed"
```

**Step 4: Test tab-completion manually (optional, requires sourcing)**

In a bash subshell:
```bash
bash
eval "$(_LATTICE_COMPLETE=bash_source uv run lattice 2>/dev/null || _LATTICE_COMPLETE=bash_source lattice)"
lattice <TAB><TAB>   # should show all commands
lattice status <TAB> # should show task IDs if in a lattice project
```

**Step 5: Run full test suite one final time**

```bash
uv run pytest tests/ -v
uv run ruff check src/ tests/
```

Expected: all tests PASS, ruff clean

**Step 6: Final commit**

```bash
git add -A
git commit -m "feat: shell autocompletion complete — bash/zsh/fish (LAT-XXX)"
```

Replace `LAT-XXX` with the actual Lattice task ID for this work.

---

## Checklist

- [ ] `src/lattice/completion/__init__.py` created with all 6 callbacks
- [ ] `shell_complete=` wired into all `task_id` arguments across CLI files
- [ ] `shell_complete=complete_status` on all `--status` options
- [ ] `click.Choice` checked/applied for `--type`, `--priority`, `--urgency`, `--complexity`
- [ ] `lattice completion` command created with `--print`, `--install`, `--uninstall`, `--json`
- [ ] Fish installs generated script (not a source line)
- [ ] Install is idempotent; uninstall writes `.lattice.bak`
- [ ] All tests pass (`uv run pytest tests/ -v`)
- [ ] Ruff clean (`uv run ruff check src/ tests/`)
