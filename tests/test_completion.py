import json
from click.shell_completion import CompletionItem
from lattice.completion import (
    complete_task_id,
    complete_status,
    complete_actor,
    complete_resource_name,
    complete_session_name,
    complete_relationship_type,
)


def _make_ids_json(tmp_path, entries):
    lattice_dir = tmp_path / ".lattice"
    lattice_dir.mkdir()
    ids_file = lattice_dir / "ids.json"
    ids_file.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "next_seqs": {},
                "map": entries,
            }
        )
    )


def test_complete_task_id_returns_matching_short_ids(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _make_ids_json(tmp_path, {"LAT-1": "ulid1", "LAT-2": "ulid2", "LAT-10": "ulid10"})
    results = complete_task_id(None, None, "LAT-1")
    values = [r.value for r in results]
    assert "LAT-1" in values
    assert "LAT-10" in values
    assert "LAT-2" not in values


def test_complete_task_id_empty_incomplete_returns_all(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _make_ids_json(tmp_path, {"LAT-1": "ulid1", "LAT-2": "ulid2"})
    results = complete_task_id(None, None, "")
    assert len(results) == 2


def test_complete_task_id_no_lattice_returns_empty(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    results = complete_task_id(None, None, "")
    assert results == []


def test_complete_task_id_returns_completion_items(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _make_ids_json(tmp_path, {"LAT-1": "ulid1"})
    results = complete_task_id(None, None, "")
    assert all(isinstance(r, CompletionItem) for r in results)


def test_complete_task_id_corrupted_json_returns_empty(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".lattice").mkdir()
    (tmp_path / ".lattice" / "ids.json").write_text("not json {{{")
    results = complete_task_id(None, None, "")
    assert results == []


def _make_config_json(tmp_path, statuses):
    lattice_dir = tmp_path / ".lattice"
    lattice_dir.mkdir(exist_ok=True)
    config = {"workflow": {"statuses": statuses}}
    (lattice_dir / "config.json").write_text(json.dumps(config))


def test_complete_status_reads_from_config(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _make_config_json(tmp_path, ["backlog", "in_progress", "done"])
    results = complete_status(None, None, "")
    values = [r.value for r in results]
    assert values == ["backlog", "in_progress", "done"]


def test_complete_status_filters_by_prefix(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    _make_config_json(tmp_path, ["backlog", "in_progress", "done"])
    results = complete_status(None, None, "in")
    values = [r.value for r in results]
    assert values == ["in_progress"]


def test_complete_status_falls_back_when_no_config(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    results = complete_status(None, None, "")
    values = [r.value for r in results]
    assert "backlog" in values
    assert "done" in values


def _make_snapshot(tmp_path, task_id, actor):
    tasks_dir = tmp_path / ".lattice" / "tasks"
    tasks_dir.mkdir(parents=True, exist_ok=True)
    snapshot = {"id": task_id, "assigned_to": actor, "title": "Test task"}
    (tasks_dir / f"{task_id}.json").write_text(json.dumps(snapshot))


def test_complete_actor_returns_unique_actors(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".lattice").mkdir(exist_ok=True)
    _make_snapshot(tmp_path, "ulid1", "human:fede")
    _make_snapshot(tmp_path, "ulid2", "human:fede")
    _make_snapshot(tmp_path, "ulid3", "agent:claude")
    results = complete_actor(None, None, "")
    values = [r.value for r in results]
    assert len(values) == len(set(values))
    assert "human:fede" in values
    assert "agent:claude" in values


def test_complete_actor_filters_by_prefix(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".lattice").mkdir(exist_ok=True)
    _make_snapshot(tmp_path, "ulid1", "human:fede")
    _make_snapshot(tmp_path, "ulid2", "agent:claude")
    results = complete_actor(None, None, "human")
    values = [r.value for r in results]
    assert "human:fede" in values
    assert "agent:claude" not in values


def _make_resource(tmp_path, name):
    res_dir = tmp_path / ".lattice" / "resources"
    res_dir.mkdir(parents=True, exist_ok=True)
    (res_dir / f"{name}.json").write_text(json.dumps({"name": name}))


def test_complete_resource_name_returns_names(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".lattice").mkdir(exist_ok=True)
    _make_resource(tmp_path, "gpu-slot")
    _make_resource(tmp_path, "db-lock")
    results = complete_resource_name(None, None, "")
    values = [r.value for r in results]
    assert "gpu-slot" in values
    assert "db-lock" in values


def test_complete_resource_name_filters_by_prefix(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".lattice").mkdir(exist_ok=True)
    _make_resource(tmp_path, "gpu-slot")
    _make_resource(tmp_path, "db-lock")
    results = complete_resource_name(None, None, "gpu")
    values = [r.value for r in results]
    assert values == ["gpu-slot"]


def _make_session(tmp_path, name):
    sess_dir = tmp_path / ".lattice" / "sessions"
    sess_dir.mkdir(parents=True, exist_ok=True)
    (sess_dir / f"{name}.json").write_text(json.dumps({"name": name, "status": "active"}))


def test_complete_session_name_returns_names(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".lattice").mkdir(exist_ok=True)
    _make_session(tmp_path, "argus")
    _make_session(tmp_path, "builder")
    results = complete_session_name(None, None, "")
    values = [r.value for r in results]
    assert "argus" in values
    assert "builder" in values


def test_complete_relationship_type_returns_all(tmp_path):
    results = complete_relationship_type(None, None, "")
    values = [r.value for r in results]
    assert "blocks" in values
    assert "blocked_by" in values
    assert "depends_on" in values


def test_complete_relationship_type_filters(tmp_path):
    results = complete_relationship_type(None, None, "b")
    values = [r.value for r in results]
    assert "blocks" in values
    assert "blocked_by" in values
    assert "subtask_of" not in values
