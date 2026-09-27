"""AC-32: the hosted docs are present, every command they name exists, and they
name no real host (SPEC §13, EVALUATION AC-32).

Commands are read from every fenced shell block of the guide, the API page,
and the README, plus the inline `lattice ...` spans of the guide, the skills,
and the CLAUDE.md block; each must resolve to a Click command whose `--help`
exits 0.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path

import click
import pytest
from click.testing import CliRunner

from lattice.cli.main import cli
from lattice.templates.claude_md_block import CLAUDE_MD_BLOCK

REPO = Path(__file__).resolve().parents[1]
HOSTED = REPO / "docs" / "hosted"
GUIDE = HOSTED / "guide.md"
API = HOSTED / "api.md"
DEPLOY = HOSTED / "deploy"
TEMPLATES = (
    "lattice-server.service.example",
    "lattice-server.plist.example",
    "logrotate.example",
    "newsyslog.conf.example",
)

# Commands the docs name whose implementation is in an open ticket, not yet on
# this branch. H-17 drafts ahead of them; each entry is removed when its ticket
# merges, and the list is empty when H-17 lands (it is).
AWAITING_MERGE: dict[str, str] = {}

FENCE = re.compile(r"^[ \t]*```([\w-]*)[ \t]*\n(.*?)^[ \t]*```", re.M | re.S)
INLINE = re.compile(r"`(lattice [^`]+)`")


def _shell_blocks(text: str) -> list[str]:
    return [m.group(2) for m in FENCE.finditer(text) if m.group(1) in ("bash", "sh")]


def _lattice_invocations(code: str) -> list[list[str]]:
    """Every `lattice ...` argv in a block of shell, continuation lines joined."""
    code = code.replace("\\\n", " ")
    found = []
    for line in code.splitlines():
        for part in re.split(r"\$\(|&&|\|\||[|;()]", line):
            part = re.sub(r"^\s*sudo\s+(-u\s+\S+\s+)?", "", part.strip())
            match = re.search(r"(?:^|\s)(lattice\s.*)$", part)
            if not match or part.startswith("#"):
                continue
            try:
                argv = shlex.split(match.group(1), comments=True)
            except ValueError:
                argv = match.group(1).split()
            found.append(argv[1:])
    return found


def _command_path(argv: list[str]) -> tuple[str, click.Command | None]:
    """Walk the Click tree along argv; return the path walked and the command
    (``None`` when a word names no subcommand of a group)."""
    command: click.Command = cli
    path: list[str] = []
    for word in argv:
        if not isinstance(command, click.Group) or word.startswith("-"):
            break
        sub = command.commands.get(word)
        if sub is None:
            if path == ["plan"]:  # `lattice plan LAT-5`: the group's legacy read
                break
            return " ".join([*path, word]), None
        path.append(word)
        command = sub
    return " ".join(path), command


def _awaiting(path: str) -> str | None:
    for prefix, ticket in AWAITING_MERGE.items():
        if path == prefix or path.startswith(prefix + " "):
            return ticket
    return None


def _doc_commands() -> dict[str, set[str]]:
    """Command path -> the documents naming it."""
    sources: dict[str, str] = {
        "guide.md": GUIDE.read_text(),
        "api.md": API.read_text(),
        "README.md": (REPO / "README.md").read_text(),
    }
    inline_only = {
        "skills/lattice/SKILL.md": (REPO / "skills/lattice/SKILL.md").read_text(),
        "src/lattice/skills/lattice/SKILL.md": (
            REPO / "src/lattice/skills/lattice/SKILL.md"
        ).read_text(),
        "claude_md_block": CLAUDE_MD_BLOCK,
    }
    argvs: list[tuple[str, list[str]]] = []
    for name, text in sources.items():
        for block in _shell_blocks(text):
            argvs += [(name, a) for a in _lattice_invocations(block)]
    for name, text in {"guide.md": sources["guide.md"], **inline_only}.items():
        for span in INLINE.findall(text):
            argvs += [(name, a) for a in _lattice_invocations(span)]
    commands: dict[str, set[str]] = {}
    for name, argv in argvs:
        path, _ = _command_path(argv)
        if path:
            commands.setdefault(path, set()).add(name)
    return commands


def test_hosted_docs_present() -> None:
    for path in (
        GUIDE,
        API,
        *(DEPLOY / t for t in TEMPLATES),
        REPO / "docs/architecture/operations.md",
        REPO / "docs/architecture/hosted.md",
    ):
        assert path.is_file(), path
        assert path.read_text().strip(), path


def test_guide_covers_ac32_topics() -> None:
    guide = GUIDE.read_text()
    for needle in (
        "uv tool install 'lattice-tracker[server]'",
        "lattice server init",
        "lattice server project create",
        "lattice server project import",
        "lattice server token create",
        "lattice server token grant",
        "lattice server token revoke",
        "deploy/lattice-server.service.example",
        "deploy/lattice-server.plist.example",
        "deploy/logrotate.example",
        "deploy/newsyslog.conf.example",
        "terminate TLS",
        "not buffer responses",
        "read timeout",
        "never redirect an API path",
        "lattice remote attach",
        "lattice setup-claude --force",
        "lattice setup-claude-skill --force",
        "lattice remote verify",
        "lattice remote op-status",
        "rotate-epoch",
        "lattice cache clear",
    ):
        assert needle in guide, needle


def test_every_documented_command_exists() -> None:
    commands = _doc_commands()
    assert "server serve" in commands and "plan write" in commands
    runner = CliRunner()
    missing, broken = [], []
    for path, docs in sorted(commands.items()):
        if _awaiting(path):
            continue
        walked, command = _command_path(path.split())
        if command is None:
            missing.append(f"lattice {path} ({', '.join(sorted(docs))})")
            continue
        result = runner.invoke(cli, [*walked.split(), "--help"])
        if result.exit_code != 0:
            broken.append(f"lattice {walked} --help exited {result.exit_code}")
    assert not missing, "documented commands that do not exist:\n" + "\n".join(missing)
    assert not broken, "\n".join(broken)


def test_awaiting_merge_entries_are_still_missing() -> None:
    """An entry whose command now exists must be removed from AWAITING_MERGE."""
    landed = [p for p in AWAITING_MERGE if _command_path(p.split())[1] is not None]
    assert not landed, f"these commands have landed; drop them from AWAITING_MERGE: {landed}"


# Hosts the docs may name: placeholders, loopback, the documentation address
# ranges (RFC 5737), and public project links.
_ALLOWED_HOSTS = re.compile(
    r"^(localhost|127\.0\.0\.1|\[?::1\]?|0\.0\.0\.0"
    r"|(192\.0\.2|198\.51\.100|203\.0\.113)\.\d{1,3}"
    r"|([a-z0-9-]+\.)*example(\.(internal|com|org|net|invalid))?"
    r"|([a-z0-9-]+\.)*(invalid|test)"
    r"|github\.com|www\.apple\.com|claude\.com|docs\.anthropic\.com)$"
)
_URL_HOST = re.compile(r"\b[a-z][a-z0-9+.-]*://(?:[^@/\s]+@)?([^/:\s\"'<>)`]+)", re.I)
_BARE_HOST = re.compile(
    r"(?<![\w./-])((?:[a-z0-9-]+\.)+(?:internal|com|net|org|io|dev|app|cloud|lan|local))\b",
    re.I,
)


@pytest.mark.parametrize(
    "path",
    [GUIDE, API, *(DEPLOY / t for t in TEMPLATES)],
    ids=lambda p: p.name,
)
def test_no_real_hostnames(path: Path) -> None:
    text = path.read_text()
    hosts = {m.group(1).lower() for m in _URL_HOST.finditer(text)}
    hosts |= {m.group(1).lower() for m in _BARE_HOST.finditer(text)}
    real = sorted(h for h in hosts if not _ALLOWED_HOSTS.match(h))
    assert not real, f"{path.name} names non-placeholder hosts: {real}"


def test_hostname_check_catches_a_real_host() -> None:
    sample = (
        "curl https://lattice.acme-corp.io/healthz and server_name board.acme.dev;"
        " proxy_pass http://192.0.2.20:8740; proxy_pass http://10.1.2.3:8740;"
    )
    hosts = {m.group(1).lower() for m in _URL_HOST.finditer(sample)}
    hosts |= {m.group(1).lower() for m in _BARE_HOST.finditer(sample)}
    assert sorted(h for h in hosts if not _ALLOWED_HOSTS.match(h)) == [
        "10.1.2.3",
        "board.acme.dev",
        "lattice.acme-corp.io",
    ]
