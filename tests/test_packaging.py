"""AC-30, G-4: the base install gains no runtime dependency, and importing the CLI loads
no server library; without the extra, ``lattice server serve`` exits 1 with a hint."""

from __future__ import annotations

import json
import subprocess
import sys
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PINNED = [
    "click>=8.1",
    "python-ulid>=2.0",
    "filelock>=3.13",
    "typing_extensions>=4.0; python_version<'3.14'",
]
SERVER_LIBS = ("starlette", "uvicorn", "sse_starlette")

# A meta-path finder that makes the server extra's libraries unimportable, so the
# subprocess behaves like an install without the extra.
_BLOCK = """
import sys
class _Block:
    def find_spec(self, name, path=None, target=None):
        if name.split(".")[0] in {libs!r}:
            raise ModuleNotFoundError(f"No module named {{name!r}}")
        return None
sys.meta_path.insert(0, _Block())
"""


def _run(code: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", _BLOCK.format(libs=SERVER_LIBS) + code],
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_base_dependencies_are_pinned() -> None:
    project = tomllib.loads((REPO / "pyproject.toml").read_text())["project"]
    assert project["dependencies"] == PINNED
    server = project["optional-dependencies"]["server"]
    assert sorted(d.split(">")[0] for d in server) == ["sse-starlette", "starlette", "uvicorn"]


def test_cli_imports_without_the_extra_and_loads_no_server_library() -> None:
    proc = _run(
        "import lattice.cli.main, lattice.cli.server_cmds, lattice.server.admin\n"
        "import lattice.server.tokens, lattice.server.control\n"
        f"bad = sorted(m for m in sys.modules if m.split('.')[0] in {SERVER_LIBS!r})\n"
        "assert not bad, bad\n"
        "print('ok')\n"
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "ok"


def test_serve_without_the_extra_exits_1_with_a_hint(tmp_path: Path) -> None:
    proc = _run(
        "from click.testing import CliRunner\n"
        "from lattice.cli.main import cli\n"
        f"r = CliRunner().invoke(cli, ['server', 'serve', '--root', {str(tmp_path)!r}])\n"
        "print(r.exit_code)\n"
        "print(r.output)\n"
    )
    assert proc.returncode == 0, proc.stderr
    exit_code, output = proc.stdout.split("\n", 1)
    assert exit_code == "1"
    assert "lattice-tracker[server]" in output


def test_serve_without_the_extra_under_json(tmp_path: Path) -> None:
    proc = _run(
        "import json\n"
        "from click.testing import CliRunner\n"
        "from lattice.cli.main import cli\n"
        f"r = CliRunner().invoke(cli, ['server', 'serve', '--root', {str(tmp_path)!r}, '--json'])\n"
        "print(r.exit_code)\n"
        "print(json.dumps(json.loads(r.output)))\n"
    )
    assert proc.returncode == 0, proc.stderr
    exit_code, output = proc.stdout.strip().split("\n", 1)
    assert exit_code == "1"
    envelope = json.loads(output)
    assert envelope["ok"] is False and envelope["error"]["code"] == "SERVER_EXTRA_MISSING"
    assert "lattice-tracker[server]" in envelope["error"]["message"]
