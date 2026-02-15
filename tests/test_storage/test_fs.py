"""Tests for atomic write operations and JSONL append."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from lattice.storage.fs import _fsync_directory, atomic_write, jsonl_append


class TestAtomicWrite:
    """atomic_write() writes content safely via temp + fsync + rename."""

    def test_writes_expected_content(self, tmp_path: Path) -> None:
        target = tmp_path / "output.json"
        atomic_write(target, '{"key": "value"}\n')

        assert target.read_text() == '{"key": "value"}\n'

    def test_writes_bytes_content(self, tmp_path: Path) -> None:
        target = tmp_path / "output.bin"
        data = b"\x00\x01\x02\x03"
        atomic_write(target, data)

        assert target.read_bytes() == data

    def test_no_temp_file_left_after_success(self, tmp_path: Path) -> None:
        target = tmp_path / "output.json"
        atomic_write(target, "content\n")

        # Only the target file should exist
        files = list(tmp_path.iterdir())
        assert files == [target]

    def test_overwrites_existing_file(self, tmp_path: Path) -> None:
        target = tmp_path / "output.json"
        target.write_text("old content\n")

        atomic_write(target, "new content\n")
        assert target.read_text() == "new content\n"

    def test_parent_directory_must_exist(self, tmp_path: Path) -> None:
        target = tmp_path / "nonexistent" / "output.json"

        with pytest.raises(FileNotFoundError, match="Parent directory does not exist"):
            atomic_write(target, "content\n")

    def test_handles_short_writes(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """os.write() can return fewer bytes than requested; atomic_write must loop."""
        target = tmp_path / "output.bin"
        payload = b"ABCDEFGHIJ"  # 10 bytes

        real_write = os.write
        call_count = 0

        def short_write(fd: int, data: bytes | memoryview) -> int:
            nonlocal call_count
            call_count += 1
            # First call writes only half, subsequent calls write normally
            if call_count == 1:
                n = max(1, len(data) // 2)
                return real_write(fd, bytes(data[:n]))
            return real_write(fd, bytes(data))

        monkeypatch.setattr(os, "write", short_write)
        atomic_write(target, payload)

        assert target.read_bytes() == payload
        assert call_count >= 2, "Should have needed multiple os.write calls"

    def test_file_permissions_are_readable(self, tmp_path: Path) -> None:
        target = tmp_path / "output.json"
        atomic_write(target, "content\n")

        # File should be readable
        assert os.access(target, os.R_OK)


class TestJsonlAppend:
    """jsonl_append() appends a single newline-terminated line to a JSONL file."""

    def test_creates_file_if_not_exists(self, tmp_path: Path) -> None:
        target = tmp_path / "events.jsonl"
        assert not target.exists()

        jsonl_append(target, '{"event":"created"}\n')

        assert target.exists()
        assert target.read_text() == '{"event":"created"}\n'

    def test_appends_not_overwrites(self, tmp_path: Path) -> None:
        target = tmp_path / "events.jsonl"
        target.write_text('{"event":"first"}\n')

        jsonl_append(target, '{"event":"second"}\n')

        assert target.read_text() == '{"event":"first"}\n{"event":"second"}\n'

    def test_multiple_appends_accumulate(self, tmp_path: Path) -> None:
        target = tmp_path / "events.jsonl"
        lines = [
            '{"id":"ev_1","type":"task_created"}\n',
            '{"id":"ev_2","type":"status_changed"}\n',
            '{"id":"ev_3","type":"comment_added"}\n',
        ]
        for line in lines:
            jsonl_append(target, line)

        content = target.read_text()
        assert content == "".join(lines)
        # Each line should be independently parseable
        import json

        for raw in content.strip().split("\n"):
            json.loads(raw)  # Should not raise

    def test_line_ends_with_newline(self, tmp_path: Path) -> None:
        target = tmp_path / "events.jsonl"
        line = '{"ok":true}\n'
        jsonl_append(target, line)

        written = target.read_text()
        assert written.endswith("\n")
        assert written == line


class TestFsyncDirectory:
    """_fsync_directory() syncs directory metadata for durability."""

    def test_called_during_atomic_write(self, tmp_path: Path) -> None:
        """atomic_write should call _fsync_directory on the parent dir."""
        target = tmp_path / "output.json"
        with patch("lattice.storage.fs._fsync_directory") as mock_fsync:
            atomic_write(target, "content\n")
        mock_fsync.assert_called_once_with(tmp_path)

    def test_called_during_jsonl_append(self, tmp_path: Path) -> None:
        """jsonl_append should call _fsync_directory on the parent dir."""
        target = tmp_path / "events.jsonl"
        with patch("lattice.storage.fs._fsync_directory") as mock_fsync:
            jsonl_append(target, '{"ok":true}\n')
        mock_fsync.assert_called_once_with(tmp_path)

    def test_does_not_raise_on_oserror(self, tmp_path: Path) -> None:
        """_fsync_directory should silently ignore OSError (e.g. macOS)."""
        with patch("lattice.storage.fs.os.open", side_effect=OSError("not supported")):
            _fsync_directory(tmp_path)  # Should not raise
