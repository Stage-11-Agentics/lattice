"""Tests for caller-supplied IDs, idempotent retries, and conflict detection."""

from __future__ import annotations

import json
from pathlib import Path

from ulid import ULID


class TestCreateIdempotency:
    """Idempotent create with caller-supplied --id."""

    def test_create_same_id_same_payload_succeeds(self, invoke, initialized_root):
        """Creating with the same --id and identical payload returns success both times."""
        task_id = f"task_{ULID()}"

        r1 = invoke("create", "Test Task", "--id", task_id, "--actor", "human:test")
        assert r1.exit_code == 0

        r2 = invoke("create", "Test Task", "--id", task_id, "--actor", "human:test")
        assert r2.exit_code == 0
        assert "idempotent" in r2.output.lower()

    def test_create_same_id_same_payload_single_event(self, invoke, initialized_root):
        """Idempotent retry does not append a second event to the JSONL log."""
        task_id = f"task_{ULID()}"

        invoke("create", "Test Task", "--id", task_id, "--actor", "human:test")
        invoke("create", "Test Task", "--id", task_id, "--actor", "human:test")

        event_file = initialized_root / ".lattice" / "events" / f"{task_id}.jsonl"
        lines = [line for line in event_file.read_text().splitlines() if line.strip()]
        assert len(lines) == 1, f"Expected 1 event line, got {len(lines)}"

    def test_create_same_id_different_title_conflicts(self, invoke, initialized_root):
        """Different title with the same --id produces a CONFLICT error."""
        task_id = f"task_{ULID()}"

        r1 = invoke("create", "Title A", "--id", task_id, "--actor", "human:test", "--json")
        assert r1.exit_code == 0

        r2 = invoke("create", "Title B", "--id", task_id, "--actor", "human:test", "--json")
        assert r2.exit_code != 0
        d2 = json.loads(r2.output)
        assert d2["ok"] is False
        assert d2["error"]["code"] == "CONFLICT"

    def test_create_same_id_different_priority_conflicts(self, invoke, initialized_root):
        """Different priority with the same --id produces a CONFLICT error."""
        task_id = f"task_{ULID()}"

        r1 = invoke(
            "create",
            "Task",
            "--id",
            task_id,
            "--priority",
            "high",
            "--actor",
            "human:test",
            "--json",
        )
        assert r1.exit_code == 0

        r2 = invoke(
            "create",
            "Task",
            "--id",
            task_id,
            "--priority",
            "low",
            "--actor",
            "human:test",
            "--json",
        )
        assert r2.exit_code != 0
        d2 = json.loads(r2.output)
        assert d2["ok"] is False
        assert d2["error"]["code"] == "CONFLICT"

    def test_create_same_id_different_tags_conflicts(self, invoke, initialized_root):
        """Different tags with the same --id produces a CONFLICT error."""
        task_id = f"task_{ULID()}"

        r1 = invoke(
            "create",
            "Task",
            "--id",
            task_id,
            "--tags",
            "a,b",
            "--actor",
            "human:test",
            "--json",
        )
        assert r1.exit_code == 0

        r2 = invoke(
            "create",
            "Task",
            "--id",
            task_id,
            "--tags",
            "c,d",
            "--actor",
            "human:test",
            "--json",
        )
        assert r2.exit_code != 0
        d2 = json.loads(r2.output)
        assert d2["ok"] is False
        assert d2["error"]["code"] == "CONFLICT"

    def test_conflict_error_in_json_envelope(self, invoke, initialized_root):
        """CONFLICT response has the correct JSON envelope structure."""
        task_id = f"task_{ULID()}"

        invoke("create", "Original", "--id", task_id, "--actor", "human:test")

        r = invoke("create", "Different", "--id", task_id, "--actor", "human:test", "--json")
        assert r.exit_code != 0
        envelope = json.loads(r.output)
        assert envelope["ok"] is False
        assert "error" in envelope
        assert envelope["error"]["code"] == "CONFLICT"
        assert isinstance(envelope["error"]["message"], str)
        assert len(envelope["error"]["message"]) > 0

    def test_idempotent_retry_returns_same_task_id(self, invoke, initialized_root):
        """Both idempotent calls return the same task ID in the JSON data."""
        task_id = f"task_{ULID()}"

        r1 = invoke("create", "Task", "--id", task_id, "--actor", "human:test", "--json")
        assert r1.exit_code == 0
        d1 = json.loads(r1.output)
        assert d1["ok"] is True
        assert d1["data"]["id"] == task_id

        r2 = invoke("create", "Task", "--id", task_id, "--actor", "human:test", "--json")
        assert r2.exit_code == 0
        d2 = json.loads(r2.output)
        assert d2["ok"] is True
        assert d2["data"]["id"] == task_id


class TestCreateInvalidId:
    """Tests for invalid caller-supplied IDs on create."""

    def test_create_with_invalid_id_format(self, invoke, initialized_root):
        """A completely invalid ID format returns INVALID_ID."""
        r = invoke("create", "Task", "--id", "not_valid", "--actor", "human:test", "--json")
        assert r.exit_code != 0
        d = json.loads(r.output)
        assert d["ok"] is False
        assert d["error"]["code"] == "INVALID_ID"

    def test_create_with_wrong_prefix_id(self, invoke, initialized_root):
        """An ID with the wrong prefix (ev_ instead of task_) returns INVALID_ID."""
        wrong_id = f"ev_{ULID()}"
        r = invoke("create", "Task", "--id", wrong_id, "--actor", "human:test", "--json")
        assert r.exit_code != 0
        d = json.loads(r.output)
        assert d["ok"] is False
        assert d["error"]["code"] == "INVALID_ID"


class TestAttachIdempotency:
    """Idempotent attach with caller-supplied --id."""

    def test_attach_same_id_same_file_idempotent(self, invoke, create_task, initialized_root):
        """Attaching the same file with the same --id twice succeeds idempotently."""
        import tempfile

        task = create_task("Test Task")
        task_id = task["id"]
        art_id = f"art_{ULID()}"

        # Use a separate temp dir so the file lives outside initialized_root
        with tempfile.TemporaryDirectory() as td:
            src = Path(td) / "test.txt"
            src.write_text("hello")

            r1 = invoke(
                "attach",
                task_id,
                str(src),
                "--id",
                art_id,
                "--actor",
                "human:test",
                "--json",
            )
            assert r1.exit_code == 0
            d1 = json.loads(r1.output)
            assert d1["ok"] is True

            r2 = invoke(
                "attach",
                task_id,
                str(src),
                "--id",
                art_id,
                "--actor",
                "human:test",
                "--json",
            )
            assert r2.exit_code == 0
            d2 = json.loads(r2.output)
            assert d2["ok"] is True
            assert d2["data"]["id"] == art_id

    def test_attach_same_id_different_file_conflicts(self, invoke, create_task, initialized_root):
        """Attaching a different file with the same --id produces a CONFLICT."""
        import tempfile

        task = create_task("Test Task")
        task_id = task["id"]
        art_id = f"art_{ULID()}"

        with tempfile.TemporaryDirectory() as td:
            file_a = Path(td) / "a.txt"
            file_a.write_text("content a")

            file_b = Path(td) / "b.txt"
            file_b.write_text("content b")

            r1 = invoke(
                "attach",
                task_id,
                str(file_a),
                "--id",
                art_id,
                "--actor",
                "human:test",
                "--json",
            )
            assert r1.exit_code == 0

            r2 = invoke(
                "attach",
                task_id,
                str(file_b),
                "--id",
                art_id,
                "--actor",
                "human:test",
                "--json",
            )
            assert r2.exit_code != 0
            d2 = json.loads(r2.output)
            assert d2["ok"] is False
            assert d2["error"]["code"] == "CONFLICT"
