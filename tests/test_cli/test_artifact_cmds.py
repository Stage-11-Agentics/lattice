"""Tests for the `lattice attach` CLI command."""

from __future__ import annotations

import json

from lattice.storage.fs import LATTICE_DIR


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_ACTOR = "human:test"


# ---------------------------------------------------------------------------
# Attach file
# ---------------------------------------------------------------------------


class TestAttachFile:
    def test_attach_file_success(self, invoke, initialized_root, tmp_path) -> None:
        # Create a task
        r = invoke("create", "My task", "--actor", _ACTOR, "--json")
        assert r.exit_code == 0, r.output
        task_id = json.loads(r.output)["data"]["id"]

        # Create a file to attach
        src_file = tmp_path / "debug.log"
        src_file.write_text("debug output")

        # Attach it
        result = invoke("attach", task_id, str(src_file), "--actor", _ACTOR)
        assert result.exit_code == 0
        assert "Attached artifact" in result.output

    def test_metadata_created(self, invoke, initialized_root, tmp_path) -> None:
        r = invoke("create", "Task", "--actor", _ACTOR, "--json")
        task_id = json.loads(r.output)["data"]["id"]

        src_file = tmp_path / "notes.txt"
        src_file.write_text("some notes")

        result = invoke("attach", task_id, str(src_file), "--actor", _ACTOR, "--json")
        assert result.exit_code == 0
        data = json.loads(result.output)["data"]

        # Verify metadata fields
        assert data["type"] == "file"
        assert data["title"] == "notes.txt"
        assert data["created_by"] == _ACTOR
        assert data["schema_version"] == 1
        assert data["sensitive"] is False

        # Verify payload
        assert data["payload"]["file"] is not None
        assert data["payload"]["content_type"] == "text/plain"
        assert data["payload"]["size_bytes"] == len("some notes")

        # Verify metadata file on disk
        art_id = data["id"]
        lattice_dir = initialized_root / LATTICE_DIR
        meta_path = lattice_dir / "artifacts" / "meta" / f"{art_id}.json"
        assert meta_path.exists()

    def test_payload_copied(self, invoke, initialized_root, tmp_path) -> None:
        r = invoke("create", "Task", "--actor", _ACTOR, "--json")
        task_id = json.loads(r.output)["data"]["id"]

        src_file = tmp_path / "data.csv"
        src_file.write_text("col1,col2\na,b\n")

        result = invoke("attach", task_id, str(src_file), "--actor", _ACTOR, "--json")
        assert result.exit_code == 0
        data = json.loads(result.output)["data"]
        art_id = data["id"]

        # Verify payload file was copied
        lattice_dir = initialized_root / LATTICE_DIR
        payload_path = lattice_dir / "artifacts" / "payload" / f"{art_id}.csv"
        assert payload_path.exists()
        assert payload_path.read_text() == "col1,col2\na,b\n"

    def test_event_appended(self, invoke, initialized_root, tmp_path) -> None:
        r = invoke("create", "Task", "--actor", _ACTOR, "--json")
        task_id = json.loads(r.output)["data"]["id"]

        src_file = tmp_path / "log.txt"
        src_file.write_text("log data")

        invoke("attach", task_id, str(src_file), "--actor", _ACTOR, "--json")

        # Check event log
        lattice_dir = initialized_root / LATTICE_DIR
        event_path = lattice_dir / "events" / f"{task_id}.jsonl"
        lines = event_path.read_text().strip().split("\n")
        # First event is task_created, second is artifact_attached
        assert len(lines) == 2
        attach_event = json.loads(lines[1])
        assert attach_event["type"] == "artifact_attached"
        assert "artifact_id" in attach_event["data"]

    def test_task_snapshot_updated(self, invoke, initialized_root, tmp_path) -> None:
        r = invoke("create", "Task", "--actor", _ACTOR, "--json")
        task_id = json.loads(r.output)["data"]["id"]

        src_file = tmp_path / "file.txt"
        src_file.write_text("content")

        result = invoke("attach", task_id, str(src_file), "--actor", _ACTOR, "--json")
        art_id = json.loads(result.output)["data"]["id"]

        # Read task snapshot — artifact_refs now stores enriched dicts
        lattice_dir = initialized_root / LATTICE_DIR
        snap = json.loads((lattice_dir / "tasks" / f"{task_id}.json").read_text())
        ref_ids = [r["id"] if isinstance(r, dict) else r for r in snap["artifact_refs"]]
        assert art_id in ref_ids

    def test_attach_with_title(self, invoke, initialized_root, tmp_path) -> None:
        r = invoke("create", "Task", "--actor", _ACTOR, "--json")
        task_id = json.loads(r.output)["data"]["id"]

        src_file = tmp_path / "data.bin"
        src_file.write_bytes(b"\x00\x01\x02")

        result = invoke(
            "attach",
            task_id,
            str(src_file),
            "--title",
            "Binary dump",
            "--actor",
            _ACTOR,
            "--json",
        )
        assert result.exit_code == 0
        data = json.loads(result.output)["data"]
        assert data["title"] == "Binary dump"

    def test_attach_with_role(self, invoke, initialized_root, tmp_path) -> None:
        r = invoke("create", "Task", "--actor", _ACTOR, "--json")
        task_id = json.loads(r.output)["data"]["id"]

        src_file = tmp_path / "output.log"
        src_file.write_text("output")

        invoke(
            "attach",
            task_id,
            str(src_file),
            "--role",
            "debug_log",
            "--actor",
            _ACTOR,
        )

        # Check event data includes role
        lattice_dir = initialized_root / LATTICE_DIR
        event_path = lattice_dir / "events" / f"{task_id}.jsonl"
        lines = event_path.read_text().strip().split("\n")
        attach_event = json.loads(lines[1])
        assert attach_event["data"]["role"] == "debug_log"

        # Check snapshot artifact_refs stores role
        snap = json.loads((lattice_dir / "tasks" / f"{task_id}.json").read_text())
        assert snap["artifact_refs"][0]["role"] == "debug_log"


# ---------------------------------------------------------------------------
# Attach URL
# ---------------------------------------------------------------------------


class TestAttachURL:
    def test_attach_url(self, invoke, initialized_root) -> None:
        r = invoke("create", "Task", "--actor", _ACTOR, "--json")
        task_id = json.loads(r.output)["data"]["id"]

        result = invoke(
            "attach",
            task_id,
            "https://example.com/docs",
            "--actor",
            _ACTOR,
            "--json",
        )
        assert result.exit_code == 0
        data = json.loads(result.output)["data"]
        assert data["type"] == "reference"
        assert data["custom_fields"]["url"] == "https://example.com/docs"
        assert data["title"] == "https://example.com/docs"

    def test_url_no_payload_file(self, invoke, initialized_root) -> None:
        r = invoke("create", "Task", "--actor", _ACTOR, "--json")
        task_id = json.loads(r.output)["data"]["id"]

        result = invoke(
            "attach",
            task_id,
            "http://example.com/page",
            "--actor",
            _ACTOR,
            "--json",
        )
        data = json.loads(result.output)["data"]
        assert data["payload"]["file"] is None
        assert data["payload"]["content_type"] is None
        assert data["payload"]["size_bytes"] is None

    def test_url_with_custom_title(self, invoke, initialized_root) -> None:
        r = invoke("create", "Task", "--actor", _ACTOR, "--json")
        task_id = json.loads(r.output)["data"]["id"]

        result = invoke(
            "attach",
            task_id,
            "https://docs.example.com",
            "--title",
            "API Documentation",
            "--actor",
            _ACTOR,
            "--json",
        )
        data = json.loads(result.output)["data"]
        assert data["title"] == "API Documentation"


# ---------------------------------------------------------------------------
# Sensitive flag
# ---------------------------------------------------------------------------


class TestSensitiveFlag:
    def test_sensitive_metadata(self, invoke, initialized_root, tmp_path) -> None:
        r = invoke("create", "Task", "--actor", _ACTOR, "--json")
        task_id = json.loads(r.output)["data"]["id"]

        src_file = tmp_path / "secrets.env"
        src_file.write_text("API_KEY=xxx")

        result = invoke(
            "attach",
            task_id,
            str(src_file),
            "--sensitive",
            "--actor",
            _ACTOR,
            "--json",
        )
        assert result.exit_code == 0
        data = json.loads(result.output)["data"]
        assert data["sensitive"] is True

    def test_not_sensitive_by_default(self, invoke, initialized_root, tmp_path) -> None:
        r = invoke("create", "Task", "--actor", _ACTOR, "--json")
        task_id = json.loads(r.output)["data"]["id"]

        src_file = tmp_path / "readme.txt"
        src_file.write_text("hello")

        result = invoke("attach", task_id, str(src_file), "--actor", _ACTOR, "--json")
        data = json.loads(result.output)["data"]
        assert data["sensitive"] is False


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------


class TestIdempotency:
    def test_same_id_same_source_succeeds(self, invoke, initialized_root, tmp_path) -> None:
        r = invoke("create", "Task", "--actor", _ACTOR, "--json")
        task_id = json.loads(r.output)["data"]["id"]

        src_file = tmp_path / "data.txt"
        src_file.write_text("content")

        art_id = "art_01AAAAAAAAAAAAAAAAAAAAAAAA"

        # First attach
        r1 = invoke(
            "attach",
            task_id,
            str(src_file),
            "--id",
            art_id,
            "--actor",
            _ACTOR,
            "--json",
        )
        assert r1.exit_code == 0

        # Second attach with same source
        r2 = invoke(
            "attach",
            task_id,
            str(src_file),
            "--id",
            art_id,
            "--actor",
            _ACTOR,
            "--json",
        )
        assert r2.exit_code == 0
        assert "idempotent" in r2.output.lower() or json.loads(r2.output)["ok"]

    def test_same_id_different_source_conflicts(self, invoke, initialized_root, tmp_path) -> None:
        r = invoke("create", "Task", "--actor", _ACTOR, "--json")
        task_id = json.loads(r.output)["data"]["id"]

        src_file1 = tmp_path / "file1.txt"
        src_file1.write_text("content1")

        src_file2 = tmp_path / "file2.txt"
        src_file2.write_text("content2")

        art_id = "art_01BBBBBBBBBBBBBBBBBBBBBBBB"

        # First attach
        r1 = invoke(
            "attach",
            task_id,
            str(src_file1),
            "--id",
            art_id,
            "--actor",
            _ACTOR,
        )
        assert r1.exit_code == 0

        # Second attach with different source (different filename -> different payload.file)
        r2 = invoke(
            "attach",
            task_id,
            str(src_file2),
            "--id",
            art_id,
            "--actor",
            _ACTOR,
            "--json",
        )
        assert r2.exit_code != 0
        parsed = json.loads(r2.output)
        assert parsed["ok"] is False
        assert parsed["error"]["code"] == "CONFLICT"

    def test_same_id_same_url_succeeds(self, invoke, initialized_root) -> None:
        r = invoke("create", "Task", "--actor", _ACTOR, "--json")
        task_id = json.loads(r.output)["data"]["id"]

        art_id = "art_01CCCCCCCCCCCCCCCCCCCCCCCC"
        url = "https://example.com/doc"

        r1 = invoke("attach", task_id, url, "--id", art_id, "--actor", _ACTOR)
        assert r1.exit_code == 0

        r2 = invoke("attach", task_id, url, "--id", art_id, "--actor", _ACTOR)
        assert r2.exit_code == 0

    def test_same_id_different_url_conflicts(self, invoke, initialized_root) -> None:
        r = invoke("create", "Task", "--actor", _ACTOR, "--json")
        task_id = json.loads(r.output)["data"]["id"]

        art_id = "art_01DDDDDDDDDDDDDDDDDDDDDDDD"

        r1 = invoke(
            "attach",
            task_id,
            "https://example.com/a",
            "--id",
            art_id,
            "--actor",
            _ACTOR,
        )
        assert r1.exit_code == 0

        r2 = invoke(
            "attach",
            task_id,
            "https://example.com/b",
            "--id",
            art_id,
            "--actor",
            _ACTOR,
            "--json",
        )
        assert r2.exit_code != 0
        parsed = json.loads(r2.output)
        assert parsed["error"]["code"] == "CONFLICT"


# ---------------------------------------------------------------------------
# Error cases
# ---------------------------------------------------------------------------


class TestErrorCases:
    def test_invalid_task(self, invoke, initialized_root, tmp_path) -> None:
        src_file = tmp_path / "f.txt"
        src_file.write_text("x")

        result = invoke(
            "attach",
            "task_01ZZZZZZZZZZZZZZZZZZZZZZZZ",
            str(src_file),
            "--actor",
            _ACTOR,
            "--json",
        )
        assert result.exit_code != 0
        parsed = json.loads(result.output)
        assert parsed["ok"] is False
        assert parsed["error"]["code"] == "NOT_FOUND"

    def test_file_not_found(self, invoke, initialized_root) -> None:
        r = invoke("create", "Task", "--actor", _ACTOR, "--json")
        task_id = json.loads(r.output)["data"]["id"]

        result = invoke(
            "attach",
            task_id,
            "/nonexistent/path/file.txt",
            "--actor",
            _ACTOR,
            "--json",
        )
        assert result.exit_code != 0
        parsed = json.loads(result.output)
        assert parsed["ok"] is False
        assert parsed["error"]["code"] == "NOT_FOUND"

    def test_invalid_artifact_type(self, invoke, initialized_root, tmp_path) -> None:
        r = invoke("create", "Task", "--actor", _ACTOR, "--json")
        task_id = json.loads(r.output)["data"]["id"]

        src_file = tmp_path / "f.txt"
        src_file.write_text("x")

        result = invoke(
            "attach",
            task_id,
            str(src_file),
            "--type",
            "invalid_type",
            "--actor",
            _ACTOR,
            "--json",
        )
        assert result.exit_code != 0
        parsed = json.loads(result.output)
        assert parsed["error"]["code"] == "VALIDATION_ERROR"

    def test_invalid_actor(self, invoke, initialized_root, tmp_path) -> None:
        r = invoke("create", "Task", "--actor", _ACTOR, "--json")
        task_id = json.loads(r.output)["data"]["id"]

        src_file = tmp_path / "f.txt"
        src_file.write_text("x")

        result = invoke(
            "attach",
            task_id,
            str(src_file),
            "--actor",
            "badactor",
            "--json",
        )
        assert result.exit_code != 0
        parsed = json.loads(result.output)
        assert parsed["error"]["code"] == "INVALID_ACTOR"


# ---------------------------------------------------------------------------
# JSON output
# ---------------------------------------------------------------------------


class TestJsonOutput:
    def test_json_envelope_on_success(self, invoke, initialized_root, tmp_path) -> None:
        r = invoke("create", "Task", "--actor", _ACTOR, "--json")
        task_id = json.loads(r.output)["data"]["id"]

        src_file = tmp_path / "test.txt"
        src_file.write_text("test content")

        result = invoke("attach", task_id, str(src_file), "--actor", _ACTOR, "--json")
        assert result.exit_code == 0
        parsed = json.loads(result.output)
        assert parsed["ok"] is True
        assert "data" in parsed
        assert parsed["data"]["id"].startswith("art_")

    def test_quiet_output(self, invoke, initialized_root, tmp_path) -> None:
        r = invoke("create", "Task", "--actor", _ACTOR, "--json")
        task_id = json.loads(r.output)["data"]["id"]

        src_file = tmp_path / "test.txt"
        src_file.write_text("test content")

        result = invoke("attach", task_id, str(src_file), "--actor", _ACTOR, "--quiet")
        assert result.exit_code == 0
        art_id = result.output.strip()
        assert art_id.startswith("art_")


# ---------------------------------------------------------------------------
# Type inference
# ---------------------------------------------------------------------------


class TestTypeInference:
    def test_file_inferred_as_file(self, invoke, initialized_root, tmp_path) -> None:
        r = invoke("create", "Task", "--actor", _ACTOR, "--json")
        task_id = json.loads(r.output)["data"]["id"]

        src_file = tmp_path / "data.json"
        src_file.write_text("{}")

        result = invoke("attach", task_id, str(src_file), "--actor", _ACTOR, "--json")
        data = json.loads(result.output)["data"]
        assert data["type"] == "file"

    def test_http_url_inferred_as_reference(self, invoke, initialized_root) -> None:
        r = invoke("create", "Task", "--actor", _ACTOR, "--json")
        task_id = json.loads(r.output)["data"]["id"]

        result = invoke(
            "attach",
            task_id,
            "http://example.com",
            "--actor",
            _ACTOR,
            "--json",
        )
        data = json.loads(result.output)["data"]
        assert data["type"] == "reference"

    def test_https_url_inferred_as_reference(self, invoke, initialized_root) -> None:
        r = invoke("create", "Task", "--actor", _ACTOR, "--json")
        task_id = json.loads(r.output)["data"]["id"]

        result = invoke(
            "attach",
            task_id,
            "https://example.com",
            "--actor",
            _ACTOR,
            "--json",
        )
        data = json.loads(result.output)["data"]
        assert data["type"] == "reference"

    def test_explicit_type_overrides_inference(self, invoke, initialized_root, tmp_path) -> None:
        r = invoke("create", "Task", "--actor", _ACTOR, "--json")
        task_id = json.loads(r.output)["data"]["id"]

        src_file = tmp_path / "build.log"
        src_file.write_text("build output")

        result = invoke(
            "attach",
            task_id,
            str(src_file),
            "--type",
            "log",
            "--actor",
            _ACTOR,
            "--json",
        )
        data = json.loads(result.output)["data"]
        assert data["type"] == "log"


# ---------------------------------------------------------------------------
# Attach inline
# ---------------------------------------------------------------------------


class TestAttachInline:
    def test_inline_creates_artifact(self, invoke, create_task) -> None:
        """--inline creates a note artifact without needing a file."""
        task = create_task("Inline test")
        task_id = task["id"]

        result = invoke(
            "attach", task_id,
            "--inline", "Review passed. No issues found.",
            "--role", "review",
            "--actor", _ACTOR,
            "--json",
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)["data"]
        assert data["type"] == "note"
        assert data["title"] == "review"

    def test_inline_payload_stored(self, invoke, create_task, initialized_root) -> None:
        """Inline text is saved as a .md payload file."""
        from lattice.storage.fs import LATTICE_DIR

        task = create_task("Inline payload")
        task_id = task["id"]

        result = invoke(
            "attach", task_id,
            "--inline", "some review text",
            "--actor", _ACTOR,
            "--json",
        )
        assert result.exit_code == 0, result.output
        data = json.loads(result.output)["data"]
        payload_file = data["payload"]["file"]
        assert payload_file is not None

        lattice_dir = initialized_root / LATTICE_DIR
        payload_path = lattice_dir / "artifacts" / "payload" / payload_file
        assert payload_path.exists()
        assert payload_path.read_text() == "some review text"

    def test_inline_and_source_mutually_exclusive(self, invoke, create_task, tmp_path) -> None:
        """Providing both SOURCE and --inline is an error."""
        task = create_task("Exclusivity test")
        src = tmp_path / "f.txt"
        src.write_text("hi")
        result = invoke(
            "attach", task["id"], str(src),
            "--inline", "also this",
            "--actor", _ACTOR,
        )
        assert result.exit_code != 0
        assert "not both" in result.output.lower()

    def test_neither_source_nor_inline_errors(self, invoke, create_task) -> None:
        """Providing neither SOURCE nor --inline is an error."""
        task = create_task("Neither test")
        result = invoke("attach", task["id"], "--actor", _ACTOR)
        assert result.exit_code != 0
