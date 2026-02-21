import json
from click.shell_completion import CompletionItem
from lattice.completion import complete_task_id


def _make_ids_json(tmp_path, entries):
    lattice_dir = tmp_path / ".lattice"
    lattice_dir.mkdir()
    ids_file = lattice_dir / "ids.json"
    ids_file.write_text(json.dumps({
        "schema_version": 2,
        "next_seqs": {},
        "map": entries,
    }))


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
