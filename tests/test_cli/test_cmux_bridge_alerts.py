"""Tests for cmux_bridge alert visuals (LAT-210)."""

from __future__ import annotations

import json

from lattice.cli import cmux_bridge


class _CallRecorder:
    """Stand-in for ``_run_cmux`` that captures positional args."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, *args: str) -> bool:
        self.calls.append(list(args))
        return True


def _patch_run(monkeypatch, recorder: _CallRecorder) -> None:
    monkeypatch.setattr(cmux_bridge, "_run_cmux", recorder)
    # Force cmux_available -> True regardless of env.
    monkeypatch.setattr(cmux_bridge, "cmux_available", lambda: True)


class TestRaiseAlertVisual:
    def test_emits_metadata_status_flash_notify_for_needs_human(self, monkeypatch):
        rec = _CallRecorder()
        _patch_run(monkeypatch, rec)

        cmux_bridge.raise_alert_visual(
            workspace="workspace:7",
            surface="surface:42",
            alert_name="needs_human",
            short="Approve plan",
            long="Reviewer says PASS, awaiting human approval.",
            flash=True,
            notify=True,
        )

        # 4 calls: set-metadata, set-status, trigger-flash, notify
        assert len(rec.calls) == 4
        verbs = [c[0] for c in rec.calls]
        assert verbs == ["set-metadata", "set-status", "trigger-flash", "notify"]

        meta_args = rec.calls[0]
        assert "--workspace" in meta_args
        assert "--surface" in meta_args
        assert "--json" in meta_args
        json_payload = meta_args[meta_args.index("--json") + 1]
        decoded = json.loads(json_payload)
        assert decoded == {"lattice": {"needs_human": {"short": "Approve plan"}}}

        status_args = rec.calls[1]
        assert "--color" in status_args
        # needs_human default color is yellow
        assert "#FFD600" in status_args

    def test_skips_flash_and_notify_when_disabled(self, monkeypatch):
        rec = _CallRecorder()
        _patch_run(monkeypatch, rec)

        cmux_bridge.raise_alert_visual(
            workspace="workspace:7",
            surface="surface:42",
            alert_name="blocked",
            short="CI failing",
            long=None,
            flash=False,
            notify=False,
        )
        verbs = [c[0] for c in rec.calls]
        assert "trigger-flash" not in verbs
        assert "notify" not in verbs
        # Still emits metadata + sidebar pill.
        assert verbs == ["set-metadata", "set-status"]

    def test_visual_overrides_take_precedence(self, monkeypatch):
        rec = _CallRecorder()
        _patch_run(monkeypatch, rec)

        cmux_bridge.raise_alert_visual(
            workspace="workspace:7",
            surface="surface:42",
            alert_name="needs_human",
            short="Override color",
            long=None,
            flash=False,
            notify=False,
            visual_overrides={"color": "#00FF00"},
        )
        status_args = rec.calls[1]
        assert "#00FF00" in status_args
        # Default icon is preserved (per-key merge, not full replacement).
        assert "exclamationmark.triangle.fill" in status_args


class TestClearAlertVisual:
    def test_emits_clear_status_and_clear_metadata(self, monkeypatch):
        rec = _CallRecorder()
        _patch_run(monkeypatch, rec)

        cmux_bridge.clear_alert_visual(
            workspace="workspace:7",
            surface="surface:42",
            alert_name="needs_human",
        )
        verbs = [c[0] for c in rec.calls]
        assert verbs == ["clear-status", "clear-metadata"]
        meta_args = rec.calls[1]
        assert "--key" in meta_args
        assert meta_args[meta_args.index("--key") + 1] == "lattice.needs_human"


class TestC11EnvDetection:
    def test_get_workspace_falls_back_to_c11_var(self, monkeypatch):
        monkeypatch.delenv("CMUX_WORKSPACE_ID", raising=False)
        monkeypatch.setenv("C11_WORKSPACE_ID", "workspace:99")
        assert cmux_bridge.get_workspace() == "workspace:99"

    def test_get_surface_falls_back_to_c11_var(self, monkeypatch):
        monkeypatch.delenv("CMUX_SURFACE_ID", raising=False)
        monkeypatch.setenv("C11_SURFACE_ID", "surface:88")
        assert cmux_bridge.get_surface() == "surface:88"

    def test_cmux_available_with_only_c11_var(self, monkeypatch):
        monkeypatch.delenv("CMUX_WORKSPACE_ID", raising=False)
        monkeypatch.setenv("C11_WORKSPACE_ID", "workspace:5")
        assert cmux_bridge.cmux_available() is True

    def test_cmux_var_takes_precedence_over_c11(self, monkeypatch):
        monkeypatch.setenv("CMUX_WORKSPACE_ID", "workspace:1")
        monkeypatch.setenv("C11_WORKSPACE_ID", "workspace:2")
        assert cmux_bridge.get_workspace() == "workspace:1"


class TestStatusVisualsCleanup:
    def test_status_visuals_drops_needs_human_and_blocked(self):
        assert "needs_human" not in cmux_bridge.STATUS_VISUALS
        assert "blocked" not in cmux_bridge.STATUS_VISUALS

    def test_status_labels_drops_needs_human_and_blocked(self):
        assert "needs_human" not in cmux_bridge.STATUS_LABELS
        assert "blocked" not in cmux_bridge.STATUS_LABELS

    def test_alert_visuals_present(self):
        assert "needs_human" in cmux_bridge.ALERT_VISUALS
        assert "blocked" in cmux_bridge.ALERT_VISUALS
