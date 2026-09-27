#!/usr/bin/env python3
"""Run every command block of docs/hosted/guide.md, in order, on a scratch machine state.

The guide's runtime proof (H-17). Each ```bash block runs in a fresh ``bash -e``
that inherits the previous block's exported variables and working directory,
so the blocks behave as one shell session and any failing command fails its
block. A block preceded by ``<!-- guide: skip: <reason> -->`` is reported as
skipped with that reason; blocks in other languages (``nginx``, ``json``, ...)
are shown files, not commands, and are listed as such.

Everything runs under a temporary ``HOME`` with ``XDG_*`` pointed inside it,
``LATTICE_*`` variables cleared, and the given ``lattice`` first on ``PATH``.
Section 1 needs a repository whose board is tracked in git and that has two
linked worktrees; the runner builds one and starts the first block there.

Usage:
    uv run python scripts/run_hosted_guide.py [--guide PATH] [--bin DIR]
        [--keep-going] [--keep] [--report PATH] [--setup SCRIPT]

For docs/hosted/api.md, pass ``--setup scripts/hosted_api_setup.sh``: it runs
the guide's quick start (a server on 127.0.0.1:8740, project ``demo``, and a
token in ``$HOME/lattice-trial/token``) so the curl examples have a server.

Exit status is 0 when every runnable block passed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
FENCE = re.compile(r"^(?P<indent>[ \t]*)```(?P<lang>[A-Za-z0-9_-]*)\s*$")
DIRECTIVE = re.compile(r"^\s*<!--\s*guide:\s*skip:\s*(?P<reason>.*?)\s*-->\s*$")
HEADING = re.compile(r"^(#{1,3})\s+(?P<title>.+)$")
BLOCK_TIMEOUT = 180
# curl exits 0 on an HTTP error, so a printed error envelope also fails a block.
ENVELOPE_ERROR = re.compile(r'"ok":\s*false')


@dataclass
class Block:
    index: int
    line: int
    lang: str
    section: str
    code: str
    skip: str | None = None
    status: str = "pending"
    seconds: float = 0.0
    output: str = ""
    notes: list[str] = field(default_factory=list)


def parse(text: str) -> list[Block]:
    blocks: list[Block] = []
    lines = text.splitlines()
    section = ""
    pending_skip: str | None = None
    i = 0
    while i < len(lines):
        line = lines[i]
        heading = HEADING.match(line)
        if heading:
            section = heading.group("title")
        directive = DIRECTIVE.match(line)
        if directive:
            pending_skip = directive.group("reason")
            i += 1
            continue
        fence = FENCE.match(line)
        if fence:
            indent = fence.group("indent")
            body: list[str] = []
            start = i + 1
            i += 1
            while i < len(lines) and lines[i].strip() != "```":
                body.append(lines[i][len(indent) :] if lines[i].startswith(indent) else lines[i])
                i += 1
            blocks.append(
                Block(
                    index=len(blocks) + 1,
                    line=start,
                    lang=fence.group("lang") or "text",
                    section=section,
                    code="\n".join(body) + "\n",
                    skip=pending_skip,
                )
            )
            pending_skip = None
        elif line.strip():
            # A directive applies only to the fence that follows it.
            pending_skip = pending_skip if line.strip().startswith("<!--") else None
        i += 1
    return blocks


def run(cmd: list[str], cwd: Path, env: dict[str, str]) -> None:
    subprocess.run(cmd, cwd=cwd, env=env, check=True, capture_output=True, text=True)


def build_fixture(home: Path, env: dict[str, str]) -> Path:
    """A repository with a git-tracked board and two linked worktrees (section 1)."""
    primary = home / "src" / "proj"
    primary.mkdir(parents=True)
    run(["git", "init", "-q", "-b", "main"], primary, env)
    run(["lattice", "init", "--project-code", "FIX", "--actor", "human:fixture"], primary, env)
    run(["lattice", "create", "Fixture task", "--actor", "human:fixture"], primary, env)
    (primary / "README.md").write_text("fixture\n")
    run(["git", "add", "-A"], primary, env)
    run(["git", "commit", "-q", "-m", "fixture with a tracked board"], primary, env)
    for name in ("wt-a", "wt-b"):
        run(["git", "worktree", "add", "-q", "-b", name, str(home / "src" / name)], primary, env)
    return primary


def scratch_env(home: Path, bindir: Path) -> dict[str, str]:
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("LATTICE_", "XDG_", "C11_", "GIT_"))
    }
    env.update(
        HOME=str(home),
        XDG_CONFIG_HOME=str(home / ".config"),
        XDG_DATA_HOME=str(home / ".local" / "share"),
        PATH=f"{bindir}{os.pathsep}{env.get('PATH', '')}",
        GIT_AUTHOR_NAME="Guide Runner",
        GIT_AUTHOR_EMAIL="guide@example.invalid",
        GIT_COMMITTER_NAME="Guide Runner",
        GIT_COMMITTER_EMAIL="guide@example.invalid",
        NO_COLOR="1",
    )
    return env


def run_block(block: Block, state: Path, cwd_file: Path, work: Path) -> None:
    script = work / f"block-{block.index:02d}.sh"
    script.write_text(block.code)
    wrapper = (
        f"source {shlex.quote(str(state))}\n"
        f'cd "$(cat {shlex.quote(str(cwd_file))})"\n'
        "set -a\n"
        f"source {shlex.quote(str(script))}\n"
        f"export -p > {shlex.quote(str(state))}.next\n"
        f"pwd > {shlex.quote(str(cwd_file))}.next\n"
    )
    started = time.monotonic()
    try:
        proc = subprocess.run(
            ["bash", "-e", "-o", "pipefail", "-c", wrapper],
            env={"PATH": os.environ["PATH"], "HOME": os.environ.get("HOME", "/")},
            capture_output=True,
            text=True,
            timeout=BLOCK_TIMEOUT,
            stdin=subprocess.DEVNULL,
        )
        code = proc.returncode
        out = proc.stdout + proc.stderr
    except subprocess.TimeoutExpired as exc:
        code = -1
        out = f"{exc.stdout or ''}{exc.stderr or ''}\n[timed out after {BLOCK_TIMEOUT} s]"
    block.seconds = time.monotonic() - started
    block.output = out if isinstance(out, str) else out.decode(errors="replace")
    if code == 0 and ENVELOPE_ERROR.search(block.output):
        code = "ok, but printed an error envelope"
    if code == 0:
        Path(f"{state}.next").replace(state)
        Path(f"{cwd_file}.next").replace(cwd_file)
        block.status = "ok"
    else:
        block.status = f"failed (exit {code})"


def main() -> int:
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--guide", default=str(REPO / "docs" / "hosted" / "guide.md"))
    ap.add_argument(
        "--bin", default=str(REPO / ".venv" / "bin"), help="directory holding the lattice to test"
    )
    ap.add_argument("--keep-going", action="store_true", help="run later blocks after a failure")
    ap.add_argument("--keep", action="store_true", help="keep the scratch directory")
    ap.add_argument("--report", help="write a JSON report here")
    ap.add_argument(
        "--setup", help="a bash script run first, in the same shell state (for api.md)"
    )
    args = ap.parse_args()

    blocks = parse(Path(args.guide).read_text())
    work = Path(tempfile.mkdtemp(prefix="lattice-guide-"))
    home = work / "home"
    home.mkdir()
    env = scratch_env(home, Path(args.bin).resolve())
    primary = build_fixture(home, env)

    state = work / "state.sh"
    state.write_text("".join(f"export {k}={shlex.quote(v)}\n" for k, v in env.items()))
    cwd_file = work / "cwd"
    cwd_file.write_text(str(primary))

    failed = False
    if args.setup:
        setup = Block(0, 0, "bash", "setup", Path(args.setup).read_text())
        run_block(setup, state, cwd_file, work)
        if setup.status != "ok":
            print("setup failed:\n" + setup.output)
            return 1
    for block in blocks:
        if block.lang not in ("bash", "sh"):
            block.status = f"shown ({block.lang})"
            continue
        if block.skip is not None:
            block.status = "skipped"
            continue
        if failed and not args.keep_going:
            block.status = "not run (earlier failure)"
            continue
        run_block(block, state, cwd_file, work)
        if block.status != "ok":
            failed = True

    # Stop any server the guide left running.
    pidfile = home / "lattice-trial" / "server.pid"
    if pidfile.exists():
        try:
            os.kill(int(pidfile.read_text().strip()), signal.SIGTERM)
        except (ValueError, ProcessLookupError):
            pass

    for block in blocks:
        first = block.code.strip().splitlines()[0] if block.code.strip() else ""
        reason = f": {block.skip}" if block.status == "skipped" else ""
        print(
            f"[{block.index:02d}] line {block.line:4d}  {block.status}{reason}  ({block.section}) {first[:60]}"
        )
        if block.status.startswith("failed"):
            print("      " + "\n      ".join(block.output.rstrip().splitlines()[-25:]))
    ran = [b for b in blocks if b.status == "ok"]
    print(
        f"\n{len(ran)} ran ok, {sum(b.status.startswith('failed') for b in blocks)} failed, "
        f"{sum(b.status == 'skipped' for b in blocks)} skipped, "
        f"{sum(b.status.startswith('shown') for b in blocks)} shown files, "
        f"{sum(b.status.startswith('not run') for b in blocks)} not run"
    )
    if args.report:
        Path(args.report).write_text(
            json.dumps(
                [
                    {
                        k: getattr(b, k)
                        for k in (
                            "index",
                            "line",
                            "lang",
                            "section",
                            "status",
                            "skip",
                            "seconds",
                            "output",
                        )
                    }
                    for b in blocks
                ],
                indent=2,
            )
            + "\n"
        )
    if args.keep:
        print(f"scratch kept at {work}")
    else:
        subprocess.run(["chmod", "-R", "u+w", str(work)], check=False)
        shutil.rmtree(work, ignore_errors=True)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
