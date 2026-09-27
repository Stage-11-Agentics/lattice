"""Run parity scenarios, normalize what they produce, and read or write goldens.

A scenario (see ``corpus.py``) runs twice, each time on its own fresh board:
once with plain output and once with ``--json`` appended to every command that
has that option. Each run yields a *capture*: per step its arguments, exit
code, stdout, and stderr, then the final board over its durable paths
(``SPEC.md`` §6.1), then the hook sentinel file when the scenario has one.

Normalization is applied identically when recording and when comparing:

- JSON and JSONL are parsed and kept as objects (the golden is re-dumped with
  sorted keys), so key order and indentation never matter;
- the ``origin`` key is dropped from events and session files (v2 adds it);
- each distinct ULID-shaped token (``task_…``, ``ev_…``, ``art_…``, ``res_…``,
  ``inst_…``, or bare) becomes ``<ID-n>`` in first-seen order, scanning the
  steps in order and then the board files in sorted path order;
- RFC 3339 timestamps become ``<TS>``;
- the temporary board root becomes ``<ROOT>``;
- relative ages in resource messages (``3s ago``, ``expires 10m``) become
  ``<AGO>`` / ``expires <REMAINING>``, since they move with the wall clock.

Record (rewrite) goldens from the current code::

    uv run python -m tests.parity.record              # every scenario
    uv run python -m tests.parity.record lifecycle    # named scenarios

Goldens are the contract later tickets prove parity against; rewrite them only
for a declared change (``SPEC.md`` G-6) and say so in the PR.
"""

from __future__ import annotations

import contextlib
import inspect
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import click
from click.testing import CliRunner

from tests.parity.corpus import (
    SCENARIOS,
    Cli,
    DashboardPost,
    DeleteFile,
    Scenario,
    WriteFile,
)

GOLDEN_DIR = Path(__file__).parent / "golden"
MODES = ("plain", "json")
SENTINEL_NAME = "hook-sentinel.log"

# SPEC §6.1: durable board data plus the workspace class. Runtime, temporary,
# server-control, cache-control, and unmanaged paths are not part of the board.
DURABLE_DIRS = (
    "tasks",
    "events",
    "archive",
    "plans",
    "notes",
    "artifacts",
    "resources",
    "sessions",
    "templates",
    "orchestration",
)
DURABLE_FILES = ("config.json", "ids.json", "context.md", ".gitignore")

ULID_RE = re.compile(r"(?<![0-9A-Za-z])(?:[a-z]+_)?([0-7][0-9A-HJKMNP-TV-Z]{25})(?![0-9A-Za-z])")
TS_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})")
PLACEHOLDER_RE = re.compile(r"<<([^<>]+)>>")
# Relative ages printed by resource commands ("since 0s ago", "expires 10m") depend on
# the wall clock between two commands, so they are normalized like timestamps.
AGO_RE = re.compile(r"\b\d+[smh] ago\b")
REMAINING_RE = re.compile(r"\bexpires \d+[smh]\b")


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


class Normalizer:
    """Stateful normalizer: one per capture, so ``<ID-n>`` numbering is per run."""

    def __init__(self, roots: list[str]) -> None:
        # Longest first, so /private/var/... wins over /var/...
        self._roots = sorted({r for r in roots if r}, key=len, reverse=True)
        self._ids: dict[str, str] = {}

    def text(self, value: str) -> str:
        for root in self._roots:
            value = value.replace(root, "<ROOT>")
        value = ULID_RE.sub(self._id, value)
        value = TS_RE.sub("<TS>", value)
        value = AGO_RE.sub("<AGO>", value)
        return REMAINING_RE.sub("expires <REMAINING>", value)

    def _id(self, match: re.Match[str]) -> str:
        body = match.group(1)
        if body not in self._ids:
            self._ids[body] = f"<ID-{len(self._ids) + 1}>"
        return self._ids[body]

    def obj(self, value: Any) -> Any:
        """Normalize a parsed JSON value, visiting object keys in sorted order."""
        if isinstance(value, dict):
            value = _drop_origin(value)
            out: dict[str, Any] = {}
            for key in sorted(value):
                norm_key = self.text(key)
                out[norm_key] = self.obj(value[key])
            return out
        if isinstance(value, list):
            return [self.obj(v) for v in value]
        if isinstance(value, str):
            return self.text(value)
        return value

    def output(self, raw: str) -> dict[str, Any]:
        """Normalize a stdout/stderr stream: parsed JSON when it is JSON, else lines."""
        stripped = raw.strip()
        if stripped[:1] in ("{", "["):
            try:
                return {"json": self.obj(json.loads(stripped))}
            except ValueError:
                pass
        return {"lines": [self.text(line) for line in raw.splitlines()]}

    def sentinel_line(self, line: str) -> Any:
        """A hook sentinel line; ``STDIN <event json>`` lines are parsed like events."""
        if line.startswith("STDIN "):
            try:
                return {"stdin": self.obj(_strip_origin(json.loads(line[len("STDIN ") :])))}
            except ValueError:
                pass
        return self.text(line)

    def file(self, rel: str, content: bytes, *, session_file: bool) -> Any:
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError:
            return {"binary_bytes": len(content)}
        if rel.endswith(".jsonl"):
            records = []
            for line in text.splitlines():
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    records.append({"unparsed": self.text(line)})
                    continue
                records.append(self.obj(_strip_origin(record)))
            return {"jsonl": records}
        if rel.endswith(".json"):
            try:
                parsed = json.loads(text)
            except ValueError:
                return {"lines": [self.text(line) for line in text.splitlines()]}
            if session_file:
                parsed = _strip_origin(parsed)
            return {"json": self.obj(parsed)}
        return {"lines": [self.text(line) for line in text.splitlines()]}


def _strip_origin(value: Any) -> Any:
    if isinstance(value, dict) and "origin" in value:
        return {k: v for k, v in value.items() if k != "origin"}
    return value


def _drop_origin(value: dict) -> dict:
    """Drop ``origin`` from an event-shaped object wherever it appears."""
    if "origin" in value and "type" in value and "ts" in value:
        return {k: v for k, v in value.items() if k != "origin"}
    return value


# ---------------------------------------------------------------------------
# Running a scenario
# ---------------------------------------------------------------------------


def _json_capable(args: list[str]) -> bool:
    """True when the Click command named by *args* declares a ``--json`` option."""
    from lattice.cli.main import cli

    cmd: click.Command = cli
    ctx = click.Context(cli)
    for token in args:
        if not isinstance(cmd, click.Group):
            break
        sub = cmd.get_command(ctx, token)
        if sub is None:
            break
        cmd = sub
    if cmd is cli:
        return False
    return any("--json" in getattr(p, "opts", ()) for p in cmd.params)


def _runner() -> CliRunner:
    # Click < 8.2 mixes stderr into stdout unless told not to; 8.2+ always separates.
    if "mix_stderr" in inspect.signature(CliRunner.__init__).parameters:
        return CliRunner(mix_stderr=False)  # type: ignore[call-arg]
    return CliRunner()


def _base_env(root: Path) -> dict[str, str | None]:
    """A hermetic environment: nothing from the caller's c11, Lattice, or XDG setup."""
    env: dict[str, str | None] = {
        k: None for k in os.environ if k.startswith(("C11_", "CMUX_", "LATTICE_", "XDG_"))
    }
    home = root / "home"
    home.mkdir(exist_ok=True)
    env.update(
        {
            "LATTICE_ROOT": str(root),
            "LATTICE_NO_UPDATE_CHECK": "1",
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "XDG_DATA_HOME": str(home / ".local" / "share"),
            "XDG_CACHE_HOME": str(home / ".cache"),
            "TZ": "UTC",
        }
    )
    return env


@contextlib.contextmanager
def _chdir(path: Path) -> Iterator[None]:
    prev = os.getcwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(prev)


@contextlib.contextmanager
def _process_env(env: dict[str, str | None]) -> Iterator[None]:
    """Apply *env* to ``os.environ`` for the duration (hooks inherit ``os.environ``)."""
    saved = {k: os.environ.get(k) for k in env}
    try:
        for k, v in env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


class _Board:
    """Reads a board's state to resolve ``<<…>>`` placeholders in step arguments."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.lattice_dir = root / ".lattice"

    def task_id(self, short: str) -> str:
        ids = json.loads((self.lattice_dir / "ids.json").read_text())
        return ids["map"][short]

    def events(self, short: str) -> list[dict]:
        tid = self.task_id(short)
        for base in (self.lattice_dir / "events", self.lattice_dir / "archive" / "events"):
            path = base / f"{tid}.jsonl"
            if path.exists():
                return [json.loads(line) for line in path.read_text().splitlines() if line]
        raise KeyError(f"no event log for {short}")

    def resolve(self, token: str) -> str:
        kind, _, rest = token.partition(":")
        if kind == "root":
            return str(self.root)
        if kind == "task":
            return self.task_id(rest)
        if kind == "event":
            # event:<short>:<type>:<n>  -> the id of the n-th event of that type
            short, etype, n = rest.split(":")
            matches = [e for e in self.events(short) if e["type"] == etype]
            return matches[int(n)]["id"]
        if kind == "artifact":
            # artifact:<short>:<n>  -> the n-th artifact attached to the task
            short, n = rest.split(":")
            matches = [e for e in self.events(short) if e["type"] == "artifact_attached"]
            return matches[int(n)]["data"]["artifact_id"]
        raise ValueError(f"unknown placeholder <<{token}>>")

    def expand(self, value: str) -> str:
        return PLACEHOLDER_RE.sub(lambda m: self.resolve(m.group(1)), value)


def init_args(root: Path) -> list[str]:
    return [
        "init",
        "--path",
        str(root),
        "--actor",
        "human:parity",
        "--project-code",
        "PAR",
        "--no-setup-claude",
        "--no-setup-agents",
        "--no-seed",
    ]


def run_scenario(scenario: Scenario, root: Path, *, mode: str) -> dict[str, Any]:
    """Run *scenario* on a fresh board at *root*; return the normalized capture."""
    from lattice.cli.main import cli

    assert mode in MODES
    root.mkdir(parents=True, exist_ok=True)
    runner = _runner()
    env = _base_env(root)
    board = _Board(root)
    raw_steps: list[dict[str, Any]] = []

    def invoke(args: list[str], *, stdin: str | None = None, step_env: dict | None = None):
        call_env = dict(env)
        if step_env:
            call_env.update(step_env)
        with _chdir(root), _process_env(call_env):
            result = runner.invoke(cli, args, env=call_env, input=stdin, catch_exceptions=True)
        exc = result.exception
        entry: dict[str, Any] = {
            "args": args,
            "exit_code": result.exit_code,
            "stdout": result.stdout,
            "stderr": _stderr(result),
        }
        if exc is not None and not isinstance(exc, SystemExit):
            entry["exception"] = f"{type(exc).__name__}: {exc}"
        return entry

    # Step 0: a fresh board, then the scenario's config patch (auto-review off always).
    raw_steps.append(invoke(init_args(root)))
    _patch_config(board.lattice_dir, scenario.config, root)

    for step in scenario.steps:
        if isinstance(step, Cli):
            if mode == "json" and step.plain_only:
                continue
            args = [board.expand(a) for a in step.args]
            if mode == "json" and _json_capable(args):
                args.append("--json")
            step_env = {k: board.expand(v) for k, v in step.env.items()} if step.env else None
            raw_steps.append(invoke(args, stdin=step.stdin, step_env=step_env))
        elif isinstance(step, WriteFile):
            path = root / board.expand(step.path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(board.expand(step.text), encoding="utf-8")
            raw_steps.append({"write_file": board.expand(step.path)})
        elif isinstance(step, DeleteFile):
            (root / board.expand(step.path)).unlink()
            raw_steps.append({"delete_file": board.expand(step.path)})
        elif isinstance(step, DashboardPost):
            raw_steps.append(_dashboard_post(board.lattice_dir, step.path, step.body, env))
        else:  # pragma: no cover - corpus authoring error
            raise TypeError(f"unknown step {step!r}")

    return _normalize_capture(scenario, mode, root, raw_steps)


def _stderr(result: Any) -> str:
    try:
        return result.stderr or ""
    except ValueError:  # stderr not captured separately
        return ""


def _patch_config(lattice_dir: Path, patch: dict[str, Any], root: Path) -> None:
    path = lattice_dir / "config.json"
    config = json.loads(path.read_text())
    config["auto_code_review_on_transition"] = False
    config["auto_plan_review_on_transition"] = False
    _deep_merge(config, json.loads(json.dumps(patch).replace("<<root>>", str(root))))
    path.write_text(json.dumps(config, sort_keys=True, indent=2) + "\n")


def _deep_merge(base: dict, patch: dict) -> None:
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _deep_merge(base[key], value)
        else:
            base[key] = value


def _dashboard_post(lattice_dir: Path, path: str, body: Any, env: dict) -> dict[str, Any]:
    """POST through the in-process dashboard server (as ``tests/test_dashboard`` does)."""
    from lattice.dashboard.server import create_server

    with _process_env(env):
        server = create_server(lattice_dir, "127.0.0.1", 0)
        thread = threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        )
        thread.start()
        try:
            port = server.server_address[1]
            data = json.dumps(body).encode() if not isinstance(body, str) else body.encode()
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}{path}",
                data=data,
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            try:
                with urllib.request.urlopen(req, timeout=10) as resp:
                    status, payload = resp.status, resp.read().decode()
            except urllib.error.HTTPError as err:
                status, payload = err.code, err.read().decode()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)
    return {"post": path, "request": body, "status": status, "response": payload}


def _board_files(root: Path) -> list[tuple[str, bytes]]:
    lattice_dir = root / ".lattice"
    found: list[tuple[str, bytes]] = []
    for name in DURABLE_FILES:
        path = lattice_dir / name
        if path.is_file():
            found.append((name, path.read_bytes()))
    for name in DURABLE_DIRS:
        base = lattice_dir / name
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if path.is_file():
                found.append((path.relative_to(lattice_dir).as_posix(), path.read_bytes()))
    return sorted(found)


def _normalize_capture(
    scenario: Scenario, mode: str, root: Path, raw_steps: list[dict[str, Any]]
) -> dict[str, Any]:
    norm = Normalizer([str(root), str(root.resolve()), os.path.realpath(root)])
    steps: list[dict[str, Any]] = []
    for raw in raw_steps:
        if "args" in raw:
            step: dict[str, Any] = {
                "args": [norm.text(a) for a in raw["args"]],
                "exit_code": raw["exit_code"],
                "stdout": norm.output(raw["stdout"]),
                "stderr": norm.output(raw["stderr"]),
            }
            if "exception" in raw:
                step["exception"] = norm.text(raw["exception"])
        elif "post" in raw:
            step = {
                "post": raw["post"],
                "request": norm.obj(raw["request"]),
                "status": raw["status"],
                "response": norm.output(raw["response"]),
            }
        else:
            step = {k: norm.text(v) for k, v in raw.items()}
        steps.append(step)

    board: dict[str, Any] = {}
    for rel, content in _board_files(root):
        norm_rel = norm.text(rel)
        board[norm_rel] = norm.file(rel, content, session_file=rel.startswith("sessions/"))

    capture: dict[str, Any] = {
        "scenario": scenario.name,
        "mode": mode,
        "description": scenario.description,
        "steps": steps,
        "board": board,
    }
    sentinel = root / SENTINEL_NAME
    if scenario.config.get("hooks") or sentinel.exists():
        text = sentinel.read_text() if sentinel.exists() else ""
        capture["sentinel"] = [norm.sentinel_line(line) for line in text.splitlines()]
    return capture


# ---------------------------------------------------------------------------
# Goldens
# ---------------------------------------------------------------------------


def golden_path(name: str, mode: str) -> Path:
    return GOLDEN_DIR / f"{name}.{mode}.json"


def dump(capture: dict[str, Any]) -> str:
    return json.dumps(capture, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def load_golden(name: str, mode: str) -> dict[str, Any]:
    return json.loads(golden_path(name, mode).read_text(encoding="utf-8"))


def capture_in_tmp(scenario: Scenario, mode: str) -> dict[str, Any]:
    tmp = Path(tempfile.mkdtemp(prefix="lattice-parity-"))
    try:
        return run_scenario(scenario, tmp / "board", mode=mode)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main(argv: list[str]) -> int:
    wanted = set(argv)
    unknown = wanted - {s.name for s in SCENARIOS}
    if unknown:
        print(f"unknown scenarios: {', '.join(sorted(unknown))}", file=sys.stderr)
        return 2
    GOLDEN_DIR.mkdir(exist_ok=True)
    for scenario in SCENARIOS:
        if wanted and scenario.name not in wanted:
            continue
        for mode in MODES:
            golden_path(scenario.name, mode).write_text(
                dump(capture_in_tmp(scenario, mode)), encoding="utf-8"
            )
            print(f"recorded {scenario.name}.{mode}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
