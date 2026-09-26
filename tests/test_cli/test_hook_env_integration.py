"""Integration: a hook fired by a real status change can run the lattice CLI (LAT-284)."""

from __future__ import annotations

import json
import stat
import sys
from pathlib import Path

import pytest

LATTICE_BIN = Path(sys.executable).parent / "lattice"


@pytest.mark.skipif(not LATTICE_BIN.exists(), reason="lattice entry point not installed in venv")
@pytest.mark.parametrize(
    "hooks_for",
    [
        lambda cmd: {"post_event": cmd},
        lambda cmd: {"on": {"status_changed": cmd}},
        lambda cmd: {"transitions": {"backlog -> in_planning": cmd}},
    ],
    ids=["post_event", "on_type", "transitions"],
)
def test_hook_runs_lattice_list_against_its_board(
    tmp_path: Path, initialized_root: Path, invoke, hooks_for
) -> None:
    out_dir = tmp_path / "hook_out"
    out_dir.mkdir()

    # The hook sets no environment of its own and leaves the project, so the
    # CLI can only find the board through the LATTICE_ROOT the hook inherited.
    hook_script = tmp_path / "list_hook.sh"
    hook_script.write_text(
        f"""#!/bin/sh
cd /
"{LATTICE_BIN}" list --json > "{out_dir}/stdout" 2> "{out_dir}/stderr"
echo $? > "{out_dir}/rc"
echo "$LATTICE_ROOT" > "{out_dir}/root"
echo "$LATTICE_DIR" > "{out_dir}/dir"
"""
    )
    hook_script.chmod(hook_script.stat().st_mode | stat.S_IEXEC)

    result = invoke("create", "Hooked task", "--actor", "human:test", "--json")
    assert result.exit_code == 0, result.output
    task_id = json.loads(result.output)["data"]["id"]

    config_path = initialized_root / ".lattice" / "config.json"
    config = json.loads(config_path.read_text())
    config["hooks"] = hooks_for(str(hook_script))
    config_path.write_text(json.dumps(config, sort_keys=True, indent=2) + "\n")

    result = invoke("status", task_id, "in_planning", "--actor", "human:test")
    assert result.exit_code == 0, result.output

    stderr = (out_dir / "stderr").read_text() + (out_dir / "stdout").read_text()
    assert (out_dir / "rc").read_text().strip() == "0", stderr
    listed = json.loads((out_dir / "stdout").read_text())
    assert listed["ok"] is True
    assert [t["id"] for t in listed["data"]] == [task_id]
    assert (out_dir / "root").read_text().strip() == str(initialized_root)
    assert (out_dir / "dir").read_text().strip() == str(initialized_root / ".lattice")
