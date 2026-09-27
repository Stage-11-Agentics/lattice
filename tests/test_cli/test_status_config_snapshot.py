"""``lattice status`` reads the configuration once, before the write: a hook that
edits ``config.json`` does not change the current command's auto-review or hints."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from click.testing import CliRunner

from lattice.cli.main import cli


def test_hook_that_edits_config_does_not_change_this_command(
    initialized_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ld = initialized_root / ".lattice"
    config_path = ld / "config.json"
    editor = tmp_path / "edit_config.py"
    editor.write_text(
        "import json, sys\n"
        "p = sys.argv[1]\n"
        "c = json.load(open(p))\n"
        "c['review_mode'] = 'triple'\n"
        "c['auto_code_review_on_transition'] = True\n"
        "open(p, 'w').write(json.dumps(c))\n"
    )
    config = json.loads(config_path.read_text())
    config["review_mode"] = "single"
    config["auto_code_review_on_transition"] = False
    config["hooks"] = {"transitions": {"* -> review": f"{sys.executable} {editor} {config_path}"}}
    config_path.write_text(json.dumps(config))

    spawned: list = []
    real_popen = subprocess.Popen

    def guarded_popen(args, *a, **kw):  # noqa: ANN001, ANN002, ANN003, ANN202
        if any(str(arg) in ("code-review", "plan-review") for arg in args):
            spawned.append(args)
            pytest.fail(f"a review was spawned: {args}")
        return real_popen(args, *a, **kw)

    monkeypatch.setattr(subprocess, "Popen", guarded_popen)
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    monkeypatch.chdir(repo)
    env = {"LATTICE_ROOT": str(initialized_root)}
    runner = CliRunner()
    created = runner.invoke(cli, ["create", "T", "--actor", "agent:t", "--json"], env=env)
    task_id = json.loads(created.output)["data"]["id"]
    runner.invoke(
        cli,
        ["status", task_id, "in_progress", "--force", "--reason", "r", "--actor", "agent:t"],
        env=env,
    )

    out = runner.invoke(
        cli, ["status", task_id, "review", "--actor", "agent:t", "--json"], env=env
    )

    assert out.exit_code == 0, out.output
    data = json.loads(out.output)["data"]
    # The hook ran and rewrote the file...
    assert json.loads(config_path.read_text())["review_mode"] == "triple"
    # ...but this command still used the configuration it started with.
    assert data["next_steps"]["review_mode"] == "single"
    assert data["auto_review"]["fired"] is False
    assert spawned == []
