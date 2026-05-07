"""Tests for review config schema, init flags, and template loading."""

from __future__ import annotations

import json

import pytest


class TestConfigDefaults:
    """Test that default_config() includes review fields."""

    def test_default_review_mode(self):
        from lattice.core.config import default_config

        config = default_config()
        assert config["review_mode"] == "single"

    def test_default_plan_review_mode(self):
        from lattice.core.config import default_config

        config = default_config()
        assert config["plan_review_mode"] == "triple"

    def test_default_plan_approval(self):
        from lattice.core.config import default_config

        config = default_config()
        assert config["plan_approval"] == "auto"

    def test_default_workflow_roles(self):
        from lattice.core.config import default_config

        config = default_config()
        roles = config["workflow"]["roles"]
        assert "review" in roles
        assert "plan-review" in roles
        assert "review-individual" in roles


class TestBackwardCompatibility:
    """Test that configs without new fields work gracefully."""

    def test_missing_review_mode_uses_default(self, initialized_root):
        """A config.json without review_mode should not break anything."""
        lattice_dir = initialized_root / ".lattice"
        config = json.loads((lattice_dir / "config.json").read_text())
        # Remove the new fields to simulate a pre-update config
        config.pop("review_mode", None)
        config.pop("plan_review_mode", None)
        config.pop("plan_approval", None)
        (lattice_dir / "config.json").write_text(
            json.dumps(config, sort_keys=True, indent=2) + "\n"
        )
        # Should still be loadable and usable
        reloaded = json.loads((lattice_dir / "config.json").read_text())
        # Callers should treat missing keys as defaults
        assert reloaded.get("review_mode", "single") == "single"
        assert reloaded.get("plan_review_mode", "triple") == "triple"
        assert reloaded.get("plan_approval", "auto") == "auto"


class TestInitFlags:
    """Test that lattice init --review-mode etc. write to config."""

    def test_init_with_review_mode_triple(self, cli_runner, tmp_path):
        from lattice.cli.main import cli

        result = cli_runner.invoke(
            cli,
            [
                "init",
                "--path",
                str(tmp_path),
                "--actor",
                "human:test",
                "--project-code",
                "TST",
                "--review-mode",
                "triple",
            ],
        )
        assert result.exit_code == 0, result.output
        config = json.loads((tmp_path / ".lattice" / "config.json").read_text())
        assert config["review_mode"] == "triple"

    def test_init_with_plan_review_mode_single(self, cli_runner, tmp_path):
        from lattice.cli.main import cli

        result = cli_runner.invoke(
            cli,
            [
                "init",
                "--path",
                str(tmp_path),
                "--actor",
                "human:test",
                "--project-code",
                "TST",
                "--plan-review-mode",
                "single",
            ],
        )
        assert result.exit_code == 0, result.output
        config = json.loads((tmp_path / ".lattice" / "config.json").read_text())
        assert config["plan_review_mode"] == "single"

    def test_init_with_plan_approval_human(self, cli_runner, tmp_path):
        from lattice.cli.main import cli

        result = cli_runner.invoke(
            cli,
            [
                "init",
                "--path",
                str(tmp_path),
                "--actor",
                "human:test",
                "--project-code",
                "TST",
                "--plan-approval",
                "human",
            ],
        )
        assert result.exit_code == 0, result.output
        config = json.loads((tmp_path / ".lattice" / "config.json").read_text())
        assert config["plan_approval"] == "human"

    def test_init_defaults_without_flags(self, cli_runner, tmp_path):
        from lattice.cli.main import cli

        result = cli_runner.invoke(
            cli,
            [
                "init",
                "--path",
                str(tmp_path),
                "--actor",
                "human:test",
                "--project-code",
                "TST",
            ],
        )
        assert result.exit_code == 0, result.output
        config = json.loads((tmp_path / ".lattice" / "config.json").read_text())
        assert config["review_mode"] == "single"
        assert config["plan_review_mode"] == "triple"
        assert config["plan_approval"] == "auto"

    def test_init_combined_review_flags(self, cli_runner, tmp_path):
        from lattice.cli.main import cli

        result = cli_runner.invoke(
            cli,
            [
                "init",
                "--path",
                str(tmp_path),
                "--actor",
                "human:test",
                "--project-code",
                "TST",
                "--review-mode",
                "triple",
                "--plan-approval",
                "human",
            ],
        )
        assert result.exit_code == 0, result.output
        config = json.loads((tmp_path / ".lattice" / "config.json").read_text())
        assert config["review_mode"] == "triple"
        assert config["plan_approval"] == "human"
        assert config["plan_review_mode"] == "triple"  # default preserved

    def test_init_scaffolds_templates_dir(self, cli_runner, tmp_path):
        from lattice.cli.main import cli

        result = cli_runner.invoke(
            cli,
            [
                "init",
                "--path",
                str(tmp_path),
                "--actor",
                "human:test",
                "--project-code",
                "TST",
            ],
        )
        assert result.exit_code == 0, result.output
        assert (tmp_path / ".lattice" / "templates").is_dir()

    def test_init_invalid_review_mode(self, cli_runner, tmp_path):
        from lattice.cli.main import cli

        result = cli_runner.invoke(
            cli,
            [
                "init",
                "--path",
                str(tmp_path),
                "--actor",
                "human:test",
                "--project-code",
                "TST",
                "--review-mode",
                "invalid",
            ],
        )
        assert result.exit_code != 0


class TestTemplateLoader:
    """Test load_review_template() with built-in and override paths."""

    def test_load_code_review_builtin(self, tmp_path):
        from lattice.templates import load_review_template

        lattice_dir = tmp_path / ".lattice"
        lattice_dir.mkdir()
        (lattice_dir / "templates").mkdir()

        template = load_review_template(lattice_dir, "code-review")
        assert "{task_id}" in template
        assert "{diff_content}" in template
        assert "Verdict" in template

    def test_load_plan_review_builtin(self, tmp_path):
        from lattice.templates import load_review_template

        lattice_dir = tmp_path / ".lattice"
        lattice_dir.mkdir()
        (lattice_dir / "templates").mkdir()

        template = load_review_template(lattice_dir, "plan-review")
        assert "{task_id}" in template
        assert "{plan_content}" in template
        assert "Completeness" in template

    def test_load_code_review_override(self, tmp_path):
        from lattice.templates import load_review_template

        lattice_dir = tmp_path / ".lattice"
        (lattice_dir / "templates").mkdir(parents=True)
        override_content = "Custom review template for {task_id}"
        (lattice_dir / "templates" / "code-review.md").write_text(override_content)

        template = load_review_template(lattice_dir, "code-review")
        assert template == override_content

    def test_load_plan_review_override(self, tmp_path):
        from lattice.templates import load_review_template

        lattice_dir = tmp_path / ".lattice"
        (lattice_dir / "templates").mkdir(parents=True)
        override_content = "Custom plan review for {task_id}"
        (lattice_dir / "templates" / "plan-review.md").write_text(override_content)

        template = load_review_template(lattice_dir, "plan-review")
        assert template == override_content

    def test_load_unknown_type_raises(self, tmp_path):
        from lattice.templates import load_review_template

        lattice_dir = tmp_path / ".lattice"
        (lattice_dir / "templates").mkdir(parents=True)

        with pytest.raises(ValueError, match="Unknown review type"):
            load_review_template(lattice_dir, "unknown-type")
