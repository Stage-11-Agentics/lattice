"""AC-5 (hosted part, H-12): ``lattice context write`` on one bound checkout
replaces the board's ``context.md``, and another checkout reads the new text
from its cache."""

from __future__ import annotations

import json
from pathlib import Path

from tests.test_remote.hosted import HostedEnv, make_repo, run_cli

CONTEXT = "# Context\n\nWhy this board exists.\n"


def test_context_written_on_one_client_is_read_from_anothers_cache(
    hosted_env: HostedEnv, tmp_path: Path
) -> None:
    a = make_repo(tmp_path / "machine-a" / "repo")
    b = make_repo(tmp_path / "machine-b" / "repo")
    for repo in (a, b):
        assert run_cli(repo, "remote", "attach", "team", "demo").exit_code == 0

    source = tmp_path / "context-src.md"
    source.write_text(CONTEXT)
    written = run_cli(a, "context", "write", "--file", str(source), "--json")
    assert written.exit_code == 0, written.output
    assert json.loads(written.stdout)["ok"] is True
    assert (hosted_env.board / "context.md").read_text() == CONTEXT

    # B catches up at its next command and reads the new file from its cache.
    assert run_cli(b, "list").exit_code == 0
    assert (b / ".lattice" / "context.md").read_text() == CONTEXT

    # Written again from B, from stdin; A sees it.
    again = run_cli(b, "context", "write", "--stdin", input="# Context v2\n")
    assert again.exit_code == 0, again.output
    assert run_cli(a, "list").exit_code == 0
    assert (a / ".lattice" / "context.md").read_text() == "# Context v2\n"
