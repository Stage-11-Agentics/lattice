"""AC-32: the hosted docs are present, every command they name exists, and they
name no real host (SPEC §13, EVALUATION AC-32).

Commands are read from every fenced shell block of the guide, the API page,
and the README, plus the inline `lattice ...` spans of the guide, the skills,
and the CLAUDE.md block; each must resolve to a Click command whose `--help`
exits 0.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import re
import shlex
import subprocess
import urllib.request
from pathlib import Path

import click
import pytest
from click.testing import CliRunner

from lattice.cli.main import cli, load_all_commands
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


@pytest.mark.parametrize("path", [GUIDE, API], ids=lambda path: path.name)
def test_hosted_bash_examples_parse(path: Path) -> None:
    """Every fenced Bash example in the hosted contract must be pasteable syntax."""
    text = path.read_text()
    for match in FENCE.finditer(text):
        if match.group(1) != "bash":
            continue
        line = text.count("\n", 0, match.start()) + 1
        result = subprocess.run(
            ["bash", "-n"], input=match.group(2), capture_output=True, text=True, timeout=5
        )
        assert result.returncode == 0, (
            f"{path.relative_to(REPO)}:{line} has invalid Bash syntax:\n{result.stderr}"
        )


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
    load_all_commands()  # the root group imports command modules on demand
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


def test_review_gate_v2_changes_are_declared_in_compatibility_docs() -> None:
    spec = (HOSTED / "SPEC.md").read_text()
    readme = (REPO / "README.md").read_text()
    user_reference = (REPO / "docs" / "user-reference.md").read_text()
    g6 = next(line for line in spec.splitlines() if line.startswith("| G-6 |"))
    required = (
        "gh",
        "Review base:",
        "--dry-run",
        "three times",
        "raw diff",
        "review_integration_branches",
        "configured order",
        "arbitrary remote branches",
        "origin/head",
        "origin/main",
        "origin/master",
        "local `main` then `master`",
        "fails closed",
        "unresolved entries are named in a warning",
    )

    for name, text in (
        ("SPEC G-6", g6),
        ("README upgrade note", readme),
        ("user reference", user_reference),
    ):
        lowered = text.lower()
        for phrase in required:
            assert phrase.lower() in lowered, f"{name} does not declare {phrase!r}"
        assert (
            "current-cycle" in lowered
            or "most recently entered" in lowered
            or "latest entry into" in lowered
        ), f"{name} does not declare the review-evidence boundary"


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


def test_v2_declares_unknown_event_and_completion_warning_behavior() -> None:
    spec = (HOSTED / "SPEC.md").read_text()
    g6 = next(line for line in spec.splitlines() if line.startswith("| G-6 |"))
    readme = (REPO / "README.md").read_text()
    upgrade_note = readme.split("### upgrading to v2", 1)[1].split("\n## ", 1)[0]

    for declaration in (
        "Unknown task event types replay quietly on `show` and `list`",
        "`lattice doctor` reports the `unknown_event_type` finding",
        "materializing writes and rebuild warn once",
        "not during a completion path that has not yet reached `done`",
    ):
        assert declaration in g6

    for declaration in (
        "`lattice show` and `lattice list` replay them quietly",
        "`lattice doctor` reports an `unknown_event_type` finding",
        "writes and rebuilds that materialize unknown task events continue",
        "The completion path `pr_open -> review -> done` does not warn",
    ):
        assert declaration in upgrade_note


# Hosts the docs may name: placeholders, loopback, the documentation address
# ranges (RFC 5737), and public project links.
_ALLOWED_HOSTS = re.compile(
    r"^(localhost|127\.0\.0\.1|\[?::1\]?|0\.0\.0\.0"
    r"|(192\.0\.2|198\.51\.100|203\.0\.113)\.\d{1,3}"
    r"|([a-z0-9-]+\.)*example(\.(internal|com|org|net|invalid))?"
    r"|([a-z0-9-]+\.)*(invalid|test)"
    r"|github\.com|www\.apple\.com|claude\.com|docs\.anthropic\.com"
    # The README's public links: the project site (never a subdomain) and papers.
    r"|(www\.)?stage11\.ai|arxiv\.org)$"
)
_URL_HOST = re.compile(r"\b[a-z][a-z0-9+.-]*://(?:[^@/\s]+@)?([^/:\s\"'<>)`]+)", re.I)
_BARE_HOST = re.compile(
    r"(?<![\w./-])((?:[a-z0-9-]+\.)+(?:internal|com|net|org|io|dev|app|cloud|lan|local))\b",
    re.I,
)


HOST_SCANNED = [
    GUIDE,
    HOSTED / "SPEC.md",
    API,
    REPO / "docs" / "user-reference.md",
    *(DEPLOY / t for t in TEMPLATES),
    REPO / "README.md",
    REPO / "skills/lattice/SKILL.md",
    REPO / "src/lattice/skills/lattice/SKILL.md",
    *sorted((REPO / "docs/architecture").glob("*.md")),
]


@pytest.mark.parametrize(
    "path",
    [*HOST_SCANNED, None],
    ids=lambda p: str(p.relative_to(REPO)) if p else "claude_md_block",
)
def test_no_real_hostnames(path: Path | None) -> None:
    text = path.read_text() if path else CLAUDE_MD_BLOCK
    name = str(path.relative_to(REPO)) if path else "claude_md_block"
    hosts = {m.group(1).lower() for m in _URL_HOST.finditer(text)}
    hosts |= {m.group(1).lower() for m in _BARE_HOST.finditer(text)}
    real = sorted(h for h in hosts if not _ALLOWED_HOSTS.match(h))
    assert not real, f"{name} names non-placeholder hosts: {real}"


def test_both_lattice_skills_explain_remote_issue_filing() -> None:
    required = (
        "curl -sS -X POST",
        "/v1/projects/<slug>/ops/issue.file",
        "source_ref",
        "`deduplicated`",
        "docs/hosted/guide.md",
        "Filing-only issue token",
    )
    for path in (
        REPO / "skills/lattice/SKILL.md",
        REPO / "src/lattice/skills/lattice/SKILL.md",
    ):
        text = path.read_text()
        assert all(fragment in text for fragment in required), path


def test_complete_via_docs_declare_bundle_and_reachability_contract() -> None:
    reference = (REPO / "docs" / "user-reference.md").read_text()
    spec = (HOSTED / "SPEC.md").read_text()
    guide = GUIDE.read_text()

    assert "lattice complete LAT-382" in reference
    assert "--via LAT-381" in reference
    assert "canonical task or pull-request object" in reference
    assert "is an ancestor of that still-existing linked branch" in reference
    assert "before deleting the branch" in reference

    for path in (
        REPO / "skills/lattice/SKILL.md",
        REPO / "src/lattice/skills/lattice/SKILL.md",
    ):
        text = path.read_text()
        assert "--via" in text, path
        assert "primary task ID" in text, path
        assert "require_reachable_review_commit" in text, path
        assert "ancestor" in text and "still-existing branch" in text, path
        assert "A bundled task's branch is never borrowed" in text, path

    assert "`task.complete`" in spec
    assert "canonical task or pull-request object" in spec
    assert "UNSUPPORTED_PARAM" in spec
    assert "does not require a `min_client_version` bump" in spec
    assert "C0, C1, and DEL control characters are refused" in spec
    assert "C0, C1, and DEL control characters are refused" in guide


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


def _section(text: str, number: int) -> str:
    start = text.index(f"\n## {number}. ")
    return text[start : text.index(f"\n## {number + 1}. ", start)]


@pytest.mark.parametrize("track", ["yes", "no"])
def test_move_back_tracks_the_board_only_when_asked(tmp_path: Path, track: str) -> None:
    """Guide section 15, steps 4 and 5, as written, on a checkout in the state
    step 3 leaves: board copied in, untracked, and ignored. With
    ``TRACK_BOARD=yes`` the commit must carry the board; with ``no`` it must
    not (a ``git commit -a`` alone skips untracked files)."""
    blocks = _shell_blocks(_section(GUIDE.read_text(), 15))
    step4 = next(b for b in blocks if "TRACK_BOARD=no" in b)
    step5 = next(b for b in blocks if "Move the Lattice board back to local" in b)
    step4 = step4.replace("TRACK_BOARD=no", f"TRACK_BOARD={track}", 1)
    step5 = "\n".join(line for line in step5.splitlines() if not line.startswith("lattice "))

    repo = tmp_path / "app"
    env = {
        **os.environ,
        "HOME": str(tmp_path),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@example.invalid",
    }

    def sh(script: str) -> str:
        done = subprocess.run(
            ["bash", "-e", "-c", script],
            cwd=repo,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert done.returncode == 0, done.stderr
        return done.stdout

    repo.mkdir()
    sh(
        "git init -q && echo '/.lattice/' > .gitignore"
        " && echo '/.lattice/' >> \"$(git rev-parse --git-common-dir)/info/exclude\""
        " && echo '{}' > .lattice-remote.json && git add .gitignore .lattice-remote.json"
        " && git commit -q -m bound && git rm -q .lattice-remote.json"
        " && mkdir -p .lattice/tasks && echo '{}' > .lattice/config.json"
        " && echo '{}' > .lattice/tasks/task_01.json"
    )
    sh(step4)
    sh(step5)
    tracked = sh("git ls-files .lattice").split()
    if track == "yes":
        assert tracked == [".lattice/config.json", ".lattice/tasks/task_01.json"]
        assert "/.lattice/" not in (repo / ".gitignore").read_text()
    else:
        assert tracked == []
        assert "/.lattice/" in (repo / ".gitignore").read_text()
    assert sh("git status --porcelain") == ""


def _json_documents(text: str) -> list[dict]:
    """Every JSON value printed back to back (curl adds no newline between them)."""
    decoder = json.JSONDecoder()
    docs, pos = [], 0
    while (pos := len(text) - len(text[pos:].lstrip())) < len(text):
        doc, pos = decoder.raw_decode(text, pos)
        docs.append(doc)
    return docs


@pytest.mark.skipif(
    not all(shutil.which(tool) for tool in ("bash", "curl", "jq", "shasum")),
    reason="the guide's filing recipe needs bash, curl, jq and shasum",
)
def test_guide_filing_recipe_files_a_geotagged_photo_and_dedupes(tmp_path: Path) -> None:
    """Guide section 8's filing-token recipe, run verbatim against a loopback
    server with a phone-style JPEG. The server strips the photo's metadata
    before staging, so the recipe must file the hash the staging reply returns,
    not the hash of the file it uploaded (LAT-421)."""
    from lattice.server import admin, tokens
    from lattice.server.testing import make_root, running_server
    from tests.photo_metadata_helpers import assert_no_identifying_metadata, jpeg_with_gps

    recipe = next(b for b in _shell_blocks(_section(GUIDE.read_text(), 8)) if "/staging/" in b)
    root = make_root(
        tmp_path, projects={"demo": {"code": "DEMO"}}, config={"audit": {"enabled": False}}
    )
    admin.set_project_config(root, "demo", {"issues.enabled": True})
    filing = tokens.create_token(
        root,
        user="human:intake",
        machine="intake-worker",
        actors=("agent:intake-worker",),
        projects=("demo",),
        only=("issue.file",),
        source="reporter-links",
    )["token"]
    reader = tokens.create_token(root, user="human:alice", machine="laptop", all_projects=True)
    photo = jpeg_with_gps()
    work = tmp_path / "intake"
    work.mkdir()
    (work / "shot.jpg").write_bytes(photo)

    with running_server(root) as server:
        env = {**os.environ, "LATTICE_URL": server.url, "LATTICE_TOKEN": filing}
        env.pop("IMAGE", None)

        def run_recipe() -> list[dict]:
            done = subprocess.run(
                ["bash", "-c", recipe],
                cwd=work,
                env=env,
                capture_output=True,
                text=True,
                timeout=60,
            )
            assert done.returncode == 0, done.stderr
            return _json_documents(done.stdout)[-2:]  # the filing and its dedupe retry

        first, retry = run_recipe()
        assert first["ok"], first
        filed = first["data"]["result"]["value"]
        assert filed["deduplicated"] is False
        assert retry["ok"], retry
        assert retry["data"]["result"]["value"]["deduplicated"] is True
        assert retry["data"]["result"]["value"]["id"] == filed["id"]

        for receipt in run_recipe():  # a second run of the whole recipe
            assert receipt["ok"], receipt
            assert receipt["data"]["result"]["value"]["deduplicated"] is True
            assert receipt["data"]["result"]["value"]["id"] == filed["id"]

        status, _, issue = server.request(
            "GET", f"/v1/projects/demo/files/issues/{filed['id']}.json", token=reader["token"]
        )
        assert status == 200, issue
        assert len(issue["media"]) == 1, issue["media"]
        media = issue["media"][0]
        assert media["sha256"] != hashlib.sha256(photo).hexdigest()
        read = urllib.request.Request(
            f"{server.url}/v1/projects/demo/issues/media/{filed['id']}/{media['id']}",
            headers={"Authorization": f"Bearer {reader['token']}"},
        )
        with urllib.request.urlopen(read, timeout=30) as response:
            stored = response.read()
        assert hashlib.sha256(stored).hexdigest() == media["sha256"]
        assert_no_identifying_metadata(stored, "image/jpeg")
