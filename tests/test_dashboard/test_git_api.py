"""Tests for git integration API endpoints (/api/git, /api/git/branches/*/commits).

Tests cover:
- git_reader module: extract_task_refs, caching, availability detection
- Dashboard server: routing, ETag/304, graceful degradation
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import threading
import time
from pathlib import Path
from unittest.mock import patch
from urllib.parse import quote
from urllib.request import Request, urlopen

import pytest

from lattice.core.config import default_config, serialize_config
from lattice.dashboard.git_reader import (
    CACHE_TTL_SECONDS,
    _prune_expired_cache,
    _summary_cache,
    _validate_branch_name,
    extract_task_refs,
    find_git_root,
    get_branches,
    get_commit_count,
    get_current_branch,
    get_git_summary,
    get_recent_commits,
    get_remote_url,
    git_available,
    invalidate_cache,
)
from lattice.dashboard.server import create_server
from lattice.storage.fs import atomic_write, ensure_lattice_dirs


# ---------------------------------------------------------------------------
# HTTP helpers (reused pattern from test_graph_api.py)
# ---------------------------------------------------------------------------


def _get(
    base_url: str, path: str, *, headers: dict[str, str] | None = None
) -> tuple[int, dict | str, dict[str, str]]:
    """Make a GET request and return (status_code, parsed_body, response_headers)."""
    req = Request(f"{base_url}{path}")
    if headers:
        for k, v in headers.items():
            req.add_header(k, v)
    try:
        with urlopen(req) as resp:
            body = resp.read().decode("utf-8")
            content_type = resp.headers.get("Content-Type", "")
            resp_headers = {k: v for k, v in resp.getheaders()}
            if "application/json" in content_type:
                return resp.status, json.loads(body), resp_headers
            return resp.status, body, resp_headers
    except Exception as exc:
        if hasattr(exc, "code"):
            body = exc.read().decode("utf-8")  # type: ignore[union-attr]
            content_type = exc.headers.get("Content-Type", "")  # type: ignore[union-attr]
            resp_headers = {k: v for k, v in exc.headers.items()}  # type: ignore[union-attr]
            if "application/json" in content_type:
                return exc.code, json.loads(body), resp_headers  # type: ignore[union-attr]
            return exc.code, body, resp_headers  # type: ignore[union-attr]
        raise


def _get_free_port() -> int:
    """Find an available TCP port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start_server(lattice_dir: Path) -> tuple[str, object]:
    """Start a dashboard server on a random port and return (base_url, server)."""
    port = _get_free_port()
    host = "127.0.0.1"
    server = create_server(lattice_dir, host, port)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
    )
    thread.start()
    return f"http://{host}:{port}", server


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_git_cache():
    """Ensure a clean cache for every test."""
    invalidate_cache()
    yield
    invalidate_cache()


@pytest.fixture()
def git_repo(tmp_path: Path) -> Path:
    """Create a real git repository with a few commits and return the repo root.

    The repo has:
    - An initial commit on main
    - A feature branch with a commit referencing LAT-42
    - The main branch checked out at the end
    """
    repo = tmp_path / "repo"
    repo.mkdir()

    # Initialize the repo
    subprocess.run(
        ["git", "init", "--initial-branch=main"], cwd=str(repo), check=True, capture_output=True
    )
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test Author"],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )

    # First commit on main
    (repo / "README.md").write_text("# Test Project\n")
    subprocess.run(["git", "add", "README.md"], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "Initial commit"],
        cwd=str(repo),
        check=True,
        capture_output=True,
        env={**os.environ, "GIT_COMMITTER_DATE": "2025-01-10T10:00:00+00:00"},
    )

    # Second commit on main referencing a task
    (repo / "main.py").write_text("print('hello')\n")
    subprocess.run(["git", "add", "main.py"], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "feat: add main entry point (LAT-1)"],
        cwd=str(repo),
        check=True,
        capture_output=True,
        env={**os.environ, "GIT_COMMITTER_DATE": "2025-01-10T11:00:00+00:00"},
    )

    # Create a feature branch with a commit
    subprocess.run(
        ["git", "checkout", "-b", "feat/LAT-42-login"],
        cwd=str(repo),
        check=True,
        capture_output=True,
    )
    (repo / "login.py").write_text("# login module\n")
    subprocess.run(["git", "add", "login.py"], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "feat: implement login flow (LAT-42)"],
        cwd=str(repo),
        check=True,
        capture_output=True,
        env={**os.environ, "GIT_COMMITTER_DATE": "2025-01-10T12:00:00+00:00"},
    )

    # Back to main
    subprocess.run(["git", "checkout", "main"], cwd=str(repo), check=True, capture_output=True)

    return repo


@pytest.fixture()
def lattice_in_git_repo(git_repo: Path) -> Path:
    """Initialize a .lattice/ dir inside the git repo and return the lattice_dir path."""
    ensure_lattice_dirs(git_repo)
    ld = git_repo / ".lattice"
    atomic_write(ld / "config.json", serialize_config(default_config()))
    (ld / "events" / "_lifecycle.jsonl").touch()
    return ld


# ---------------------------------------------------------------------------
# Unit tests: extract_task_refs
# ---------------------------------------------------------------------------


class TestExtractTaskRefs:
    def test_short_id(self):
        refs = extract_task_refs("Fix login issue LAT-42")
        assert refs == ["LAT-42"]

    def test_multiple_short_ids(self):
        refs = extract_task_refs("Closes LAT-42 and PROJ-7")
        assert refs == ["LAT-42", "PROJ-7"]

    def test_ulid_ref(self):
        refs = extract_task_refs("Related to task_01KHM2ZCQ0VR86N10ZJ1ES8G4C")
        assert refs == ["task_01KHM2ZCQ0VR86N10ZJ1ES8G4C"]

    def test_mixed_refs(self):
        refs = extract_task_refs("LAT-42 see also task_01KHM2ZCQ0VR86N10ZJ1ES8G4C")
        assert refs == ["LAT-42", "task_01KHM2ZCQ0VR86N10ZJ1ES8G4C"]

    def test_no_refs(self):
        refs = extract_task_refs("Just a regular commit message")
        assert refs == []

    def test_deduplication(self):
        refs = extract_task_refs("LAT-42 and again LAT-42")
        assert refs == ["LAT-42"]

    def test_empty_string(self):
        refs = extract_task_refs("")
        assert refs == []


# ---------------------------------------------------------------------------
# Unit tests: git_reader functions with real repos
# ---------------------------------------------------------------------------


class TestGitAvailable:
    def test_git_is_available(self):
        # git should be available in CI and dev environments
        assert git_available() is True

    def test_git_not_available(self):
        with patch("lattice.dashboard.git_reader.shutil.which", return_value=None):
            assert git_available() is False


class TestFindGitRoot:
    def test_finds_root(self, git_repo: Path):
        root = find_git_root(git_repo)
        assert root is not None
        assert root == git_repo

    def test_finds_root_from_subdir(self, git_repo: Path):
        subdir = git_repo / "subdir"
        subdir.mkdir()
        root = find_git_root(subdir)
        assert root is not None
        assert root == git_repo

    def test_not_a_repo(self, tmp_path: Path):
        root = find_git_root(tmp_path)
        assert root is None


class TestGetCurrentBranch:
    def test_returns_main(self, git_repo: Path):
        branch = get_current_branch(git_repo)
        assert branch == "main"

    def test_returns_feature_branch(self, git_repo: Path):
        subprocess.run(
            ["git", "checkout", "feat/LAT-42-login"],
            cwd=str(git_repo),
            check=True,
            capture_output=True,
        )
        branch = get_current_branch(git_repo)
        assert branch == "feat/LAT-42-login"


class TestGetBranches:
    def test_lists_branches(self, git_repo: Path):
        branches = get_branches(git_repo)
        names = [b["name"] for b in branches]
        assert "main" in names
        assert "feat/LAT-42-login" in names

    def test_branch_fields(self, git_repo: Path):
        branches = get_branches(git_repo)
        for b in branches:
            assert "name" in b
            assert "is_current" in b
            assert "commit_hash" in b
            assert "commit_subject" in b
            assert "commit_date" in b
            assert "author_name" in b
            assert "author_email" in b

    def test_current_branch_marked(self, git_repo: Path):
        branches = get_branches(git_repo)
        current = [b for b in branches if b["is_current"]]
        assert len(current) == 1
        assert current[0]["name"] == "main"


class TestGetRecentCommits:
    def test_returns_commits(self, git_repo: Path):
        commits = get_recent_commits(git_repo, "main")
        assert len(commits) == 2  # Initial commit + feat commit

    def test_commit_fields(self, git_repo: Path):
        commits = get_recent_commits(git_repo, "main")
        for c in commits:
            assert "hash" in c
            assert "short_hash" in c
            assert "subject" in c
            assert "author_name" in c
            assert "author_email" in c
            assert "date" in c
            assert "task_refs" in c

    def test_extracts_task_refs_from_commits(self, git_repo: Path):
        commits = get_recent_commits(git_repo, "main")
        # The second commit on main references LAT-1
        subjects = {c["subject"]: c for c in commits}
        lat1_commit = subjects.get("feat: add main entry point (LAT-1)")
        assert lat1_commit is not None
        assert "LAT-1" in lat1_commit["task_refs"]

    def test_feature_branch_commits(self, git_repo: Path):
        commits = get_recent_commits(git_repo, "feat/LAT-42-login")
        # Feature branch has 3 commits (2 from main + 1 feature)
        assert len(commits) == 3
        # Most recent commit should reference LAT-42
        assert "LAT-42" in commits[0]["task_refs"]

    def test_nonexistent_branch(self, git_repo: Path):
        commits = get_recent_commits(git_repo, "nonexistent-branch")
        assert commits == []

    def test_limit(self, git_repo: Path):
        commits = get_recent_commits(git_repo, "main", limit=1)
        assert len(commits) == 1


class TestGetCommitCount:
    def test_count(self, git_repo: Path):
        count = get_commit_count(git_repo)
        assert count == 2  # 2 commits on main


class TestGetRemoteUrl:
    def test_no_remote(self, git_repo: Path):
        url = get_remote_url(git_repo)
        assert url is None

    def test_with_remote(self, git_repo: Path):
        subprocess.run(
            ["git", "remote", "add", "origin", "https://github.com/example/repo.git"],
            cwd=str(git_repo),
            check=True,
            capture_output=True,
        )
        url = get_remote_url(git_repo)
        assert url == "https://github.com/example/repo.git"


# ---------------------------------------------------------------------------
# Unit tests: get_git_summary
# ---------------------------------------------------------------------------


class TestGetGitSummary:
    def test_summary_available(self, lattice_in_git_repo: Path):
        summary, etag = get_git_summary(lattice_in_git_repo)
        assert summary["available"] is True
        assert summary["current_branch"] == "main"
        assert summary["branch_count"] == 2
        assert summary["commit_count"] == 2
        assert isinstance(summary["branches"], list)
        assert etag != ""

    def test_summary_not_a_repo(self, tmp_path: Path):
        ensure_lattice_dirs(tmp_path)
        ld = tmp_path / ".lattice"
        atomic_write(ld / "config.json", serialize_config(default_config()))

        summary, etag = get_git_summary(ld)
        assert summary["available"] is False
        assert summary["reason"] == "not_a_git_repo"
        assert etag == ""

    def test_summary_git_not_installed(self, lattice_in_git_repo: Path):
        with patch("lattice.dashboard.git_reader.git_available", return_value=False):
            summary, etag = get_git_summary(lattice_in_git_repo)
            assert summary["available"] is False
            assert summary["reason"] == "git_not_installed"
            assert etag == ""

    def test_summary_caching(self, lattice_in_git_repo: Path):
        summary1, etag1 = get_git_summary(lattice_in_git_repo)
        summary2, etag2 = get_git_summary(lattice_in_git_repo)
        # Should be the exact same object (from cache)
        assert summary1 is summary2
        assert etag1 == etag2

    def test_cache_invalidation(self, lattice_in_git_repo: Path):
        summary1, _ = get_git_summary(lattice_in_git_repo)
        invalidate_cache()
        summary2, _ = get_git_summary(lattice_in_git_repo)
        # After invalidation, should be a fresh object (different identity)
        assert summary1 is not summary2
        # But structurally equivalent
        assert summary1["current_branch"] == summary2["current_branch"]


# ---------------------------------------------------------------------------
# Integration tests: /api/git endpoint via HTTP
# ---------------------------------------------------------------------------


class TestGitApiEndpoint:
    def test_git_summary_200(self, lattice_in_git_repo: Path):
        base_url, server = _start_server(lattice_in_git_repo)
        try:
            status, body, hdrs = _get(base_url, "/api/git")
            assert status == 200
            assert body["ok"] is True

            data = body["data"]
            assert data["available"] is True
            assert data["current_branch"] == "main"
            assert isinstance(data["branches"], list)
            assert data["branch_count"] == 2
        finally:
            server.shutdown()
            server.server_close()

    def test_git_summary_etag(self, lattice_in_git_repo: Path):
        base_url, server = _start_server(lattice_in_git_repo)
        try:
            # First request — get the ETag
            status1, body1, hdrs1 = _get(base_url, "/api/git")
            assert status1 == 200
            etag = hdrs1.get("ETag")
            assert etag is not None
            assert etag.startswith('"') and etag.endswith('"')

            # Second request with If-None-Match — should get 304
            status2, body2, hdrs2 = _get(base_url, "/api/git", headers={"If-None-Match": etag})
            assert status2 == 304
            assert hdrs2.get("ETag") == etag
        finally:
            server.shutdown()
            server.server_close()

    def test_git_summary_etag_mismatch(self, lattice_in_git_repo: Path):
        base_url, server = _start_server(lattice_in_git_repo)
        try:
            status, body, hdrs = _get(base_url, "/api/git", headers={"If-None-Match": '"wrong"'})
            assert status == 200
            assert body["ok"] is True
            assert body["data"]["available"] is True
        finally:
            server.shutdown()
            server.server_close()

    def test_git_summary_not_a_repo(self, tmp_path: Path):
        """When .lattice/ is not inside a git repo, return available=False."""
        ensure_lattice_dirs(tmp_path)
        ld = tmp_path / ".lattice"
        atomic_write(ld / "config.json", serialize_config(default_config()))

        base_url, server = _start_server(ld)
        try:
            status, body, _hdrs = _get(base_url, "/api/git")
            assert status == 200
            assert body["ok"] is True
            assert body["data"]["available"] is False
            assert body["data"]["reason"] == "not_a_git_repo"
        finally:
            server.shutdown()
            server.server_close()


# ---------------------------------------------------------------------------
# Integration tests: /api/git/branches/<name>/commits endpoint via HTTP
# ---------------------------------------------------------------------------


class TestGitBranchCommitsEndpoint:
    def test_branch_commits(self, lattice_in_git_repo: Path):
        base_url, server = _start_server(lattice_in_git_repo)
        try:
            status, body, _hdrs = _get(base_url, "/api/git/branches/main/commits")
            assert status == 200
            assert body["ok"] is True

            data = body["data"]
            assert data["branch"] == "main"
            assert data["count"] == 2
            assert isinstance(data["commits"], list)
            assert len(data["commits"]) == 2
        finally:
            server.shutdown()
            server.server_close()

    def test_branch_commits_with_slash(self, lattice_in_git_repo: Path):
        """Branch names with slashes must be URL-encoded."""
        base_url, server = _start_server(lattice_in_git_repo)
        try:
            # URL-encode the branch name: feat/LAT-42-login -> feat%2FLAT-42-login
            encoded = quote("feat/LAT-42-login", safe="")
            status, body, _hdrs = _get(base_url, f"/api/git/branches/{encoded}/commits")
            assert status == 200
            assert body["ok"] is True
            assert body["data"]["branch"] == "feat/LAT-42-login"
            assert body["data"]["count"] == 3  # 2 from main + 1 feature
        finally:
            server.shutdown()
            server.server_close()

    def test_branch_commits_nonexistent(self, lattice_in_git_repo: Path):
        """Requesting commits for a nonexistent branch returns empty list."""
        base_url, server = _start_server(lattice_in_git_repo)
        try:
            status, body, _hdrs = _get(base_url, "/api/git/branches/nonexistent/commits")
            assert status == 200
            assert body["ok"] is True
            assert body["data"]["commits"] == []
            assert body["data"]["count"] == 0
        finally:
            server.shutdown()
            server.server_close()

    def test_branch_commits_not_a_repo(self, tmp_path: Path):
        """When not in a git repo, return available=False."""
        ensure_lattice_dirs(tmp_path)
        ld = tmp_path / ".lattice"
        atomic_write(ld / "config.json", serialize_config(default_config()))

        base_url, server = _start_server(ld)
        try:
            status, body, _hdrs = _get(base_url, "/api/git/branches/main/commits")
            assert status == 200
            assert body["ok"] is True
            assert body["data"]["available"] is False
            assert body["data"]["reason"] == "not_a_git_repo"
        finally:
            server.shutdown()
            server.server_close()

    def test_branch_commits_task_refs(self, lattice_in_git_repo: Path):
        """Commits with task references should have them extracted."""
        base_url, server = _start_server(lattice_in_git_repo)
        try:
            encoded = quote("feat/LAT-42-login", safe="")
            status, body, _hdrs = _get(base_url, f"/api/git/branches/{encoded}/commits")
            assert status == 200
            commits = body["data"]["commits"]
            # The first (most recent) commit should reference LAT-42
            assert "LAT-42" in commits[0]["task_refs"]
        finally:
            server.shutdown()
            server.server_close()


# ---------------------------------------------------------------------------
# Routing tests: unknown paths under /api/git
# ---------------------------------------------------------------------------


class TestGitApiRouting:
    def test_unknown_git_subpath(self, lattice_in_git_repo: Path):
        base_url, server = _start_server(lattice_in_git_repo)
        try:
            status, body, _hdrs = _get(base_url, "/api/git/branches/main/unknown")
            assert status == 404
        finally:
            server.shutdown()
            server.server_close()


# ---------------------------------------------------------------------------
# Tests for code review fixes
# ---------------------------------------------------------------------------


class TestETagIncludesCurrentBranch:
    """ETag must change when the checked-out branch changes, even if branch
    tips stay the same."""

    def test_etag_changes_on_branch_switch(self, lattice_in_git_repo: Path, git_repo: Path):
        # Get ETag on main
        summary1, etag1 = get_git_summary(lattice_in_git_repo)
        assert summary1["current_branch"] == "main"
        assert etag1 != ""

        # Switch to feature branch (tips unchanged)
        invalidate_cache()
        subprocess.run(
            ["git", "checkout", "feat/LAT-42-login"],
            cwd=str(git_repo),
            check=True,
            capture_output=True,
        )

        summary2, etag2 = get_git_summary(lattice_in_git_repo)
        assert summary2["current_branch"] == "feat/LAT-42-login"

        # ETags MUST differ because current_branch changed
        assert etag1 != etag2

    def test_etag_changes_on_remote_url_change(self, lattice_in_git_repo: Path, git_repo: Path):
        summary1, etag1 = get_git_summary(lattice_in_git_repo)
        invalidate_cache()

        # Add a remote
        subprocess.run(
            ["git", "remote", "add", "origin", "https://github.com/example/repo.git"],
            cwd=str(git_repo),
            check=True,
            capture_output=True,
        )

        summary2, etag2 = get_git_summary(lattice_in_git_repo)
        assert etag1 != etag2


class TestBranchNameInjection:
    """Branch names starting with '-' must be rejected to prevent git argument
    injection."""

    def test_validate_branch_name_normal(self):
        assert _validate_branch_name("main") is True
        assert _validate_branch_name("feat/foo") is True

    def test_validate_branch_name_rejects_flags(self):
        assert _validate_branch_name("--all") is False
        assert _validate_branch_name("-v") is False
        assert _validate_branch_name("--exec=whoami") is False

    def test_validate_branch_name_rejects_empty(self):
        assert _validate_branch_name("") is False

    def test_get_recent_commits_rejects_flag(self, git_repo: Path):
        """get_recent_commits returns [] for flag-like branch names."""
        commits = get_recent_commits(git_repo, "--all")
        assert commits == []

    def test_server_rejects_flag_branch(self, lattice_in_git_repo: Path):
        """The HTTP endpoint returns 400 for flag-like branch names."""
        base_url, server = _start_server(lattice_in_git_repo)
        try:
            status, body, _hdrs = _get(base_url, "/api/git/branches/--all/commits")
            assert status == 400
            assert body["ok"] is False
            assert "VALIDATION_ERROR" in body["error"]["code"]
        finally:
            server.shutdown()
            server.server_close()

    def test_server_rejects_dash_v_branch(self, lattice_in_git_repo: Path):
        base_url, server = _start_server(lattice_in_git_repo)
        try:
            status, body, _hdrs = _get(base_url, "/api/git/branches/-v/commits")
            assert status == 400
            assert body["ok"] is False
        finally:
            server.shutdown()
            server.server_close()


class TestCacheEviction:
    """Expired entries must be pruned from the cache to prevent unbounded growth."""

    def test_prune_removes_expired_entries(self):
        """Manually insert an expired cache entry and verify it gets pruned."""
        invalidate_cache()

        # Insert a cache entry with a timestamp well in the past
        old_time = time.monotonic() - CACHE_TTL_SECONDS - 100
        _summary_cache["/fake/repo1"] = (old_time, "etag1", {"available": True})
        _summary_cache["/fake/repo2"] = (old_time, "etag2", {"available": True})

        # Insert a fresh entry
        fresh_time = time.monotonic()
        _summary_cache["/fake/repo3"] = (fresh_time, "etag3", {"available": True})

        assert len(_summary_cache) == 3

        _prune_expired_cache()

        # Expired entries removed, fresh entry kept
        assert "/fake/repo1" not in _summary_cache
        assert "/fake/repo2" not in _summary_cache
        assert "/fake/repo3" in _summary_cache
        assert len(_summary_cache) == 1

    def test_get_git_summary_prunes_on_access(self, lattice_in_git_repo: Path):
        """get_git_summary should prune stale entries from other repos."""
        invalidate_cache()

        # Plant a stale entry for a different repo
        old_time = time.monotonic() - CACHE_TTL_SECONDS - 100
        _summary_cache["/stale/repo"] = (old_time, "old_etag", {"available": True})

        # Call get_git_summary, which should prune the stale entry
        get_git_summary(lattice_in_git_repo)

        assert "/stale/repo" not in _summary_cache
